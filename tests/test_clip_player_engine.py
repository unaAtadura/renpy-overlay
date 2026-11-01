"""真实媒体端到端冒烟（无 GUI）：验证 PyAV + 缓存源 + 索引 + 解码上下文。

测试素材：PyAV 现场合成的 mp4（h264 yuv420p + aac，GOP 锁定每秒一个
关键帧），无外部素材依赖（离线环境仍可跑）。

覆盖：
- CachedSource 经 av.open 打开（file-like 协议与 PyAV 兼容性）
- build_index 全量扫描真实容器 + sidecar 往返
- DecodeContext.open/seek 真实链路：元数据、首帧信箱、预填充、
  索引 seek 落点、音频流入环形缓冲
"""

import time

import av
import pytest

from renpy_overlay.video.clip.player.decoder import DecodeContext
from renpy_overlay.video.clip.player.keyframe_index import build_index, load_cached, save
from renpy_overlay.video.clip.player.queues import AudioRingBuffer, BoundedFrameQueue
from renpy_overlay.video.clip.player.source_filter import CachedSource

RATE = 30
SECONDS = 8  # 加长到 8s：seek 目标 3s 附近有足够的解码余量
SAMPLE_RATE = 16_000  # aac 音轨采样率（与 ASR 截取规格一致，量级足够断言）


def _make_video(tmp_path) -> str:
    """合成 8s / 30fps / 64x64 / yuv420p + aac 音轨的 h264 视频。

    ``gop_size=RATE``：每秒一个 IDR（0/967/1967...ms，x264 在 gop_size
    前一帧放 IDR），保证索引关键帧数量确定；音频为固定电平正弦帧。
    """
    path = tmp_path / "media.mp4"
    container = av.open(str(path), "w")
    vstream = container.add_stream("libx264", rate=RATE)
    vstream.width = 64
    vstream.height = 64
    vstream.pix_fmt = "yuv420p"
    vstream.gop_size = RATE
    astream = container.add_stream("aac", rate=SAMPLE_RATE)
    astream.layout = "mono"
    samples_per_frame = SAMPLE_RATE // RATE  # 每视频帧对齐的音频样本数
    for i in range(RATE * SECONDS):
        vframe = av.VideoFrame(64, 64, "yuv420p")
        for packet in vstream.encode(vframe):
            container.mux(packet)
        # 音频帧与视频帧逐帧交替 mux（s16 mono；PyAV 19 构造不接受 rate）
        aframe = av.AudioFrame(samples=samples_per_frame, layout="mono", format="s16")
        aframe.sample_rate = SAMPLE_RATE
        aframe.pts = i * samples_per_frame
        silence = bytes(samples_per_frame * 2)  # s16 mono 静音
        for plane in aframe.planes:
            plane.update(silence)
        for packet in astream.encode(aframe):
            container.mux(packet)
    for packet in vstream.encode(None):
        container.mux(packet)
    for packet in astream.encode(None):
        container.mux(packet)
    container.close()
    return str(path)


@pytest.fixture()
def media_file(tmp_path):
    return _make_video(tmp_path)


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
    def test_cached_source_opens_with_av(self, media_file):
        source = CachedSource(media_file)
        with av.open(source) as container:
            v = container.streams.video[0]
            assert v.codec_context.width == 64
            assert container.duration and container.duration > 0

    def test_build_index_and_sidecar(self, media_file):
        source = CachedSource(media_file)
        with av.open(source) as container:
            index = build_index(container, container.streams.video[0])
        # GOP 锁定每秒一个 IDR：8s 素材关键帧数确定（首帧 + 每个 GOP 边界）
        assert len(index) >= SECONDS
        assert index.lookup_floor_ms(3_000).pts_units > 0
        save(media_file, index)
        loaded = load_cached(media_file)
        assert loaded is not None and len(loaded) == len(index)

    def test_open_and_seek_real(self, media_file, monkeypatch):
        # 无 SDL 消费者：放宽水位/容量，避免空等（口径同 test_decoder）
        monkeypatch.setattr(
            "renpy_overlay.video.clip.player.decoder.AUDIO_CAPACITY_MS", 100_000
        )
        monkeypatch.setattr(
            "renpy_overlay.video.clip.player.decoder.AUDIO_WATERMARK_MS", 100
        )
        ctx, hooks = make_ctx()
        assert ctx.open(str(media_file))
        info = ctx.info
        assert info.fps > 20 and info.duration_ms > 5_000  # ~8s 合成素材
        assert info.width == 64 and info.height == 64
        # 索引由后台线程异步构建（open 返回时 index_source='pending'、
        # index=None）：轮询等待回填完成（全量 demux 约 0.1s 量级）
        deadline = time.monotonic() + 15.0
        while (
            (ctx.index is None or len(ctx.index) < SECONDS)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert ctx.index is not None and len(ctx.index) >= SECONDS
        assert hooks.display and hooks.display[0].pts_ms >= 0  # 首帧信箱
        assert ctx._vqueue.depth() >= 2  # 预填充
        assert ctx._ring.level_bytes() > 0  # 音频入独立缓冲
        assert hooks.audio_written and hooks.audio_written[0][1] > 0
        # seek 到 3s：索引直跳 → 解码到首个 ≥3000ms 的帧
        target = 3_000
        # 基线取在 seek 之前：seek 落定后的低水位音频预填充在 seek 内同步
        # 完成（DecodeContext 为同步驱动，无后台续流线程）
        writes_before_seek = len(hooks.audio_written)
        ctx.seek(target)
        assert hooks.display[-1].pts_ms >= target
        assert hooks.display[-1].pts_ms - target < 100  # 目标精度：一帧内
        payload = hooks.display[-1].payload
        assert len(payload.planes) == 3  # yuv420p 三平面（直传纹理就绪）
        assert payload.planes[0].line_size > 0
        # seek 后音频从新位置续流（对齐裁剪：seek 起的新写入不再有早于
        # 目标的时序——旧时序 PCM 已在 seek 入口随 ring.clear 作废）
        new_writes = hooks.audio_written[writes_before_seek:]
        assert new_writes, "seek 预填充应写入新时序音频"
        assert all(pts >= target - 50 for pts, _ in new_writes)
