"""decoder 逻辑单测：假容器/假帧同步驱动（无 av、无线程、无 SDL）。

覆盖：SeekLatch 最新胜出、open 预填充 + 首帧信箱、索引 seek 定位、
seek 清陈帧（abort/gen）、音频流入环形缓冲 + 时钟登记、seek 后早于
目标的音频丢弃、EOF 哨兵。
"""

from fractions import Fraction

import pytest

from renpy_overlay.video.player import decoder as dec
from renpy_overlay.video.player.decoder import DecodeContext, SeekLatch
from renpy_overlay.video.player.keyframe_index import KeyframeEntry, KeyframeIndex
from renpy_overlay.video.player.queues import AudioRingBuffer, BoundedFrameQueue

# ---------------------------------------------------------------- 假 av 对象

class FakePlane:
    def __init__(self, data: bytes):
        self.buffer_ptr = 0
        self.buffer_size = len(data)
        self.line_size = len(data)
        self.data = data


class FakeChunk:
    def __init__(self, data: bytes):
        self.planes = [FakePlane(data)]


class FakeFrame:
    def __init__(self, time_s: float):
        self.time = time_s


class FakeAudioFrame(FakeFrame):
    pass


class FakePacket:
    def __init__(self, stream, decode_frames, dts=0, pts=None, pos=0, is_keyframe=False):
        self.stream = stream
        self.decode_frames = decode_frames
        self.dts = dts
        self.pts = pts if pts is not None else dts
        self.pos = pos
        self.is_keyframe = is_keyframe

    def decode(self):
        return self.decode_frames


class FakeVideoStream:
    time_base = Fraction(1, 1000)  # 1 unit = 1ms

    class _Codec:
        width = 64
        height = 48

    codec_context = _Codec()
    average_rate = Fraction(24, 1)


class FakeAudioStream:
    time_base = Fraction(1, 48000)


class FakeContainer:
    """共享包流：demux(*streams) 返回同一序列（真实容器语义）。"""

    duration = 10_000_000  # 10s（微秒）

    def __init__(self, packets, video_stream, audio_stream):
        self._packets = packets
        self._video = video_stream
        self.seek_calls = []
        self._pos_index = 0  # demux 位置：seek(ts) 后指向首个 pts ≥ ts 的视频包
        audio = [audio_stream] if audio_stream is not None else []
        self.streams = type(
            "Streams", (), {"video": [video_stream], "audio": audio}
        )()

    def demux(self, *streams):
        wanted = set(id(s) for s in streams)
        return iter([p for p in self._packets[self._pos_index :] if id(p.stream) in wanted])

    def seek(self, ts, stream=None, backward=False, any_frame=False):
        self.seek_calls.append((ts, stream, backward))
        # 模拟 demuxer 定位：从首个 pts ≥ ts 的视频包继续
        for i, p in enumerate(self._packets):
            if p.stream is self._video and p.pts >= ts:
                self._pos_index = i
                return


def make_engine(video_ms, audio_ms=None, audio_chunk=b"\x00" * 96_000):
    """构造 (ctx, container, records)；包时序：视频帧每 40ms 一帧。"""
    video = FakeVideoStream()
    audio = FakeAudioStream() if audio_ms is not None else None
    # 按 pts 交错排列（真实容器 demux 语义：音视频交织）
    vp = [FakePacket(video, [FakeFrame(ms / 1000.0)], dts=ms, pos=100 + i,
                     is_keyframe=(i % 5 == 0))
          for i, ms in enumerate(video_ms)]
    ap = [FakePacket(audio, [FakeAudioFrame(ms / 1000.0)], dts=ms)
          for ms in (audio_ms or [])]
    packets = sorted(vp + ap, key=lambda p: p.dts)
    container = FakeContainer(packets, video, audio)
    records = {"display": [], "audio_written": [], "flushed": 0, "now": 0.0}

    def clock_ms():
        records["now"] += 0.1
        return records["now"]

    def resampler_factory():
        class R:
            def resample(self, frame):
                return [FakeChunk(audio_chunk)]

        return R()

    vqueue = BoundedFrameQueue(8)
    ring = AudioRingBuffer(32 << 20)  # 32MB：容纳全部假音频（测试无消费者）
    ctx = DecodeContext(
        vqueue=vqueue,
        ring=ring,
        open_fn=lambda path: container,
        resampler_factory=resampler_factory,
        convert_frame=lambda f: f,
        on_display_frame=records["display"].append,
        on_audio_written=lambda pts, n: records["audio_written"].append((pts, n)),
        on_audio_flushed=lambda: records.__setitem__("flushed", records["flushed"] + 1),
        clock_ms=clock_ms,
    )
    return ctx, container, records, vqueue, ring


