"""av_media（PyAV 封装）的离线验证：可用性探测、截取与合并调用契约。

遵循项目离线测试约定：不触真实 FFmpeg 进程与网络——容器经 open_fn 注入
stub 验证调用契约（逐项断言）；另以 PyAV 进程内生成微型样本冒烟真实
数据通路（无外部进程、无网络）。
"""

from __future__ import annotations

import math
import types
from fractions import Fraction

from renpy_overlay.video import av_media

VIDEO = "D:/game/renpy_overlay_cache/video/tmp_video.mp4"
AUDIO = "D:/game/renpy_overlay_cache/video/tmp_audio.wav"
OUT = "D:/game/renpy_overlay_cache/video/20261011-101010.mp4"


# ---------------------------------------------------------------- 可用性探测


def test_av_available_injected_success():
    calls = []

    def _ok():
        calls.append(1)

    assert av_media.av_available(import_fn=_ok) is True
    assert calls == [1]


def test_av_available_injected_failure():
    def _boom():
        raise ImportError("no av")

    assert av_media.av_available(import_fn=_boom) is False


def test_av_available_default_returns_bool():
    """默认探测路径（真实环境 av 已安装应为 True）；注入路径不触碰全局缓存。"""
    assert isinstance(av_media.av_available(), bool)


# ---------------------------------------------------------------- 区间整理


def test_normalize_range_swaps_reversed_endpoints():
    assert av_media.normalize_range(5.0, 2.0) == (2.0, 5.0)


def test_normalize_range_keeps_normal_interval():
    assert av_media.normalize_range(2.0, 5.0) == (2.0, 5.0)


def test_normalize_range_rejects_zero_interval():
    assert av_media.normalize_range(3.0, 3.0) is None


# ---------------------------------------------------------------- stub 基建


class _StubPacket:
    def __init__(self, dts=0):
        self.dts = dts
        self.pts = dts
        self.time_base = Fraction(1, 1000)
        self.duration = 100  # 0.1s @ 1/1000 时基
        self.stream = None


class _StubVideoStream:
    pass


class _StubAudioStream:
    rate = 16000
    time_base = Fraction(1, 16000)

    class _Layout:
        nb_channels = 1
        name = "mono"

    layout = _Layout()


class _StubOutStream:
    """输出流 stub：encode 记录帧/flush 并返回空包序列。"""

    def __init__(self, spec, container):
        self.spec = spec
        self._container = container
        self.encoded = []
        self.flushed = False

    def encode(self, frame):
        if frame is None:
            self.flushed = True
            return [f"pkt-flush:{self.spec}"]
        self.encoded.append(frame)
        return [f"pkt:{self.spec}:{frame}"]


class _StubReadContainer:
    """读容器 stub：demux/decode 返回预置序列；__enter__ 语义对齐 av。"""

    def __init__(self, packets=(), video=True, audio=True):
        self.streams = types.SimpleNamespace(
            video=[_StubVideoStream()] if video else [],
            audio=[_StubAudioStream()] if audio else [],
        )
        self._packets = list(packets)
        self.seek_calls: list = []
        self.duration = 1.0

    def demux(self, stream):
        return list(self._packets)

    def decode(self, stream):
        return iter(())

    def seek(self, offset, **kwargs):
        self.seek_calls.append((offset, kwargs))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubMuxContainer:
    """写容器 stub：记录 add_stream/add_stream_from_template/mux 调用。"""

    def __init__(self):
        self.streams = types.SimpleNamespace(video=[], audio=[])
        self.added: list = []
        self.muxed: list = []
        self.closed = False

    def add_stream(self, codec=None, **kwargs):
        self.added.append(("codec", codec, kwargs))
        return _StubOutStream(codec, self)

    def add_stream_from_template(self, template, **kwargs):
        self.added.append(("template", template, kwargs))
        return _StubOutStream(template, self)

    def mux(self, packet):
        self.muxed.append(packet)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def _open_fn(routes: dict):
    """按路径后缀分发 stub 容器；记录打开序列（供断言打开参数）。"""
    opened: list = []

    def _open(path, *args, **kwargs):
        opened.append((str(path), args, kwargs))
        for suffix, factory in routes.items():
            if str(path).endswith(suffix):
                return factory()
        raise AssertionError(f"意外打开路径 {path}")

    _open.opened = opened
    return _open


# ---------------------------------------------------------------- wav 截取（契约）


