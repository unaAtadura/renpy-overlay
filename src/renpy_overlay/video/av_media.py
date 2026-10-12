"""音视频封装与截取：PyAV（进程内 FFmpeg 库）实现，替代外置 ffmpeg.exe。

选型（需求确认：不再依赖用户自备的 ffmpeg.exe，PyPI 的 av wheels 自带
FFmpeg 动态库，进程内调用、无子进程、无控制台窗口）：

- 可用性经 :func:`av_available` 惰性 import 探测（替代原 find_ffmpeg 的
  程序目录/PATH 探测）；缺失时录制/语音识别降级提示，不影响主程序；
- wav 截取：解码 + 重采样 16k 单声道 s16 → 内存 wav（BytesIO），失败/空
  输出返回 ``None``（与原 subprocess 版返回值语义一致）；
- 双流合并：视频流 packet copy + aac 音轨 → mp4；仅视频时退化为单流
  remux（mkv 中间容器 → mp4）；原 ``-shortest`` 语义 = 音频以视频流时长
  为上限截断（截短不补齐）；
- 录屏（gdigrab，libavdevice）见 :mod:`.screen_capture`：官方 wheels 自
  av 19 系实测已包含 libavdevice，输入/编码参数与原命令行逐项对应。

为什么截取终点按重采样输出样本数推进而非时间戳：``-ss`` 作为输入选项
重置时间戳后 ``-to`` 的参考点存在版本歧义，样本数计时语义唯一（与原
``-t`` 时长语义对应）。
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger("renpy_overlay.video.av_media")

#: 录制帧率：gdigrab 实时抓屏的稳妥值（过高易丢帧）
DEFAULT_RECORD_FPS = 30
#: 音轨截取输出规格：16k 单声道 s16le（ASR 够用且体积最小，~32KB/s）
ASR_SAMPLE_RATE = 16000
ASR_CHANNELS = 1


def _import_av() -> None:
    """探测 PyAV 可导入性（集中一处，便于测试注入）。"""
    import av  # noqa: F401 - 仅探测，不使用


_AV_AVAILABLE: bool | None = None


def av_available(import_fn=_import_av) -> bool:
    """PyAV 是否可用（惰性 import 探测并缓存；诊断与降级判断用）。

    缺失时缓存 ``False``（import 结果进程内不变，无需重试）。
    ``import_fn`` 仅供离线测试注入（注入路径不触碰全局缓存）。
    """
    global _AV_AVAILABLE
    if import_fn is not _import_av:
        try:
            import_fn()
            return True
        except ImportError:
            return False
    if _AV_AVAILABLE is None:
        try:
            import_fn()
            _AV_AVAILABLE = True
        except ImportError:
            _AV_AVAILABLE = False
            logger.warning("PyAV 不可用（import 失败），视频功能降级")
    return _AV_AVAILABLE


def normalize_range(start_s: float, end_s: float) -> tuple[float, float] | None:
    """整理截取区间：终点早于起点自动交换（需求）；间隔为 0 视为无效。

    返回 ``(start, end)``（start < end）或 ``None``（调用方不发起请求）。
    """
    start_s, end_s = float(start_s), float(end_s)
    if start_s > end_s:
        start_s, end_s = end_s, start_s
    if end_s - start_s <= 0:
        return None
    return start_s, end_s


def extract_wav(
    *,
    video_path: str | Path,
    start_s: float,
    end_s: float,
    open_fn=None,
) -> bytes | None:
    """截取 ``[start_s, end_s]`` 音频轨为 16k 单声道 wav 字节；失败/空返回 ``None``。

    ``open_fn`` 仅供离线测试注入（默认 :func:`av.open`）。区间终点早于起点
    自动交换（需求）。seek 只到关键帧精度，起点经 pts 裁剪对齐；终点按
    重采样输出样本数推进（对应原 ``-ss/-t`` 时长语义）。
    """
    import av

    rng = normalize_range(start_s, end_s)
    if rng is None:
        logger.warning("音频截取区间无效（start=%s，end=%s）", start_s, end_s)
        return None
    start, duration = rng[0], rng[1] - rng[0]
    if open_fn is None:
        open_fn = av.open
    try:
        buf = io.BytesIO()
        with open_fn(str(video_path)) as src:
            if not src.streams.audio:
                logger.error("音频截取失败（无音频流）：%s", video_path)
                return None
            audio = src.streams.audio[0]
            # seek 只到关键帧：起点按流时基换算，解码循环内 pts 裁剪对齐
            src.seek(int(start / audio.time_base), stream=audio, backward=True)
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=ASR_SAMPLE_RATE
            )
            with open_fn(buf, "w", format="wav") as dst:
                # av 19 的 add_stream 不再接受 layout 参数，须在 mux 前置属性
                out = dst.add_stream("pcm_s16le", rate=ASR_SAMPLE_RATE)
                out.layout = "mono"
                produced = 0
                limit = int(duration * ASR_SAMPLE_RATE)
                for frame in src.decode(audio):
                    if frame.time is not None and frame.time < start:
                        continue  # 关键帧提前量：丢弃起点之前的部分
                    for piece in resampler.resample(frame):
                        produced += piece.samples
                        for packet in out.encode(piece):
                            dst.mux(packet)
                    if produced >= limit:
                        break
                for packet in out.encode(None):  # flush 尾包（PCM 为空迭代）
                    dst.mux(packet)
        data = buf.getvalue()
        if not data:
            logger.error("音频截取失败（空输出）：%s", video_path)
            return None
        logger.info(
            "音频截取完成：%s [%s, %s] -> %d 字节", video_path, start_s, end_s, len(data)
        )
        return data
    except Exception as exc:  # 解码/封装/IO 异常统一收敛为失败（行为同原版）
        logger.error("音频截取失败（%s）：%s", video_path, exc)
        return None


def _native_layout(layout) -> str:
    """归一化音频布局为 FFmpeg native 布局名。

    wav 等容器读回的布局常为非命名形式（如 ``'2 channels'``，容器头未携带
    channel mask），而 FFmpeg 8 系的 aac 等编码器只接受 native 命名布局
    （否则 ``avcodec_open2`` 返回 EINVAL）——按声道数映射，录制场景的
    mono/stereo 均在表内。
    """
    native = _NATIVE_LAYOUTS.get(int(layout.nb_channels))
    return native if native is not None else str(layout.name)


#: 声道数 → FFmpeg native 布局名（av_channel_layout_describe 对非命名布局
#: 只返回 'N channels' 描述串，无法直接作编码器布局）
_NATIVE_LAYOUTS = {
    1: "mono",
    2: "stereo",
    3: "2.1",
    4: "4.0",
    5: "5.0",
    6: "5.1",
    7: "6.1",
    8: "7.1",
}


def merge_streams(
    *,
    video_path: str | Path,
    audio_path: str | Path | None,
    out_path: str | Path,
    open_fn=None,
) -> bool:
    """停止阶段封装执行（双流合并或仅视频 remux）；失败返回 ``False``。

    ``audio_path`` 为 None 时退化为单流 remux（mkv 中间容器 → mp4，流 copy
    秒级完成）；双流 = 视频流 packet copy + aac 音轨，音频以视频包实际
    终点为上限（对应原 ``-shortest``：截短不补齐；mkv 容器级 duration
    不可靠，逐包取终点）。失败时不清理半成品输出文件（与原实现一致，
    由调用方回退保留 mkv 中间容器）。
    ``open_fn`` 仅供离线测试注入。
    """
    from fractions import Fraction

    import av

    if open_fn is None:
        open_fn = av.open
    try:
        with open_fn(str(video_path)) as src_video:
            with open_fn(str(out_path), "w") as dst:
                v_in = src_video.streams.video[0]
                v_out = dst.add_stream_from_template(v_in)
                # 音频流须在首个包 mux（触发 header 写入）前添加并配置；
                # 容器生命周期手动管理（早于视频 mux 打开，finally 统一关闭）
                src_audio = src_audio_cm = None
                a_in = resampler = a_out = None
                try:
                    if audio_path is not None:
                        src_audio_cm = open_fn(str(audio_path))
                        src_audio = src_audio_cm.__enter__()
                        a_in = src_audio.streams.audio[0]
                        # 布局归一化后配置编码器与重采样（见 _native_layout）
                        layout = _native_layout(a_in.layout)
                        a_out = dst.add_stream("aac", rate=a_in.rate)
                        a_out.layout = layout  # mux 前置属性（av 19 API）
                        # 流时基须显式给定，否则 PyAV 的 mux rebase 遇
                        # 0/1 时基报错（Cannot rebase to zero time）
                        a_out.time_base = Fraction(1, a_in.rate)
                        resampler = av.AudioResampler(
                            format="fltp", layout=layout, rate=a_in.rate
                        )
                    video_end_s = 0.0
                    base_pts = None
                    for packet in src_video.demux(v_in):
                        if packet.dts is None:
                            continue  # flush 占位包不写入
                        # 时间戳归零：gdigrab 用系统启动起的墙钟 pts，与音频流
                        # （零起点）不同空间，交错 mux 会 EINVAL；原 ffmpeg 的
                        # mov muxer 默认同样把起始 dts 平移到零
                        if base_pts is None:
                            base_pts = packet.pts
                        if packet.pts is not None:
                            packet.pts -= base_pts
                        packet.dts -= base_pts
                        if packet.pts is not None:
                            end_s = float(packet.pts * packet.time_base) + (
                                float(packet.duration * packet.time_base)
                                if packet.duration
                                else 0.0
                            )
                            video_end_s = max(video_end_s, end_s)
                        packet.stream = v_out
                        dst.mux(packet)
                    if a_out is not None:
                        for frame in src_audio.decode(a_in):
                            if video_end_s and frame.time >= video_end_s:
                                break  # -shortest：视频流结束时截断
                            for piece in resampler.resample(frame):
                                for p in a_out.encode(piece):
                                    dst.mux(p)
                        for p in a_out.encode(None):  # flush 尾包
                            dst.mux(p)
                finally:
                    if src_audio_cm is not None:
                        src_audio_cm.__exit__(None, None, None)
        logger.info("双流合并完成：%s", out_path)
        return True
    except Exception as exc:  # 解码/编码/IO 异常统一收敛为失败（行为同原版）
        logger.error("双流合并失败（%s）：%s", out_path, exc)
        return False
