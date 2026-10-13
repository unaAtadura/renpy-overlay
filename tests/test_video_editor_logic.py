"""视频编辑纯函数的离线验证：帧率换算、文本解析、单击换算与音量步进。

遵循项目离线测试约定：不构造真实 QWidget（窗口与播放引擎交互由
人工联测覆盖），仅验证模块级纯函数的换算/解析/边界行为；帧放大窗的
关闭/复用容错逻辑经 fake 对象绑定未绑定方法验证（不构造真实窗口）。
"""

from __future__ import annotations

import types

from PyQt6.QtGui import QImage

from renpy_overlay.video.editor_window import (
    BUTTON_SEEK_THROTTLE_MS,
    DEFAULT_FPS,
    LIVENESS_HEARTBEAT_MS,
    SEEK_THROTTLE_MS,
    STEP_MS,
    VideoEditorWindow,
    _LivenessStats,
    frame_duration_ms,
    frame_to_ms,
    max_frame_for,
    ms_to_frame,
    parse_frame_text,
    should_seek_now,
    slider_value_from_click,
    volume_from_wheel,
)
from renpy_overlay.video.frame_viewer import MAX_SCALE, MIN_SCALE, next_scale


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
    assert STEP_MS == 1000  # 需求：步退/步进 1 秒


def test_liveness_stats_counts_and_reset():
    """活性统计：计数累加与心跳窗口重置（冻结/挂起判别的数据基础）。"""
    assert LIVENESS_HEARTBEAT_MS == 2000
    stats = _LivenessStats()
    stats.frame_count += 2
    stats.frame_skip_count += 1
    stats.position_count += 3
    stats.reset()
    assert stats.frame_count == 0
    assert stats.frame_skip_count == 0
    assert stats.position_count == 0


def test_log_liveness_logs_and_resets():
    """心跳打点：输出汇总后清零计数（下个窗口从零开始）。"""
    fake = types.SimpleNamespace(_liveness=_LivenessStats())
    fake._liveness.frame_count = 4
    fake._liveness.position_count = 7
    VideoEditorWindow._log_liveness(fake)
    assert fake._liveness.frame_count == 0
    assert fake._liveness.position_count == 0


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