@pytest.fixture()
def media_file(tmp_path):
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"\x00" * 4096)
    return path


class TestSeekLatch:
    def test_latest_wins(self):
        latch = SeekLatch()
        latch.request(100)
        latch.request(500)
        latch.request(200)
        assert latch.pop() == 200
        assert latch.pop() is None  # 一次消费即清空

    def test_clamp_negative(self):
        latch = SeekLatch()
        latch.request(-5)
        assert latch.pop() == 0


class TestOpen:
    def test_open_prefills_and_reports_first_frame(self, media_file, monkeypatch):
        index = KeyframeIndex([KeyframeEntry(0, 100), KeyframeEntry(2000, 300)], 0.001)
        monkeypatch.setattr(dec, "load_cached", lambda p: index)
        monkeypatch.setattr(dec, "save", lambda p, i: True)
        ctx, _, records, vqueue, _ = make_engine([0, 40, 80, 120, 160])
        assert ctx.open(str(media_file))
        assert ctx.info.fps == 24.0
        assert ctx.info.duration_ms == 10_000
        assert ctx.info.index_source == "cache"
        assert records["display"] and records["display"][0].pts_ms == 0
        assert vqueue.depth() >= 2  # 预填充到位

    def test_open_without_audio(self, media_file, monkeypatch):
        monkeypatch.setattr(dec, "load_cached", lambda p: None)
        monkeypatch.setattr(
            dec, "build_index", lambda c, s: KeyframeIndex([KeyframeEntry(0, 0)], 0.001)
        )
        monkeypatch.setattr(dec, "save", lambda p, i: True)
        ctx, _, records, _, _ = make_engine([0, 40, 80], audio_ms=None)
        ctx.open(str(media_file))
        assert ctx.info.has_audio is False
        assert records["display"][0].pts_ms == 0


class TestSeek:
    @pytest.fixture(autouse=True)
    def _no_audio_block(self, monkeypatch):
        # 测试无 SDL callback 消费：放宽容量上限避免空等；水印调低让
        # prefill 尽快达标（单帧 500ms chunk 即满足）
        monkeypatch.setattr(dec, "AUDIO_CAPACITY_MS", 100_000)
        monkeypatch.setattr(dec, "AUDIO_WATERMARK_MS", 100)

    def _opened(self, monkeypatch, media_file):
        index = KeyframeIndex(
            [KeyframeEntry(0, 100), KeyframeEntry(2000, 300), KeyframeEntry(4000, 500)],
            0.001,
        )
        monkeypatch.setattr(dec, "load_cached", lambda p: index)
        monkeypatch.setattr(dec, "save", lambda p, i: True)
        video_ms = list(range(0, 4800, 40))
        audio_ms = list(range(0, 4800, 20))
        ctx, container, records, vqueue, ring = make_engine(video_ms, audio_ms)
        ctx.open(str(media_file))
        return ctx, container, records, vqueue, ring

    def test_seek_via_index_lands_keyframe(self, media_file, monkeypatch):
        ctx, container, records, vqueue, ring = self._opened(monkeypatch, media_file)
        gen_before = ctx.gen
        ctx.seek(2300)
        # 索引 floor(2300) = 2000 → 按流时基直跳关键帧（backward=True floor 语义）
        video_stream = container.streams.video[0]
        assert container.seek_calls[-1][0] == 2000
        assert container.seek_calls[-1][1] is video_stream
        assert container.seek_calls[-1][2] is True
        assert ctx.gen == gen_before + 1  # 代数号递增（渲染侧丢陈帧）
        assert records["display"][-1].pts_ms == 2320  # 解码到首个 ≥2300 的帧
        assert vqueue.depth() >= 1  # 重新预填充

    def test_seek_clears_stale_frames(self, media_file, monkeypatch):
        ctx, _, records, vqueue, _ = self._opened(monkeypatch, media_file)
        stale_before = vqueue.depth()
        assert stale_before >= 2
        ctx.seek(2400)
        # abort 清空 + 新批次重填：队列内全部为新 gen
        gens = {f.gen for f in vqueue._items}
        assert gens == {ctx.gen}
        assert records["flushed"] >= 1  # 音频环同步清空

    def test_seek_drops_early_audio(self, media_file, monkeypatch):
        ctx, _, records, _, ring = self._opened(monkeypatch, media_file)
        records["audio_written"].clear()
        ctx.seek(3000)
        assert all(pts >= 3000 - 30 - 20 for pts, _ in records["audio_written"])
        assert records["audio_written"]  # 目标之后的音频正常流入

    def test_audio_flows_to_ring(self, media_file, monkeypatch):
        ctx, _, records, _, ring = self._opened(monkeypatch, media_file)
        assert ring.level_bytes() > 0
        assert records["audio_written"][0][0] == 0  # 首个音频帧 pts
        assert records["audio_written"][0][1] > 0


