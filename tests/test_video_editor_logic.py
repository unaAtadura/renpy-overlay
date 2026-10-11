"""视频编辑纯函数的离线验证：帧率换算、文本解析、单击换算与音量步进。

遵循项目离线测试约定：不构造真实 QWidget（窗口与 QMediaPlayer 交互由
人工联测覆盖），仅验证模块级纯函数的换算/解析/边界行为；帧放大窗的
关闭/复用容错逻辑经 fake 对象绑定未绑定方法验证（不构造真实窗口）。
"""

from __future__ import annotations

import types

from PyQt6.QtGui import QImage

from renpy_overlay.video.editor_window import (
    DEFAULT_FPS,
    FRAME_MIN_INTERVAL_MS,
    SEEK_THROTTLE_MS,
    STEP_MS,
    VideoEditorWindow,
    fps_from_metadata,
    frame_duration_ms,
    frame_to_ms,
    max_frame_for,
    ms_to_frame,
    parse_frame_text,
    should_accept_frame,
    should_seek_now,
    slider_value_from_click,
    volume_from_wheel,
)
from renpy_overlay.video.frame_viewer import MAX_SCALE, MIN_SCALE, next_scale


def test_fps_from_metadata_valid_and_fallback():
    """元数据帧率解析（防回归：PyQt6 metaDataChanged 信号无参，槽内经
    QMediaPlayer.metaData() 主动读取后交本纯函数换算）。"""
    assert fps_from_metadata(30.0) == 30.0
    assert fps_from_metadata(23.976) == 23.976
    assert fps_from_metadata(0) == DEFAULT_FPS  # 零值回退
    assert fps_from_metadata(-5) == DEFAULT_FPS  # 非法回退
    assert fps_from_metadata(None) == DEFAULT_FPS  # 元数据缺失回退
    assert fps_from_metadata("abc") == DEFAULT_FPS  # 类型非法回退


def test_frame_duration_ms_default_and_invalid_fps():
    assert frame_duration_ms(DEFAULT_FPS) == 33  # 1000/30 ≈ 33ms
    assert frame_duration_ms(0) == 33  # 非法回退 30fps
    assert frame_duration_ms(-5) == 33
    assert frame_duration_ms(60) == 17  # 1000/60 ≈ 17ms


def test_ms_frame_roundtrip():
    assert ms_to_frame(0, 30) == 0
    assert ms_to_frame(33, 30) == 1
    assert frame_to_ms(1, 30) == 33
    # 帧号 → 毫秒 → 帧号往返稳定
    for frame in (0, 1, 42, 900):
        assert ms_to_frame(frame_to_ms(frame, 30), 30) == frame


def test_frame_to_ms_clamps_negative():
    assert frame_to_ms(-3, 30) == 0


def test_parse_frame_text_valid_and_boundary():
    assert parse_frame_text("0", 100) == 0
    assert parse_frame_text(" 42 ", 100) == 42
    assert parse_frame_text("100", 100) == 100  # 边界内（含最大帧）


def test_parse_frame_text_rejects_invalid_or_out_of_range():
    """非法与越界一律不响应（返回 None）：负数、非数字、空串、超出范围。"""
    assert parse_frame_text("-1", 100) is None
    assert parse_frame_text("abc", 100) is None
    assert parse_frame_text("", 100) is None
    assert parse_frame_text("101", 100) is None
    assert parse_frame_text("1.5", 100) is None


def test_max_frame_for_unknown_duration_blocks_everything():
    """未打开视频（duration=0）时任何帧号都视为越界。"""
    assert max_frame_for(0, 30) == -1
    assert parse_frame_text("0", max_frame_for(0, 30)) is None


def test_max_frame_for_known_duration():
    # duration=10s，30fps → ~303 帧
    assert max_frame_for(10_000, 30) == ms_to_frame(10_000, 30)


def test_slider_value_from_click_ratio_and_clamp():
    assert slider_value_from_click(0, 1000, 60_000) == 0
    assert slider_value_from_click(500, 1000, 60_000) == 30_000
    assert slider_value_from_click(1000, 1000, 60_000) == 60_000
    assert slider_value_from_click(1500, 1000, 60_000) == 60_000  # 越界钳满
    assert slider_value_from_click(100, 0, 60_000) == 0  # 宽度非法归零