def test_extract_wav_invalid_interval_skips_open():
    def _open(path, *args, **kwargs):
        raise AssertionError("区间无效时不应打开容器")

    assert (
        av_media.extract_wav(
            video_path=VIDEO, start_s=2, end_s=2, open_fn=_open
        )
        is None
    )


def test_extract_wav_without_audio_stream_returns_none():
    open_fn = _open_fn({VIDEO: lambda: _StubReadContainer(audio=False)})
    assert (
        av_media.extract_wav(
            video_path=VIDEO, start_s=1, end_s=2, open_fn=open_fn
        )
        is None
    )


def test_extract_wav_open_error_returns_none():
    def _boom(path, *args, **kwargs):
        raise OSError("cannot open")

    assert (
        av_media.extract_wav(video_path=VIDEO, start_s=1, end_s=2, open_fn=_boom)
        is None
    )


def test_extract_wav_decode_error_returns_none():
    class _Broken(_StubReadContainer):
        def decode(self, stream):
            raise ValueError("decode failed")

    open_fn = _open_fn({VIDEO: lambda: _Broken()})
    assert (
        av_media.extract_wav(video_path=VIDEO, start_s=1, end_s=2, open_fn=open_fn)
        is None
    )


def test_extract_wav_seeks_to_start_by_stream_timebase():
    """seek 偏移 = 起点秒 / 音频流时基（流内 seek），backward 允许落在关键帧。"""
    container = _StubReadContainer()
    open_fn = _open_fn({VIDEO: lambda: container, ".wav": _StubMuxContainer})
    av_media.extract_wav(
        video_path=VIDEO, start_s=4.0, end_s=1.0, open_fn=open_fn
    )  # 终点早于起点：交换后 start=1.0
    (offset, kwargs), = container.seek_calls
    assert 1.0 - 1e-6 < offset * container.streams.audio[0].time_base < 1.0 + 1e-6
    assert kwargs.get("backward") is True


# ---------------------------------------------------------------- wav 截取（真实链路冒烟）


def _make_tone_wav(path, *, seconds=1.0, rate=44100):
    """进程内生成 440Hz 立体声 wav（PyAV 编码，无外部进程/网络）。"""
    import av

    with av.open(str(path), "w", format="wav") as container:
        stream = container.add_stream("pcm_s16le", rate=rate, layout="stereo")
        total, chunk, t = int(seconds * rate), 1024, 0
        while t < total:
            n = min(chunk, total - t)
            frame = av.AudioFrame(format="s16", layout="stereo", samples=n)
            frame.sample_rate = rate
            frame.pts = t
            data = bytearray()
            for i in range(n):
                v = int(8000 * math.sin(2 * math.pi * 440 * (t + i) / rate))
                data += v.to_bytes(2, "little", signed=True) * 2
            frame.planes[0].update(bytes(data))
            for packet in stream.encode(frame):
                container.mux(packet)
            t += n
        for packet in stream.encode(None):
            container.mux(packet)


def test_extract_wav_real_pipeline_returns_wav_bytes(tmp_path):
    """真实数据通路：4s 立体声源 → 截取 [1, 2) → 16k 单声道 wav 字节。"""
    src = tmp_path / "src.wav"
    _make_tone_wav(src, seconds=4.0)
    data = av_media.extract_wav(video_path=str(src), start_s=1.0, end_s=2.0)
    assert data is not None
    assert data[:4] == b"RIFF", "输出应为合法 wav 容器"
    # 16k 单声道 s16：约 1s 数据 + 44 字节头（重采样帧粒度允许少量溢出）
    payload = len(data) - 44
    assert 16000 * 2 <= payload <= 16400 * 2


def test_extract_wav_real_pipeline_empty_range_returns_none(tmp_path):
    """区间超出音频末尾：无样本输出 → 按失败返回 None（空输出语义）。"""
    src = tmp_path / "src.wav"
    _make_tone_wav(src, seconds=1.0)
    assert av_media.extract_wav(video_path=str(src), start_s=5.0, end_s=6.0) is None


# ---------------------------------------------------------------- 双流合并（契约）