class _SeekClock:
    """可控毫秒钟 + setPosition 记录（拖拽 seek 路径的 fake）。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.seeks: list[int] = []

    def player(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(setPosition=lambda p: self.seeks.append(p))


def _seek_fake(clock: _SeekClock) -> types.SimpleNamespace:
    fake = types.SimpleNamespace(
        _player=clock.player(),
        _clock_ms=lambda: clock.now,
        _last_seek_ms=-1.0,
        _pending_seek=None,
        _scrubbing=True,
        _pending_timer=_StubTimer(),
    )
    fake._commit_pending_seek = lambda *args: (
        VideoEditorWindow._commit_pending_seek(fake, *args)
    )
    return fake


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


def test_drag_sequence_press_move_move_release():
    """完整拖拽序列（窗口端契约）：press 单击跳转首拍 → 拖动节流挂起 →
    松手落定最终位置；拖动中 seek 次数被节流，终点必达。"""
    clock = _SeekClock()
    fake = _seek_fake(clock)
    VideoEditorWindow._on_slider_moved(fake, 5000)  # press：单击跳转（首拍）
    assert clock.seeks == [5000]
    clock.now += 10
    VideoEditorWindow._on_slider_moved(fake, 6000)  # 拖动（窗口内：挂起）
    clock.now += 10
    VideoEditorWindow._on_slider_moved(fake, 7000)  # 拖动（挂起更新）
    assert clock.seeks == [5000], "拖动中仅节流首拍已 seek"
    VideoEditorWindow._on_slider_released(fake)  # 松手：落定最终位置
    assert clock.seeks == [5000, 7000]
    assert fake._pending_seek is None and fake._scrubbing is False


# ---------------------------------------------------------------- 统一 seek 合并（按钮/文本框路径）


class _StubTimer:
    """QTimer 的轻量 stub（记录 start/stop 调用，不进事件循环）。"""

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def _seek_to_fake(clock: _SeekClock, timer: _StubTimer, *, valid_source: bool = True):
    fake = types.SimpleNamespace(
        _player=types.SimpleNamespace(
            setPosition=lambda p: clock.seeks.append(p),
            position=lambda: 400_000,
            hasMedia=lambda: valid_source,
        ),
        _clock_ms=lambda: clock.now,
        _last_seek_ms=-1.0,
        _pending_seek=None,
        _last_target_ms=None,
        _scrubbing=False,
        _pending_timer=timer,
    )
    fake._commit_pending_seek = lambda *args: (
        VideoEditorWindow._commit_pending_seek(fake, *args)
    )
    fake._seek_to = lambda *args, **kwargs: VideoEditorWindow._seek_to(fake, *args, **kwargs)
    return fake


def test_seek_to_commits_immediately_on_first_call():
    """按钮/文本框 seek 首拍立即真实 seek（原先直连 setPosition 无节流，
    连点步退触发 seek 风暴花屏卡死；现与拖拽共用合并节流）。"""
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer)
    VideoEditorWindow._seek_to(fake, 12_345)
    assert clock.seeks == [12_345]
    assert fake._pending_seek is None
    assert fake._last_seek_ms == 1000.0  # 记录本次真实 seek 时刻
    assert timer.started == 1  # 兜底 timer 已启动


def test_seek_to_pends_within_throttle_window():
    """节流窗口内连续 seek：只挂起最新目标，不触发真实 seek。"""
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer)
    VideoEditorWindow._seek_to(fake, 5000)
    clock.now = 1050.0  # 距首拍 50ms（< 150ms 窗口）
    VideoEditorWindow._seek_to(fake, 9000)
    assert clock.seeks == [5000], "窗口内不应重复真实 seek"
    assert fake._pending_seek == 9000, "挂起目标应更新为最新请求"
    assert timer.started == 2


def test_pending_timer_timeout_commits_latest_target():
    """兜底 timer 超时落定：挂起的最新目标补一次 seek 并清空。"""
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer)
    VideoEditorWindow._seek_to(fake, 5000)
    clock.now = 1050.0
    VideoEditorWindow._seek_to(fake, 9000)  # 挂起 9000
    clock.now = 1200.0
    VideoEditorWindow._on_pending_timer_timeout(fake)
    assert clock.seeks == [5000, 9000]
    assert fake._pending_seek is None
    assert fake._last_seek_ms == 1200.0


def test_pending_timer_timeout_skips_while_scrubbing():
    """拖拽中 timer 超时不抢落定（由 sliderReleased 落定）。"""
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer)
    fake._scrubbing = True
    fake._pending_seek = 7000
    VideoEditorWindow._on_pending_timer_timeout(fake)
    assert clock.seeks == []
    assert fake._pending_seek == 7000


def test_seek_to_ignores_without_valid_source():
    """未打开视频：不 seek、不挂起、不启动 timer。"""
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer, valid_source=False)
    VideoEditorWindow._seek_to(fake, 5000)
    assert clock.seeks == []
    assert fake._pending_seek is None
    assert timer.started == 0


def test_seek_by_accumulates_pending_target():
    """连点步进基于「挂起/最近请求目标」累加：position() 读数陈旧时不再
    重复 seek 同一目标（实测连续两次落定同一位置），点 N 次累计 N 秒。"""
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer)
    VideoEditorWindow._seek_by(fake, -1000)  # 首拍：base=position()=400000，立即落定
    assert clock.seeks == [399_000]
    clock.now = 1050.0  # 按钮窗口内：挂起
    VideoEditorWindow._seek_by(fake, -1000)  # base=最近请求目标 399000 → 398000
    assert fake._pending_seek == 398_000, "累加基准应为最近请求目标而非陈旧 position()"
    clock.now = 1350.0
    VideoEditorWindow._on_pending_timer_timeout(fake)  # 兜底落定累加结果
    assert clock.seeks == [399_000, 398_000]


def test_seek_to_uses_widened_button_window():
    """按钮 seek 专用 250ms 窗口：距上次 seek 200ms 的新请求仍挂起（拖拽
    150ms 窗口此时会放行），抑制连点期间的持续 seek 流（花屏残留实测）。"""
    assert BUTTON_SEEK_THROTTLE_MS == 250
    clock = _SeekClock()
    timer = _StubTimer()
    fake = _seek_to_fake(clock, timer)
    VideoEditorWindow._seek_to(fake, 5000)  # 首拍落定，last_seek_ms=1000
    clock.now = 1200.0  # 距上次 seek 200ms（150 < 200 < 250）
    VideoEditorWindow._seek_to(fake, 6000)
    assert clock.seeks == [5000], "按钮窗口 250ms 内应挂起"
    assert fake._pending_seek == 6000


def test_last_target_invalidates_when_position_catches_up():
    """位置回放追平请求目标（差 ≤ 一帧）后失效：后续步进改以实际位置为基准。

    fake 最小依赖：_on_position_changed 只触碰 _last_target_ms/_fps/
    _scrubbing/_position_slider（离线测试约定不构造真实 QWidget）。
    """
    slider = types.SimpleNamespace(
        blockSignals=lambda *_: None,
        setValue=lambda *_: None,
    )
    fake = types.SimpleNamespace(
        _last_target_ms=398_000,
        _fps=DEFAULT_FPS,
        _scrubbing=False,
        _position_slider=slider,
        _liveness=_LivenessStats(),
    )
    VideoEditorWindow._on_position_changed(fake, 397_000)  # 差 1000ms：不追平
    assert fake._last_target_ms == 398_000
    assert fake._liveness.position_count == 1  # 活性统计同步累加
    VideoEditorWindow._on_position_changed(fake, 398_010)  # 差 10ms ≤ 33ms：追平失效
    assert fake._last_target_ms is None


def test_frame_ready_updates_canvas_and_liveness():
    """引擎完成帧上屏：canvas 接收成品 QImage，活性帧计数同步累加。"""
    frames: list = []
    fake = types.SimpleNamespace(
        _canvas=types.SimpleNamespace(set_frame=lambda img: frames.append(img)),
        _liveness=_LivenessStats(),
    )
    VideoEditorWindow._on_frame_ready(fake, "IMG")
    VideoEditorWindow._on_frame_ready(fake, "IMG2")
    assert frames == ["IMG", "IMG2"]
    assert fake._liveness.frame_count == 2
