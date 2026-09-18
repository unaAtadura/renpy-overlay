"""系统音频录制：三重回退方案（soundcard 环回 / WASAPI 环回 / 立体声混音）。

移植自参考项目 screen_translaor_4.0 的 audio_recorder 实现，并增加中断支持：

1. **soundcard 环回**：默认扬声器的 loopback 麦克风整段录制；实现简单但部分
   蓝牙耳机不支持；
2. **pyaudiowpatch WASAPI 环回**：Windows 原生环回，支持蓝牙耳机；逐块读取，
   每块之间检查 ``stop_event`` 可提前退出；
3. **pyaudio 立体声混音**：依赖用户在 Windows 声音控制面板手动启用的录音设备。

所有重依赖延迟到录制函数内部导入，缺库的方案自动跳过；三案全失败返回 None，
日志中给出各方案错误与可执行的排查提示。调用方负责识别后删除临时 wav 文件。
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import wave
from importlib.util import find_spec
from pathlib import Path

logger = logging.getLogger("renpy_overlay.song_recognition.recorder")

#: 逐块读取的块大小（帧）：兼顾中断响应速度与读取开销
_CHUNK = 1024
#: soundcard / pyaudio 方案的采样率；WASAPI 环回方案跟随设备默认采样率
_SAMPLE_RATE = 44100


def _is_available(name: str) -> bool:
    """依赖可用性探测：只查包元数据不真正导入（重库导入放到录制函数内部）。"""
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - 非法包名等极端场景
        return False


#: 三方案的可用性标志（模块导入时确定，运行期不变）
SOUNDCARD_AVAILABLE = _is_available("soundcard") and _is_available("soundfile")
PYAUDIOWPATCH_AVAILABLE = _is_available("pyaudiowpatch")
PYAUDIO_AVAILABLE = _is_available("pyaudio")


def record_system_audio(
    duration: int = 8, stop_event: threading.Event | None = None
) -> Path | None:
    """录制系统音频（三案自动回退），返回临时 wav 路径；失败或被中止返回 None。

    Args:
        duration: 录制时长（秒）
        stop_event: 中断信号；逐块读取的方案在块间检查，预置时直接跳过录制

    Returns:
        临时音频文件路径（调用方负责删除），失败/被中止返回 None
    """
    if stop_event is not None and stop_event.is_set():
        logger.info("录制开始前已被中止，跳过录制")
        return None

    errors: list[str] = []

    if SOUNDCARD_AVAILABLE:
        result, err = _record_with_soundcard(duration, stop_event)
        if result is not None:
            return result
        if _aborted(stop_event):
            return None
        if err:
            errors.append(f"方案1 (soundcard 环回): {err}")

    if PYAUDIOWPATCH_AVAILABLE:
        result, err = _record_with_pyaudiowpatch(duration, stop_event)
        if result is not None:
            return result
        if _aborted(stop_event):
            return None
        if err:
            errors.append(f"方案2 (pyaudiowpatch WASAPI 环回): {err}")

    if PYAUDIO_AVAILABLE:
        result, err = _record_with_pyaudio(duration, stop_event)
        if result is not None:
            return result
        if _aborted(stop_event):
            return None
        if err:
            errors.append(f"方案3 (pyaudio 立体声混音): {err}")

    logger.error("无法录制系统音频（duration=%ds）：%s", duration, "；".join(errors))
    logger.error("可能的原因：蓝牙耳机不支持环回（改用内置扬声器/有线耳机），")
    logger.error("或未安装任何录制库（soundcard / pyaudiowpatch / pyaudio），")
    logger.error("或未在 Windows 声音控制面板启用「立体声混音」设备。")
    return None


def _aborted(stop_event: threading.Event | None) -> bool:
    """是否已被外部中止（双击打断等）：中止后不再回退到下一个方案。"""
    return stop_event is not None and stop_event.is_set()


def _record_with_soundcard(
    duration: int, stop_event: threading.Event | None
) -> tuple[Path | None, str | None]:
    """方案 1：soundcard 默认扬声器环形回录（整段阻塞录制，无法中途打断）。"""
    try:
        import numpy as np
        import soundcard as sc
        import soundfile as sf

        logger.info("[方案1] 正在使用 soundcard 查找系统音频录制设备…")

        default_speaker = sc.default_speaker()
        logger.info("[方案1] 使用默认扬声器：%s", default_speaker.name)

        if "蓝牙耳机" in default_speaker.name or "Bluetooth" in default_speaker.name:
            logger.info("[方案1] 检测到蓝牙耳机，soundcard 环回可能不支持")
            return None, "蓝牙耳机可能不支持 soundcard 环回"

        loopback_mic = sc.get_microphone(
            id=str(default_speaker.name), include_loopback=True
        )
        logger.info("[方案1] 成功获取环形回录麦克风：%s", loopback_mic.name)

        with loopback_mic.recorder(samplerate=_SAMPLE_RATE, channels=2) as mic:
            audio_data = mic.record(numframes=int(_SAMPLE_RATE * duration))

        if _aborted(stop_event):  # 录制完成才检查：整段阻塞无法更早退出
            logger.info("[方案1] 录制完成后发现已被中止，丢弃结果")
            return None, None

        audio_data_int16 = np.clip(audio_data * 32767, -32768, 32767).astype(np.int16)

        temp_file = _temp_wav_path()
        sf.write(str(temp_file), audio_data_int16, _SAMPLE_RATE, subtype="PCM_16")
        logger.info("[方案1] 音频已保存到临时文件：%s", temp_file)
        return temp_file, None

    except Exception as exc:
        logger.warning("[方案1] soundcard 录制失败：%s", exc)
        return None, str(exc)


def _record_with_pyaudiowpatch(
    duration: int, stop_event: threading.Event | None
) -> tuple[Path | None, str | None]:
    """方案 2：pyaudiowpatch WASAPI 环回（支持蓝牙耳机，逐块读取可中断）。"""
    try:
        import pyaudiowpatch as pyaudio_patch

        logger.info("[方案2] 正在使用 pyaudiowpatch 查找 WASAPI 环回设备…")

        with pyaudio_patch.PyAudio() as p:
            try:
                wasapi_loopback = p.get_default_wasapi_loopback()
            except OSError as exc:
                logger.warning("[方案2] 未找到 WASAPI 环回设备：%s", exc)
                return None, "未找到 WASAPI 环回设备"

            sample_rate = int(wasapi_loopback["defaultSampleRate"])
            channels = int(wasapi_loopback["maxInputChannels"])
            logger.info(
                "[方案2] 使用 WASAPI 环回设备：%s（采样率 %d Hz，%d 声道）",
                wasapi_loopback["name"],
                sample_rate,
                channels,
            )

            with p.open(
                format=pyaudio_patch.paInt16,
                channels=channels,
                rate=sample_rate,
                input=True,
                frames_per_buffer=_CHUNK,
                input_device_index=wasapi_loopback["index"],
            ) as stream:
                frames = []
                total_chunks = int(sample_rate / _CHUNK * duration)
                for _ in range(total_chunks):
                    if _aborted(stop_event):
                        logger.info("[方案2] 录制被中止，提前退出")
                        return None, None
                    frames.append(stream.read(_CHUNK))

            temp_file = _temp_wav_path()
            with wave.open(str(temp_file), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(p.get_sample_size(pyaudio_patch.paInt16))
                wf.setframerate(sample_rate)
                wf.writeframes(b"".join(frames))

            logger.info("[方案2] 音频已保存到临时文件：%s", temp_file)
            return temp_file, None

    except Exception as exc:
        logger.warning("[方案2] pyaudiowpatch 录制失败：%s", exc)
        return None, str(exc)


def _record_with_pyaudio(
    duration: int, stop_event: threading.Event | None
) -> tuple[Path | None, str | None]:
    """方案 3：pyaudio 立体声混音（需 Windows 手动启用该录音设备，逐块可中断）。"""
    p = None
    try:
        import pyaudio

        logger.info("[方案3] 正在使用 pyaudio 查找立体声混音设备…")

        p = pyaudio.PyAudio()

        stereo_mix_index = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            name = str(info.get("name", ""))
            if int(info.get("maxInputChannels", 0)) > 0 and (
                "立体声混音" in name
                or "Stereo Mix" in name
                or "stereo mix" in name
            ):
                stereo_mix_index = i
                logger.info("[方案3] 找到立体声混音设备：%s", name)
                break

        if stereo_mix_index is None:
            logger.info("[方案3] 未找到立体声混音设备（可在声音控制面板启用后重试）")
            return None, "未找到立体声混音设备"

        device_info = p.get_device_info_by_index(stereo_mix_index)
        channels = min(int(device_info.get("maxInputChannels", 2)), 2)

        stream = p.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=_SAMPLE_RATE,
            input=True,
            input_device_index=stereo_mix_index,
            frames_per_buffer=_CHUNK,
        )

        frames = []
        total_chunks = int(_SAMPLE_RATE / _CHUNK * duration)
        for _ in range(total_chunks):
            if _aborted(stop_event):
                logger.info("[方案3] 录制被中止，提前退出")
                stream.stop_stream()
                stream.close()
                return None, None
            frames.append(stream.read(_CHUNK, exception_on_overflow=False))

        stream.stop_stream()
        stream.close()

        temp_file = _temp_wav_path()
        with wave.open(str(temp_file), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(pyaudio.paInt16))
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(b"".join(frames))

        logger.info("[方案3] 音频已保存到临时文件：%s", temp_file)
        return temp_file, None

    except Exception as exc:
        logger.warning("[方案3] pyaudio 录制失败：%s", exc)
        return None, str(exc)
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:  # pragma: no cover - 设备已被关闭等
                pass


def _temp_wav_path() -> Path:
    """生成唯一的临时 WAV 文件路径（系统临时目录，调用方负责删除）。"""
    fd, name = tempfile.mkstemp(prefix="renpy_song_", suffix=".wav")
    os.close(fd)
    return Path(name)
