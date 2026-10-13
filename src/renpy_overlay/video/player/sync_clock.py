"""同步时钟与音视频节拍判定（纯函数，离线单测覆盖）。

音视频同步策略：音频主时钟 + 视频节拍。

- 主时钟来自音频输出已播放样本数（callback 精确计量，见 audio_output）；
  无音轨或音频未启动时回退墙钟推算（play 基准 + 已流逝时间）。
- 视频帧节拍：帧 pts 早于主时钟 → 等待到期；落后超过丢帧阈值 → 丢弃
  追平（预解码领先渲染，丢帧不阻塞解码流水线）。
"""

from __future__ import annotations

#: 帧率回退值：流元数据缺失/非法时（与主项目 editor_window 同口径）
DEFAULT_FPS = 30.0
#: 视频帧丢帧阈值（毫秒）：帧落后主时钟超过该值则丢弃直取下一帧
DROP_LAG_MS = 40
#: 节拍提前量（毫秒）：到期前留出上传/present 的抖动余量，避免周期性掉帧
PRESENT_LEAD_MS = 10
#: 节拍等待上限（毫秒）：防止主时钟异常（音频停跳）导致渲染线程死等
MAX_PACE_WAIT_MS = 250


def stream_fps(video_stream) -> float:
    """视频流帧率：average_rate 优先、guessed_rate 兜底、非法回退 30fps。"""
    for attr in ("average_rate", "guessed_rate"):
        rate = getattr(video_stream, attr, None)
        if rate is None:
            continue
        try:
            value = float(rate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_FPS


def media_duration_ms(video_stream, container_duration_us) -> int:
    """媒体时长（毫秒）：流时基优先、容器兜底；未知返回 -1。"""
    duration = getattr(video_stream, "duration", None)
    time_base = getattr(video_stream, "time_base", None)
    if duration is not None and time_base is not None and duration > 0:
        return int(duration * float(time_base) * 1000)
    if container_duration_us is not None and container_duration_us > 0:
        return int(container_duration_us / 1000)
    return -1


def frame_time_ms(av_frame) -> int:
    """帧时间戳（毫秒）：frame.time（秒，float）换算并钳零。"""
    pts = getattr(av_frame, "time", None)
    if pts is None:
        return 0
    return max(0, int(pts * 1000))


def audio_clock_ms(played_samples: int, sample_rate: int) -> int:
    """音频主时钟（毫秒）：已播放样本数 ÷ 采样率。"""
    if sample_rate <= 0:
        return 0
    return max(0, int(played_samples * 1000 / sample_rate))


def wall_clock_ms(start_pts_ms: int, start_wall_ms: float, now_wall_ms: float) -> int:
    """墙钟回退时钟（毫秒）：起播基准 + 已流逝 wall 时间。"""
    return int(start_pts_ms + max(0.0, now_wall_ms - start_wall_ms))


def pace_wait_ms(frame_pts_ms: int, clock_ms: int) -> int:
    """当前帧距到期还差多少毫秒（≤0 即到期可上屏）。

    到期判定带提前量（帧 pts - lead ≤ 时钟），并钳到等待上限：超过上限
    说明主时钟异常停跳，渲染线程直接放行（宁可早不可卡死）。
    """
    if frame_pts_ms <= 0 and clock_ms <= 0:
        return 0
    due = frame_pts_ms - PRESENT_LEAD_MS - clock_ms
    if due <= 0:
        return 0
    return min(int(due), MAX_PACE_WAIT_MS)


def should_drop(frame_pts_ms: int, clock_ms: int, max_lag_ms: int = DROP_LAG_MS) -> bool:
    """丢帧判定：帧落后主时钟超过阈值（预解码领先时的追平策略）。"""
    return frame_pts_ms < clock_ms - max_lag_ms
