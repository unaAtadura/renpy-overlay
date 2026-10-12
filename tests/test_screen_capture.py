"""录屏编码线程（screen_capture）的离线验证：gdigrab 参数契约与协作停止。

不触真实 gdigrab 设备：输入/输出容器经 open_fn 注入 stub，仅验证调用
契约（打开参数逐项断言、停止事件置位即退出、flush 正常 finalize）。
"""

from __future__ import annotations

import types

from renpy_overlay.video.screen_capture import CAPTURE_AVAILABLE, record_screen

OUT = "D:/game/renpy_overlay_cache/video/20261011-101010.tmp.mkv"


class _StubVideoStream:
    pass


class _StubInContainer:
    """gdigrab 输入容器 stub：decode 逐帧检查停止事件（模拟实时流）。"""

    def __init__(self, stop_event, max_frames=3):
        self._stop = stop_event
        self._max = max_frames
        self.streams = types.SimpleNamespace(video=[_StubVideoStream()])

    def decode(self, stream):
        for i in range(self._max):
            if self._stop.is_set():
                return
            yield f"frame-{i}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubEncoder:
    def __init__(self, codec, kwargs):
        self.codec = codec
        self.kwargs = kwargs
        self.width = None
        self.height = None
        self.pix_fmt = None
        self.options = None
        self.encoded = []
        self.flushed = False

    def encode(self, frame):
        if frame is None:
            self.flushed = True
            return ["pkt-flush"]
        self.encoded.append(frame)
        return [f"pkt:{frame}"]


class _StubOutContainer:
    def __init__(self):
        self.added: list = []
        self.muxed: list = []

    def add_stream(self, codec=None, **kwargs):
        encoder = _StubEncoder(codec, kwargs)
        self.added.append(encoder)
        return encoder

    def mux(self, packet):
        self.muxed.append(packet)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_capture_available_is_callable_probe():
    """CAPTURE_AVAILABLE 是惰性探测函数（recorder 构造默认值直接引用）。"""
    assert callable(CAPTURE_AVAILABLE)
    assert isinstance(CAPTURE_AVAILABLE(), bool)


def test_record_screen_opens_gdigrab_with_options():
    """打开参数与原 ffmpeg 命令行逐项对应（framerate/offset/video_size）。"""
    opened: list = []

    def open_fn(path, *args, **kwargs):
        opened.append((path, args, kwargs))
        if "desktop" in str(path):
            return _StubInContainer(types.SimpleNamespace(is_set=lambda: True))
        return _StubOutContainer()

    assert record_screen(
        OUT,
        types.SimpleNamespace(is_set=lambda: False),
        offset_x=-1920,
        offset_y=0,
        width=1920,
        height=1080,
        fps=30,
        open_fn=open_fn,
    ) is True
    (src_call, out_call) = opened
    assert src_call[0] == "desktop"
    assert src_call[1] == ()
    assert src_call[2] == {
        "format": "gdigrab",
        "options": {
            "framerate": "30",
            "offset_x": "-1920",
            "offset_y": "0",
            "video_size": "1920x1080",
        },
    }
    assert out_call[0] == OUT and out_call[1] == ("w",)


def test_record_screen_encodes_libx264_ultrafast(tmp_path):
    """编码器配置：libx264 + yuv420p + ultrafast（与原命令行一致）。"""
    stop = types.SimpleNamespace(is_set=lambda: False)
    dst = _StubOutContainer()

    def open_fn(path, *args, **kwargs):
        return _StubInContainer(stop, max_frames=3) if "desktop" in str(path) else dst

    assert record_screen(
        OUT, stop, offset_x=0, offset_y=0, width=800, height=600, open_fn=open_fn
    ) is True
    assert len(dst.added) == 1
    encoder = dst.added[0]
    assert encoder.codec == "libx264"
    assert encoder.width == 800 and encoder.height == 600, (
        "编码器尺寸须显式取传入几何（PyAV 19 预置 640x480 默认值不随帧更新）"
    )
    assert encoder.pix_fmt == "yuv420p"
    assert encoder.options == {"preset": "ultrafast"}
    assert encoder.kwargs.get("rate") == 30, "默认帧率取 DEFAULT_RECORD_FPS"
    assert encoder.encoded == ["frame-0", "frame-1", "frame-2"]
    assert encoder.flushed is True, "正常结束须 flush 尾包"
    assert dst.muxed[-1] == "pkt-flush"


def test_record_screen_stops_on_event_mid_stream():
    """停止事件置位：当前帧编码完即退出（协作式，无强杀概念）。"""
    stop = types.SimpleNamespace(is_set=lambda: False)
    dst = _StubOutContainer()

    def open_fn(path, *args, **kwargs):
        return _StubInContainer(stop, max_frames=10) if "desktop" in str(path) else dst

    # 第 3 次停止检查置位（frame-0 编码完，取下一帧前退出）
    state = {"count": 0}

    def flip_on_third_check():
        state["count"] += 1
        return state["count"] > 2

    stop.is_set = flip_on_third_check
    assert record_screen(
        OUT, stop, offset_x=0, offset_y=0, width=800, height=600, open_fn=open_fn
    ) is True
    assert dst.added[0].encoded == ["frame-0"], "事件置位后不再取新帧"
    assert dst.added[0].flushed is True


def test_record_screen_open_failure_returns_false():
    def boom(path, *args, **kwargs):
        raise OSError("no gdigrab")

    assert (
        record_screen(
            OUT,
            types.SimpleNamespace(is_set=lambda: False),
            offset_x=0,
            offset_y=0,
            width=800,
            height=600,
            open_fn=boom,
        )
        is False
    )