def test_volume_from_wheel_steps_and_clamps():
    assert volume_from_wheel(120, 50) == 55  # 上滚 +
    assert volume_from_wheel(-120, 50) == 45  # 下滚 -
    assert volume_from_wheel(120, 98) == 100  # 上限钳制
    assert volume_from_wheel(-120, 2) == 0  # 下限钳制


def test_step_ms_constant():
    assert STEP_MS == 5000  # 需求：步退/步进 5 秒


def test_next_scale_zoom_in_out_and_clamp():
    scale = 1.0
    scale = next_scale(scale, 120)  # 放大
    assert scale > 1.0
    scale = next_scale(scale, -120)  # 缩小回原值附近
    assert abs(scale - 1.0) < 1e-9
    # 钳制：反复放大不超过 MAX，反复缩小不低于 MIN
    for _ in range(50):
        scale = next_scale(scale, 120)
    assert scale == MAX_SCALE
    for _ in range(50):
        scale = next_scale(scale, -120)
    assert scale == MIN_SCALE


# ---------------------------------------------------------------- 帧放大窗关闭/复用


class _Signal:
    """destroyed 信号的轻量 stub（connect/emit）。"""

    def __init__(self):
        self.handlers = []

    def connect(self, handler):
        self.handlers.append(handler)

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _StubViewer:
    """FrameViewerWindow 鸭子接口 stub：close 可注入已销毁异常。"""

    def __init__(self, close_error: Exception | None = None):
        self.destroyed = _Signal()
        self.closed = 0
        self.shown = False
        self._close_error = close_error

    def close(self):
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error

    def show_frame(self):
        self.shown = True


class _StubCanvas:
    def __init__(self, frame):
        self._frame = frame

    def current_frame(self):
        return self._frame


def _fake_editor(previous_viewer, new_viewer) -> types.SimpleNamespace:
    """构造 _open_frame_viewer 依赖的最小 fake（不构造 QWidget）。

    方法绑定到 fake 实例（等价实现），验证调用链与容错行为。
    """
    frame = QImage(4, 4, QImage.Format.Format_RGB32)
    fake = types.SimpleNamespace(
        _player=types.SimpleNamespace(
            playbackState=lambda: None,  # 非 PlayingState：暂停态
            position=lambda: 0,
        ),
        _fps=DEFAULT_FPS,
        _canvas=_StubCanvas(frame),
        _frame_viewer_factory=lambda frame, lower_than=None: new_viewer,
        _locked_hwnd_provider=None,
        _frame_viewer=previous_viewer,
    )
    fake._close_frame_viewer = lambda: VideoEditorWindow._close_frame_viewer(fake)
    fake._on_frame_viewer_destroyed = lambda *_args: (
        VideoEditorWindow._on_frame_viewer_destroyed(fake)
    )
    return fake


def test_open_closes_live_previous_and_holds_new():
    live = _StubViewer()
    new_viewer = _StubViewer()
    fake = _fake_editor(live, new_viewer)
    VideoEditorWindow._open_frame_viewer(fake)
    assert live.closed == 1, "打开新实例前应关闭旧实例"
    assert fake._frame_viewer is new_viewer, "必须持有新窗口引用（防 GC 销毁）"
    assert new_viewer.shown is True


def test_open_tolerates_deleted_stale_reference():
    """旧引用包装已被 deleteLater 销毁（close 抛 RuntimeError）：
    容错跳过、正常开新窗、不崩溃（实测崩溃路径）。"""
    stale = _StubViewer(
        close_error=RuntimeError("wrapped C/C++ object of type FrameViewerWindow has been deleted")
    )
    new_viewer = _StubViewer()
    fake = _fake_editor(stale, new_viewer)
    VideoEditorWindow._open_frame_viewer(fake)
    assert stale.closed == 1
    assert fake._frame_viewer is new_viewer
    assert new_viewer.shown is True


def test_close_frame_viewer_clears_reference_and_is_idempotent():
    live = _StubViewer()
    fake = _fake_editor(live, _StubViewer())
    VideoEditorWindow._close_frame_viewer(fake)
    assert live.closed == 1
    assert fake._frame_viewer is None
    VideoEditorWindow._close_frame_viewer(fake)  # 引用已空：幂等无副作用
    assert live.closed == 1


def test_destroyed_signal_clears_reference():
    """C++ 对象销毁（deleteLater 异步执行）后 destroyed 同步清引用。"""
    viewer = _StubViewer()
    fake = _fake_editor(None, viewer)
    fake._frame_viewer = viewer
    VideoEditorWindow._open_frame_viewer(fake)  # 内部完成 destroyed 连接
    viewer.destroyed.emit(viewer)
    assert fake._frame_viewer is None


