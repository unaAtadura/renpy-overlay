"""系统音频持续环回录音：录到 stop_event 置位为止（视频录制的音轨用）。

与 :mod:`renpy_overlay.song_recognition.recorder` 同源的设备三重回退
（soundcard 环回 / pyaudiowpatch WASAPI 环回 / pyaudio 立体声混音），可用性
标志直接复用识曲模块（单一事实来源，避免两处探测结果不一致）；区别在录制
时长语义：识曲是定时长采样，本模块持续录制直到外部停止（视频录制开始 →
停止），wav 边录边写、停止后由 ``wave.close`` 补全 RIFF 头。

线程约束：录制函数为阻塞式，必须在调用方的后台线程执行；stop_event 由
Qt 主线程置位，逐块（~23ms）检查保证快速停止响应。
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

from ..song_recognition.recorder import (
    PYAUDIO_AVAILABLE,
    PYAUDIOWPATCH_AVAILABLE,
    SOUNDCARD_AVAILABLE,
)

logger = logging.getLogger("renpy_overlay.video.audio_capture")

#: 是否存在至少一个可用的系统音频录制方案（缺失时录制降级为仅视频）
RECORD_AVAILABLE = (
    SOUNDCARD_AVAILABLE or PYAUDIOWPATCH_AVAILABLE or PYAUDIO_AVAILABLE
)

#: 逐块读取的块大小（帧）：兼顾停止响应速度与读取开销
_CHUNK = 1024
#: soundcard / pyaudio 方案的采样率；WASAPI 环回方案跟随设备默认采样率
_SAMPLE_RATE = 44100


def record_loopback(out_path: Path, stop_event: threading.Event) -> bool:
    """持续录系统音频到 ``out_path``（wav），直到 ``stop_event`` 置位。

    三案自动回退（与识曲一致：已中止后不再回退下一案）；全部失败返回
    ``False``（调用方降级为仅视频）。返回 ``True`` 表示已写入至少一块
    数据（wav 文件有效）。
    """
    if stop_event.is_set():
        logger.info("录音开始前已被停止，跳过录制")
        return False
    errors: list[str] = []

    if SOUNDCARD_AVAILABLE:
        ok, err = _loop_with_soundcard(out_path, stop_event)
        if ok:
            return True
        if stop_event.is_set():
            return False  # 正常停止路径：不回退
        if err:
            errors.append(f"方案1 (soundcard 环回): {err}")

    if PYAUDIOWPATCH_AVAILABLE:
        ok, err = _loop_with_pyaudiowpatch(out_path, stop_event)
        if ok:
            return True
        if stop_event.is_set():
            return False
        if err:
            errors.append(f"方案2 (pyaudiowpatch WASAPI 环回): {err}")

    if PYAUDIO_AVAILABLE:
        ok, err = _loop_with_pyaudio(out_path, stop_event)
        if ok:
            return True
        if stop_event.is_set():
            return False
        if err:
            errors.append(f"方案3 (pyaudio 立体声混音): {err}")

    logger.error("无法持续录制系统音频：%s", "；".join(errors))
    return False


def _loop_with_soundcard(
    out_path: Path, stop_event: threading.Event
) -> tuple[bool, str | None]:
    """方案 1：soundcard 默认扬声器环回（逐块阻塞读取，蓝牙耳机可能不支持）。"""
    try:
        import numpy as np
        import soundcard as sc

        logger.info("[方案1] 正在使用 soundcard 查找系统音频录制设备…")

        default_speaker = sc.default_speaker()
        if "蓝牙耳机" in default_speaker.name or "Bluetooth" in default_speaker.name:
            logger.info("[方案1] 检测到蓝牙耳机，soundcard 环回可能不支持")
            return False, "蓝牙耳机可能不支持 soundcard 环回"

        loopback_mic = sc.get_microphone(
            id=str(default_speaker.name), include_loopback=True
        )
        logger.info("[方案1] 环回录音持续进行（%s）", loopback_mic.name)

        wrote = False
        with loopback_mic.recorder(samplerate=_SAMPLE_RATE, channels=2) as mic:
            with wave.open(str(out_path), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(_SAMPLE_RATE)
                while not stop_event.is_set():
                    data = mic.record(numframes=_CHUNK)
                    pcm = np.clip(data * 32767, -32768, 32767).astype(np.int16)
                    wf.writeframes(pcm.tobytes())
                    wrote = True
        logger.info("[方案1] 环回录音已停止：%s", out_path)
        return wrote, None

    except Exception as exc:
        logger.warning("[方案1] soundcard 录音失败：%s", exc)
        return False, str(exc)


def _loop_with_pyaudiowpatch(
    out_path: Path, stop_event: threading.Event
) -> tuple[bool, str | None]:
    """方案 2：pyaudiowpatch WASAPI 环回（支持蓝牙耳机，逐块读取）。"""
    try:
        import pyaudiowpatch as pyaudio_patch

        logger.info("[方案2] 正在使用 pyaudiowpatch 查找 WASAPI 环回设备…")

        with pyaudio_patch.PyAudio() as p:
            try:
                wasapi_loopback = p.get_default_wasapi_loopback()
            except OSError as exc:
                logger.warning("[方案2] 未找到 WASAPI 环回设备：%s", exc)
                return False, "未找到 WASAPI 环回设备"

            sample_rate = int(wasapi_loopback["defaultSampleRate"])
            channels = int(wasapi_loopback["maxInputChannels"])
            logger.info(
                "[方案2] 使用 WASAPI 环回设备：%s（采样率 %d Hz，%d 声道）",
                wasapi_loopback["name"],
                sample_rate,
                channels,
            )

            wrote = False
            with p.open(
                format=pyaudio_patch.paInt16,
                channels=channels,
                rate=sample_rate,
                input=True,
                frames_per_buffer=_CHUNK,
                input_device_index=wasapi_loopback["index"],
            ) as stream:
                with wave.open(str(out_path), "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wrote = False
                    while not stop_event.is_set():
                        wf.writeframes(stream.read(_CHUNK))
                        wrote = True

            logger.info("[方案2] 环回录音已停止：%s", out_path)
            return wrote, None

    except Exception as exc:
        logger.warning("[方案2] pyaudiowpatch 录音失败：%s", exc)
        return False, str(exc)


def _loop_with_pyaudio(
    out_path: Path, stop_event: threading.Event
) -> tuple[bool, str | None]:
    """方案 3：pyaudio 立体声混音（需 Windows 手动启用该录音设备）。"""
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
            return False, "未找到立体声混音设备"

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

        wrote = False
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(_SAMPLE_RATE)
            wrote = False
            while not stop_event.is_set():
                wf.writeframes(stream.read(_CHUNK, exception_on_overflow=False))
                wrote = True

        stream.stop_stream()
        stream.close()
        logger.info("[方案3] 环回录音已停止：%s", out_path)
        return wrote, None

    except Exception as exc:
        logger.warning("[方案3] pyaudio 录音失败：%s", exc)
        return False, str(exc)
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:  # pragma: no cover - 设备已被关闭等
                pass
