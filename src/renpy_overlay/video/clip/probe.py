"""媒体元数据轻量探测：不启 worker/SDL，直接开容器读信息。

供剪辑窗口在载入视频时获取时长/帧率（时间轴换算、帧步进口径），
失败返回 ``None`` 不抛异常（调用方降级：时间轴不可用、按 30fps 兜底）。
元数据结构复用 :mod:`.player.decoder` 的 MediaInfo（与引擎打开回报
同构，消费方无需区分来源）；时长/帧率换算与解码路径同口径
（:mod:`.player.sync_clock` 纯函数），保证「探测值」与「播放回报值」
一致。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("renpy_overlay.video.clip.probe")


def probe_media_info(path: str):
    """读取媒体元数据（无引擎轻量探测；失败返回 ``None`` 不抛异常）。

    时长/帧率换算与引擎解码路径同口径（:mod:`.player.sync_clock` 纯函数），
    保证「探测值」与「播放回报值」一致，时间轴换算可直接采用。
    """
    from .player.decoder import MediaInfo
    from .player.sync_clock import media_duration_ms, stream_fps

    try:
        import av

        with av.open(str(path)) as container:
            video_streams = container.streams.video
            if not video_streams:
                logger.info("无视频流，忽略探测：%s", path)
                return None
            video = video_streams[0]
            return MediaInfo(
                path=str(path),
                fps=stream_fps(video),
                duration_ms=media_duration_ms(video, container.duration),
                has_audio=bool(container.streams.audio),
                width=video.codec_context.width,
                height=video.codec_context.height,
                keyframes=0,  # 轻量探测不扫关键帧索引；索引统计以引擎回报为准
                index_source="probe",
            )
    except Exception:
        logger.warning("媒体探测失败：%s", path, exc_info=True)
        return None