# ---------------------------------------------------------------- 进度跳转节流


def test_should_seek_now_first_call_and_throttle_window():
    """首拍必放行（从未 seek）；节流窗口内挂起、超窗放行。"""
    assert should_seek_now(1000.0, -1.0) is True  # 首拍
    assert should_seek_now(1100.0, 1000.0) is False  # 窗口内（110ms < 150ms）
    assert should_seek_now(1150.0, 1000.0) is True  # 超窗（150ms）
    assert SEEK_THROTTLE_MS == 150


def test_should_accept_frame_throttle():
    """取帧节流：首帧必收、最小间隔内跳过、超窗接收。"""
    assert should_accept_frame(100.0, -1.0) is True  # 首帧
    assert should_accept_frame(110.0, 100.0) is False  # 10ms < 33ms
    assert should_accept_frame(133.0, 100.0) is True  # 33ms 达到间隔
    assert FRAME_MIN_INTERVAL_MS == 33


class _SeekClock:
    """可控毫秒钟 + setPosition 记录（拖拽 seek 路径的 fake）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.seeks: list[int] = []

    def player(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(setPosition=lambda p: self.seeks.append(p))


def _seek_fake(clock: _SeekClock) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _player=clock.player(),
        _clock_ms=lambda: clock.now,
        _last_seek_ms=-1.0,
        _pending_seek=None,
        _scrubbing=True,
    )


def test_slider_moved_throttles_seek_and_keeps_pending():
    """拖拽节流：首拍立即 seek，窗口内的后续 moved 只挂起位置不再 seek。"""
    clock = _SeekClock()
    fake = _seek_fake(clock)
    VideoEditorWindow._on_slider_moved(fake, 5000)
    assert clock.seeks == [5000]  # 首拍立即 seek
    clock.now = 1050.0  # 距上次 seek 仅 50ms（< 150ms 节流窗口）
    VideoEditorWindow._on_slider_moved(fake, 6000)
    assert clock.seeks == [5000], "节流窗口内不应重复 seek（seek 风暴实测撕裂/卡死）"
    assert fake._pending_seek == 6000, "挂起位置应更新为最新拖拽值"


def test_slider_released_seeks_pending_value():
    """松手落定：节流期间挂起的最终位置补一次 seek 并清空。"""
    clock = _SeekClock()
    fake = _seek_fake(clock)
    VideoEditorWindow._on_slider_moved(fake, 5000)
    clock.now = 1050.0
    VideoEditorWindow._on_slider_moved(fake, 8000)  # 挂起 8000
    VideoEditorWindow._on_slider_released(fake)
    assert clock.seeks == [5000, 8000], "拖拽终点应补 seek 落定"
    assert fake._pending_seek is None
    assert fake._scrubbing is False


def test_slider_released_without_pending_does_not_seek():
    clock = _SeekClock()
    fake = _seek_fake(clock)
    VideoEditorWindow._on_slider_released(fake)
    assert clock.seeks == []  # 无挂起位置：不额外 seek


def test_frame_changed_throttles_and_skips_invalid():
    """取帧节流：无效帧跳过；最小间隔内的后续帧跳过（主线程 toImage 减负）。"""
    frames: list = []
    clock = types.SimpleNamespace(now=1000.0)
    fake = types.SimpleNamespace(
        _canvas=types.SimpleNamespace(set_frame=lambda img: frames.append(img)),
        _clock_ms=lambda: clock.now,
        _last_frame_ms=-1.0,
    )
    valid = types.SimpleNamespace(isValid=lambda: True, toImage=lambda: "IMG")
    invalid = types.SimpleNamespace(isValid=lambda: False)
    VideoEditorWindow._on_video_frame_changed(fake, invalid)
    assert frames == [], "无效帧应被跳过"
    VideoEditorWindow._on_video_frame_changed(fake, valid)
    assert frames == ["IMG"]  # 首帧必收
    clock.now = 1010.0  # 10ms < 33ms：节流窗口内
    VideoEditorWindow._on_video_frame_changed(fake, valid)
    assert frames == ["IMG"], "节流窗口内的帧应跳过（避免主线程 toImage 风暴）"
    clock.now = 1033.0
    VideoEditorWindow._on_video_frame_changed(fake, valid)
    assert frames == ["IMG", "IMG"]
