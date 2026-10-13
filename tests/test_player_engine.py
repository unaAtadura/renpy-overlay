"""真实媒体端到端冒烟（无 GUI）：验证 PyAV + 缓存源 + 索引 + 解码上下文。

测试素材：参考实验项目目录内的 mp4（h264 720p yuv420p + aac）。素材
缺失时整模块跳过（离线环境仍可跑其余纯逻辑单测）。

覆盖：
- CachedSource 经 av.open 打开（file-like 协议与 PyAV 兼容性）
- build_index 全量扫描真实容器 + sidecar 往返
- DecodeContext.open/seek 真实链路：元数据、首帧信箱、预填充、
  索引 seek 落点、音频流入环形缓冲
"""

from pathlib import Path

import pytest

from renpy_overlay.video.player.decoder import DecodeContext
from renpy_overlay.video.player.keyframe_index import build_index, load_cached, save
from renpy_overlay.video.player.queues import AudioRingBuffer, BoundedFrameQueue
from renpy_overlay.video.player.source_filter import CachedSource

ROOT = Path(__file__).resolve().parents[1]
VIDEO = next(iter((ROOT / ".reference_projects" / "videoplayer").glob("*.mp4")), None)

pytestmark = pytest.mark.skipif(VIDEO is None, reason="参考项目目录无 mp4 素材")


class RealHooks:
    """DecodeContext 回调收集器（同步驱动，无线程）。"""

    def __init__(self) -> None:
        self.display = []
        self.audio_written = []
        self.flushed = 0

    def on_display_frame(self, frame):
        self.display.append(frame)

    def on_audio_written(self, pts_ms, n):
        self.audio_written.append((pts_ms, n))

    def on_audio_flushed(self):
        self.flushed += 1


def make_ctx():
    hooks = RealHooks()
    ctx = DecodeContext(
        vqueue=BoundedFrameQueue(8),
        ring=AudioRingBuffer(32 << 20),
        convert_frame=lambda f: f,  # 直通：断言直接检查 av 帧
        on_display_frame=hooks.on_display_frame,
        on_audio_written=hooks.on_audio_written,
        on_audio_flushed=hooks.on_audio_flushed,
        clock_ms=lambda: 0.0,
    )
    return ctx, hooks


class TestRealMedia:
    def test_cached_source_opens_with_av(self):
        import av

        source = CachedSource(VIDEO)
        with av.open(source) as container:
            v = container.streams.video[0]
            assert v.codec_context.width == 1280
            assert container.duration and container.duration > 0

    def test_build_index_and_sidecar(self):
        import av

        source = CachedSource(VIDEO)
        with av.open(source) as container:
            index = build_index(container, container.streams.video[0])
        assert len(index) > 100  # 25 分钟素材：关键帧数百量级
        assert index.lookup_floor_ms(60_000).pts_units > 0
        save(VIDEO, index)
        loaded = load_cached(VIDEO)
        assert loaded is not None and len(loaded) == len(index)

    def test_open_and_seek_real(self, monkeypatch):
        # 无 SDL 消费者：放宽水位/容量，避免空等（口径同 test_decoder）
        monkeypatch.setattr(
            "renpy_overlay.video.player.decoder.AUDIO_CAPACITY_MS", 100_000
        )
        monkeypatch.setattr(
            "renpy_overlay.video.player.decoder.AUDIO_WATERMARK_MS", 100
        )
        ctx, hooks = make_ctx()
        assert ctx.open(str(VIDEO))
        info = ctx.info
        assert info.fps > 20 and info.duration_ms > 1_000_000  # ~25 分钟
        assert info.width == 1280 and info.height == 720
        assert info.keyframes > 100
        assert hooks.display and hooks.display[0].pts_ms >= 0  # 首帧信箱
        assert ctx._vqueue.depth() >= 2  # 预填充
        assert ctx._ring.level_bytes() > 0  # 音频入独立缓冲
        assert hooks.audio_written and hooks.audio_written[0][1] > 0
        # seek 到 60s：索引直跳 → 解码到首个 ≥60000ms 的帧
        target = 60_000
        ctx.seek(target)
        assert hooks.display[-1].pts_ms >= target
        assert hooks.display[-1].pts_ms - target < 100  # 目标精度：一帧内
        payload = hooks.display[-1].payload
        assert len(payload.planes) == 3  # yuv420p 三平面（直传纹理就绪）
        assert payload.planes[0].line_size > 0
        # seek 后音频从新位置续流（对齐裁剪：不再有早于目标的时序）
        assert all(pts >= target - 50 for pts, _ in hooks.audio_written[-10:])