class TestWorkerHooks:
    def test_do_open_filters_worker_level_hooks(self, media_file, monkeypatch):
        """worker 级回调（on_open_failed/on_opened）不得传入 DecodeContext。"""
        monkeypatch.setattr(
            dec, "load_cached", lambda p: KeyframeIndex([KeyframeEntry(0, 0)], 0.001)
        )
        video = FakeVideoStream()
        packets = [
            FakePacket(video, [FakeFrame(ms / 1000.0)], dts=ms, pos=i, is_keyframe=True)
            for i, ms in enumerate((0, 40, 80))
        ]
        container = FakeContainer(packets, video, None)
        opened = []
        worker = dec.DecoderWorker(
            vqueue=BoundedFrameQueue(8),
            ring=AudioRingBuffer(4096),
            open_fn=lambda p: container,
            convert_frame=lambda f: f,
            on_display_frame=lambda f: None,
            on_audio_flushed=lambda: None,
            on_open_failed=lambda msg: opened.append(("failed", msg)),
            on_opened=lambda info: opened.append(("opened", info)),
            clock_ms=lambda: 0.0,
        )
        worker._do_open(str(media_file))  # 不启线程，同步驱动
        assert worker.ctx is not None and worker.ctx.info.duration_ms == 10_000
        assert [kind for kind, _ in opened] == ["opened"]


class TestPlayback:
    def test_step_and_eof_sentinel(self, media_file, monkeypatch):
        monkeypatch.setattr(dec, "load_cached", lambda p: None)
        monkeypatch.setattr(
            dec, "build_index", lambda c, s: KeyframeIndex([KeyframeEntry(0, 0)], 0.001)
        )
        monkeypatch.setattr(dec, "save", lambda p, i: True)
        ctx, _, _, vqueue, _ = make_engine([0, 40, 80], audio_ms=None)
        ctx.open(str(media_file))
        while vqueue.depth():
            vqueue.get(timeout_s=0.01)
        # 首帧已由 open 的 _decode_until(0) 消费投递信箱；余帧预填后即 EOF
        assert not ctx.step()
        assert ctx.eof
        tail = vqueue.get(timeout_s=0.01)
        assert tail is not None and tail.pts_ms == -1  # 哨兵帧

    def test_convert_hook_applied(self, media_file, monkeypatch):
        monkeypatch.setattr(dec, "load_cached", lambda p: None)
        monkeypatch.setattr(
            dec, "build_index", lambda c, s: KeyframeIndex([KeyframeEntry(0, 0)], 0.001)
        )
        monkeypatch.setattr(dec, "save", lambda p, i: True)
        marker = object()
        ctx, _, records, vqueue, _ = make_engine([0, 40, 80], audio_ms=None)
        ctx._convert = lambda f: marker  # 转换器注入：队列载荷经转换
        ctx.open(str(media_file))
        frame = vqueue.get(timeout_s=0.01)
        assert frame.payload is marker