def test_merge_streams_dual_stream_call_contract():
    """双流 = 视频流 template copy + aac 编码；dts 为 None 的占位包跳过。"""
    packets = [_StubPacket(dts=1), _StubPacket(dts=None), _StubPacket(dts=2)]
    video_src = _StubReadContainer(packets=packets)
    audio_src = _StubReadContainer(video=False)
    muxer = _StubMuxContainer()
    open_fn = _open_fn(
        {VIDEO: lambda: video_src, AUDIO: lambda: audio_src, OUT: lambda: muxer}
    )
    assert av_media.merge_streams(
        video_path=VIDEO, audio_path=AUDIO, out_path=OUT, open_fn=open_fn
    ) is True
    opened = [path for path, _, _ in open_fn.opened]
    assert opened == [VIDEO, OUT, AUDIO], "打开顺序：视频源 → 输出 → 音频源"
    assert muxer.added[0] == (
        "template",
        video_src.streams.video[0],
        {},
    ), "视频流按模板 copy（add_stream_from_template）"
    assert muxer.added[1][:2] == ("codec", "aac")
    assert muxer.added[1][2].get("rate") == 16000
    assert muxer.muxed[:2] == packets[0::2], "有效包重挂输出流后 mux"
    assert muxer.muxed[2] == "pkt-flush:aac", "音频编码器 flush 尾包入容器"
    assert len(muxer.muxed) == 3, "dts=None 占位包不入容器"


def test_merge_streams_remux_without_audio(tmp_path):
    """仅视频降级：audio_path=None 退化为单流 remux（不打开音频源）。"""
    packets = [_StubPacket(dts=1)]
    video_src = _StubReadContainer(packets=packets)
    muxer = _StubMuxContainer()

    open_fn = _open_fn({VIDEO: lambda: video_src, OUT: lambda: muxer})
    assert av_media.merge_streams(
        video_path=VIDEO, audio_path=None, out_path=OUT, open_fn=open_fn
    ) is True
    opened = [path for path, _, _ in open_fn.opened]
    assert opened == [VIDEO, OUT]
    assert muxer.added[0][1] is video_src.streams.video[0]


def test_merge_streams_open_error_returns_false():
    def _boom(path, *args, **kwargs):
        raise OSError("cannot open")

    assert (
        av_media.merge_streams(
            video_path=VIDEO, audio_path=AUDIO, out_path=OUT, open_fn=_boom
        )
        is False
    )


# ---------------------------------------------------------------- 双流合并（真实链路冒烟）


def _make_video_mkv(path, *, seconds=2.0, fps=10):
    """进程内生成纯视频 mkv 中间容器（64x64 黑帧，PyAV 编码）。"""
    import av

    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.pix_fmt = "yuv420p"
        for i in range(int(seconds * fps)):
            frame = av.VideoFrame(64, 64, "yuv420p")
            frame.pts = i
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def test_merge_streams_real_dual_stream(tmp_path):
    """真实合并：视频 mkv + 4s wav → mp4（视频 copy + aac），-shortest 截断。"""
    video = tmp_path / "20261011-101010.tmp.mkv"
    audio = tmp_path / "20261011-101010.tmp.wav"
    out = tmp_path / "20261011-101010.mp4"
    _make_video_mkv(video, seconds=2.0)
    _make_tone_wav(audio, seconds=4.0)
    assert av_media.merge_streams(
        video_path=video, audio_path=audio, out_path=out
    ) is True
    import av

    with av.open(str(out)) as container:
        assert len(container.streams.video) == 1
        assert len(container.streams.audio) == 1
        assert container.streams.audio[0].codec_context.name == "aac"
        # -shortest：音频以视频包终点（2.0s）为上限截断（aac/重采样
        # 帧粒度允许少量溢出；mp4 流级 duration 记录略小于终点）
        v_dur = float(container.streams.video[0].duration * container.streams.video[0].time_base)
        a_dur = float(container.streams.audio[0].duration * container.streams.audio[0].time_base)
        assert 1.5 <= v_dur <= 2.5
        assert a_dur <= 2.2


def test_merge_streams_real_video_only_remux(tmp_path):
    """仅视频降级：mkv → mp4 单流 remux，无音频流。"""
    import av

    video = tmp_path / "20261011-101010.tmp.mkv"
    out = tmp_path / "20261011-101010.mp4"
    _make_video_mkv(video, seconds=1.0)
    assert av_media.merge_streams(
        video_path=video, audio_path=None, out_path=out
    ) is True
    with av.open(str(out)) as container:
        assert len(container.streams.video) == 1
        assert len(container.streams.audio) == 0
