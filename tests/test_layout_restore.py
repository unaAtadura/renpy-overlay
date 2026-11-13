"""布局恢复编排的离线验证：stub 宿主注入，不构造真实 QWidget。

需求（.raw_plans/future_恢复布局.txt）的恢复编排逐项覆盖：恢复顺序约束
（先建+锁截图窗、再开快捷键模式——锁定窗口表在模式开启瞬间快照）、
restore_window 的守卫与几何、``_set_locked`` 与 ``_toggle_lock`` 共用同一
代码路径、``_open_choice_log`` 的 pos 分支与 skip_injection 硬守卫、
``_handle_injected`` 的入口 label 按窗口实际状态同步（回归）、follow 的
一次性 pending 定位。全部经鸭子接口 stub / 真实方法绑定覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QColor

from renpy_overlay import layout, win32api
from renpy_overlay.quick_menu import QuickMenu
from renpy_overlay.screenshot.window import FRAME_COLORS, ScreenshotWindow
from renpy_overlay.stream.follow import WindowFollowMixin
from renpy_overlay.stream.geometry import compute_geometry, pair_layout
from renpy_overlay.stream.host_core import StreamOverlayCore
from renpy_overlay.stream.injection_flow import InjectionFlowMixin

SCREEN_FALLBACK = QPoint(400, 300)  # QuickMenu._screen_center 的无屏兜底值


# ---- 截图窗口 stub ------------------------------------------------------------


class _StubScreenshotWindow:
    """restore_window 工厂注入的鸭子 stub：记录 show/resize/move/锁定调用。"""

    def __init__(self, index: int, color: QColor, on_click=None, parent=None) -> None:
        self.index = index
        self.color = color
        self.on_click = on_click
        self.shown = False
        self.size: tuple[int, int] | None = None
        self.position: QPoint | None = None
        self._locked = False
        self.lock_calls: list[bool] = []

    def show(self) -> None:
        self.shown = True

    def hide(self) -> None:
        self.shown = False

    def resize(self, width: int, height: int) -> None:
        self.size = (int(width), int(height))

    def move(self, *args) -> None:
        # 兼容 QWidget.move 的两种重载：move(QPoint) 与 move(x, y)
        self.position = QPoint(args[0]) if len(args) == 1 else QPoint(args[0], args[1])

    def _set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        self.lock_calls.append(bool(locked))

    @property
    def is_locked(self) -> bool:
        return self._locked

    def geometry(self) -> QRect:
        """锁定期线框快照源：Qt 全局逻辑坐标矩形。"""
        pos = self.position or QPoint(0, 0)
        size = self.size or (300, 200)
        return QRect(pos.x(), pos.y(), size[0], size[1])

    def close(self) -> None:
        pass

    def deleteLater(self) -> None:  # noqa: N802 - Qt 约定
        pass


class _FakeOverlay:
    """框选提示窗口组鸭子接口 stub（布局锁定路径触碰的最小面）。"""

    def show_frames(self, frames) -> None:
        pass

    def hide_overlay(self) -> None:
        pass

    def restore(self) -> None:
        pass

    def is_showing(self) -> bool:
        return False

    def destroy(self) -> None:
        pass

    def reassert_topmost(self) -> None:
        pass

    def set_frame_visible(self, index, visible) -> None:
        pass


def _make_menu() -> QuickMenu:
    return QuickMenu(
        None,
        None,
        frame_overlay_factory=lambda: _FakeOverlay(),
        window_factory=_StubScreenshotWindow,
    )


# ---- QuickMenu.restore_window ---------------------------------------------------


def test_restore_window_positions_and_locks_via_double_click_path():
    menu = _make_menu()
    window = menu.restore_window(320, 240, locked=True, pos=(100, 200))
    assert window is not None
    assert window.size == (320, 240)
    assert window.position == QPoint(100, 200)
    assert window.shown is True  # 先 show 再定位（与 create_window 同序）
    assert window.is_locked is True
    assert window.lock_calls == [True]  # 与双击锁定同一代码路径
    assert menu.window_count == 1


def test_restore_window_color_follows_pool_order():
    menu = _make_menu()
    first = menu.restore_window(300, 200, pos=(0, 0))
    second = menu.restore_window(300, 200, pos=(10, 10))
    # 需求：与右键创建效果一致——颜色按创建顺序分配
    assert first.color == FRAME_COLORS[0]
    assert second.color == FRAME_COLORS[1]


def test_restore_window_rejects_when_full():
    menu = _make_menu()
    for _ in range(layout.MAX_SCREENSHOT_WINDOWS):
        assert menu.restore_window(300, 200, pos=(0, 0)) is not None
    assert menu.restore_window(300, 200, pos=(0, 0)) is None
    assert menu.window_count == layout.MAX_SCREENSHOT_WINDOWS


def test_restore_window_rejects_when_layout_locked():
    menu = _make_menu()
    menu.lock_layout()
    assert menu.restore_window(300, 200, pos=(0, 0)) is None
    assert menu.window_count == 0


def test_restore_window_without_pos_uses_cascade_like_create(monkeypatch):
    monkeypatch.setattr(QuickMenu, "_screen_center", staticmethod(lambda: SCREEN_FALLBACK))
    menu = _make_menu()
    window = menu.restore_window(300, 200)
    assert window is not None
    # 级联定位与 create_window 同款：主屏中心 - 半尺寸 + 池序偏移
    assert window.position == QPoint(400 - 150, 300 - 100)


# ---- 宿主编排：_apply_layout / _restore_screenshot_windows ------------------------


class _RecordingHost:
    """鸭子接口宿主：绑定 StreamOverlayCore 的真实编排方法，记录调用序。"""

    def __init__(self, skip_injection: bool = False) -> None:
        self.skip_injection = skip_injection
        self.width = 800
        self.height = 200
        self._title_h = 20
        self._locked = False
        self._user_offset = None
        self._layout_pending_position = False
        self._choice_log_window = None
        self.resized: list[tuple[int, int]] = []
        self.enabled_calls: list[bool] = []
        self.choice_log_calls: list[dict] = []
        self.screen_rects = [(0, 0, 1920, 1080)]
        self.restore_order: list[str] = []
        self._body_window = SimpleNamespace(
            resize=lambda w, h: self.resized.append((int(w), int(h)))
        )
        self._title_window = SimpleNamespace(resize=lambda w, h: None)
        self._quick_menu = QuickMenu(
            None,
            None,
            frame_overlay_factory=lambda: _FakeOverlay(),
            window_factory=self._make_pool_window,
        )
        self._hotkey_mode = SimpleNamespace(
            enabled=False,
            set_enabled=self._record_hotkey_enable,
        )
        self.remote_baselines: list = []
        self._remote_screenshot = SimpleNamespace(
            set_layout_restore=self._record_remote_baseline,
            layout_snapshot=lambda: None,
            windows_created=False,
        )

    def _record_remote_baseline(self, state) -> None:
        self.restore_order.append("remote")
        self.remote_baselines.append(state)

    def _make_pool_window(self, index, color, on_click=None, parent=None):
        self.restore_order.append(f"window{index}")
        return _StubScreenshotWindow(index, color, on_click)

    def _record_hotkey_enable(self, enable: bool) -> None:
        self.restore_order.append(f"hotkey{enable}")
        self.enabled_calls.append(bool(enable))
        self._hotkey_mode.enabled = bool(enable)

    def _open_choice_log(self, width, height, pos=None, locked=False):
        self.restore_order.append("choice")
        self.choice_log_calls.append(
            {"width": width, "height": height, "pos": pos, "locked": locked}
        )
        self._choice_log_window = object()

    def _screen_rects_logical(self):
        return self.screen_rects

    def _restore_screenshot_windows(self, entries) -> None:
        # 绑定真实编排方法：_apply_layout 经 self 调用，stub 必须可达
        StreamOverlayCore._restore_screenshot_windows(self, entries)


def _layout_state(**overrides) -> layout.LayoutState:
    kwargs = dict(
        stream_width=900,
        stream_height=220,
        stream_locked=True,
        stream_user_offset=(5, 5),
        stream_abs_rect=None,
        screenshot_windows=[(0, 0, 300, 200, True), (50, 50, 300, 200, True)],
        hotkey_mode=True,
        choice_log_open=True,
        choice_log_rect=(10, 10, 150, 300),
        choice_log_locked=False,
    )
    kwargs.update(overrides)
    return layout.parse_layout(layout.collect_layout(**kwargs))


def test_apply_layout_order_windows_then_hotkey_then_choice():
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(host, _layout_state())
    # 顺序即正确性：截图窗先建+锁 → 快捷键模式（锁定窗口表快照依赖）→ 日志窗
    assert host.restore_order == ["window0", "window1", "hotkeyTrue", "choice"]
    assert host.enabled_calls == [True]


def test_apply_layout_restores_stream_state_and_pending_position():
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(host, _layout_state())
    assert (host.width, host.height) == (900, 220)
    assert host.resized == [(900, 220)]  # 正文窗尺寸写回
    assert host._locked is True
    assert host._user_offset == (5, 5)
    assert host._layout_pending_position is True  # follow 首个有效 tick 消费


def test_apply_layout_unlocked_offset_only_keeps_unlocked():
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(
        host,
        _layout_state(stream_locked=False, hotkey_mode=False, choice_log_open=False),
    )
    assert host._locked is False
    assert host._user_offset == (5, 5)


def test_apply_layout_restored_screenshot_windows_are_locked():
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(host, _layout_state())
    windows = host._quick_menu.screenshot_windows()
    assert len(windows) == 2
    assert all(window.is_locked for window in windows)


def test_apply_layout_hotkey_mode_not_reenabled_when_already_on():
    host = _RecordingHost()
    host._hotkey_mode.enabled = True
    StreamOverlayCore._apply_layout(
        host,
        _layout_state(
            stream_locked=False, stream_user_offset=None, screenshot_windows=[],
            choice_log_open=False,
        ),
    )
    assert host.enabled_calls == []


def test_apply_layout_skip_injection_never_opens_choice_log():
    host = _RecordingHost(skip_injection=True)
    StreamOverlayCore._apply_layout(host, _layout_state())
    # 需求：跳过注入模式无副作用日志窗口——即便 layout 记录 open 也不显示
    assert host.choice_log_calls == []
    assert host.restore_order == ["window0", "window1", "hotkeyTrue"]


def test_apply_layout_passes_layout_size_and_pos_to_choice_log():
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(host, _layout_state())
    assert host.choice_log_calls == [
        {"width": 150, "height": 300, "pos": (10, 10), "locked": False}
    ]


def _remote_node() -> dict:
    return layout.collect_remote_screenshot_layout(
        a_x=10,
        a_y=20,
        a_diameter=80,
        b_x=200,
        b_y=300,
        b_diameter=100,
        loaded_color="#ff3232",
    )


def test_apply_layout_injects_remote_baseline_before_hotkey_mode():
    """需求四.2：基线必须在快捷键模式开启之前注入（A/B 生成时消费）。"""
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(
        host, _layout_state(remote_screenshot=_remote_node())
    )
    assert host.restore_order == [
        "window0",
        "window1",
        "remote",
        "hotkeyTrue",
        "choice",
    ]
    baseline = host.remote_baselines[-1]
    assert baseline.a.diameter == 80
    assert baseline.loaded_color == "#ff3232"


def test_apply_layout_without_remote_node_does_not_touch_controller():
    """需求四.5：节点缺失 → 不注入基线（控制器保持改动前默认摆放行为）。"""
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(host, _layout_state())
    assert host.remote_baselines == []


def test_apply_layout_drops_offset_when_saved_rect_offscreen(monkeypatch):
    monkeypatch.setattr(
        "renpy_overlay.stream.host_core.win32api.virtual_screen_rect",
        lambda: (0, 0, 1920, 1080),
    )
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(
        host,
        _layout_state(
            stream_locked=False,
            stream_user_offset=(100, 100),
            stream_abs_rect=(5000, 5000, 6000, 5200),
            screenshot_windows=[],
            hotkey_mode=False,
            choice_log_open=False,
        ),
    )
    # 离屏（分辨率/显示器变化）：丢弃偏移回退自动停靠，尺寸保留
    assert host._user_offset is None
    assert (host.width, host.height) == (900, 220)
    assert host._layout_pending_position is True


def test_apply_layout_keeps_offset_when_saved_rect_onscreen(monkeypatch):
    monkeypatch.setattr(
        "renpy_overlay.stream.host_core.win32api.virtual_screen_rect",
        lambda: (0, 0, 1920, 1080),
    )
    host = _RecordingHost()
    StreamOverlayCore._apply_layout(
        host,
        _layout_state(
            stream_locked=False,
            stream_user_offset=(100, 100),
            stream_abs_rect=(100, 100, 1000, 320),
            screenshot_windows=[],
            hotkey_mode=False,
            choice_log_open=False,
        ),
    )
    assert host._user_offset == (100, 100)


def test_restore_screenshot_windows_uses_recorded_position():
    host = _RecordingHost()
    entries = (layout.ScreenshotWindowLayout(x=120, y=80, width=320, height=240, locked=True),)
    StreamOverlayCore._restore_screenshot_windows(host, entries)
    window = host._quick_menu.screenshot_windows()[0]
    assert window.position == QPoint(120, 80)
    assert window.size == (320, 240)
    assert window.is_locked is True


def test_restore_screenshot_windows_offscreen_falls_back_to_cascade(monkeypatch):
    monkeypatch.setattr(QuickMenu, "_screen_center", staticmethod(lambda: SCREEN_FALLBACK))
    host = _RecordingHost()
    host.screen_rects = [(0, 0, 1920, 1080)]
    entries = (layout.ScreenshotWindowLayout(x=5000, y=100, width=300, height=200),)
    StreamOverlayCore._restore_screenshot_windows(host, entries)
    window = host._quick_menu.screenshot_windows()[0]
    # 离屏回退：与新建相同的级联定位（尺寸保留）
    assert window.position == QPoint(250, 200)
    assert window.size == (300, 200)


def test_restore_screenshot_windows_clamps_size_to_minimum():
    host = _RecordingHost()
    entries = (layout.ScreenshotWindowLayout(x=0, y=0, width=10, height=10),)
    StreamOverlayCore._restore_screenshot_windows(host, entries)
    assert host._quick_menu.screenshot_windows()[0].size == (80, 60)


def test_restore_screenshot_windows_stops_when_refused():
    host = _RecordingHost()
    host._quick_menu._layout_locked = True  # 布局锁定拒绝：剩余条目一并放弃
    entries = (layout.ScreenshotWindowLayout(x=0, y=0),)
    StreamOverlayCore._restore_screenshot_windows(host, entries)
    assert host._quick_menu.window_count == 0


# ---- ScreenshotWindow._set_locked 与 _toggle_lock 的路径等价 ----------------------


class _LockTarget:
    """鸭子目标：只实现 _set_locked 触碰的成员（状态与调用记录）。"""

    def __init__(self) -> None:
        self._index = 3
        self._locked = False
        self._dragging = True
        self._resizing = True
        self._resize_edge = "nw"
        self._hovered_edge = "n"
        self.cursors: list[object] = []
        self.updates = 0

    def setCursor(self, cursor) -> None:  # noqa: N802 - Qt 约定
        self.cursors.append(cursor)

    def update(self) -> None:
        self.updates += 1


def test_set_locked_same_state_is_noop():
    target = _LockTarget()
    target._locked = True
    ScreenshotWindow._set_locked(target, True)
    assert target.updates == 0  # 目标态一致：不重绘、不走清理分支
    assert target._dragging is True


def test_set_locked_lock_branch_clears_gesture_state():
    target = _LockTarget()
    ScreenshotWindow._set_locked(target, True)
    assert target._locked is True
    assert target._dragging is False
    assert target._resizing is False
    assert target._resize_edge is None
    assert target._hovered_edge is None
    assert target.updates == 1  # 状态重绘立即触发


def test_toggle_lock_delegates_to_set_locked():
    target = _LockTarget()
    # 绑定委托链：_toggle_lock 经 self._set_locked 翻转（双击路径等价性）
    target._set_locked = lambda locked: ScreenshotWindow._set_locked(target, locked)
    ScreenshotWindow._toggle_lock(target)
    assert target._locked is True
    ScreenshotWindow._toggle_lock(target)
    assert target._locked is False  # 双击路径 = 翻转后经 _set_locked


# ---- InjectionFlowMixin._open_choice_log -----------------------------------------


class _FakeLogWindow:
    """ChoiceLogWindow 替身：记录锚定/移动/初始锁定/显示调用。"""

    def __init__(self, log, width, height):
        self.size = (width, height)
        self.anchors: list = []
        self.moves: list[QPoint] = []
        self.initial_locks = 0
        self.shown = False

    def anchor_to(self, point) -> None:
        self.anchors.append(point)

    def move(self, *args) -> None:
        self.moves.append(QPoint(args[0]) if len(args) == 1 else QPoint(args[0], args[1]))

    def apply_initial_locked(self) -> None:
        self.initial_locks += 1

    def show(self) -> None:
        self.shown = True


class _ChoiceHost:
    """鸭子宿主：绑定真实的 _open_choice_log。"""

    def __init__(self, screen_rects) -> None:
        self._closed = False
        self._choice_log = object()  # ChoiceLogWindow 构造入参（累积器）
        self._choice_log_window = None
        self.screen_rects = screen_rects
        self.entry_anchors: list = []
        self.titles: list[str] = []
        self.entry_labels: list[bool] = []
        self._title_window = SimpleNamespace(
            entry_top_right_global=lambda entry_id: self.entry_anchors.append(entry_id) or "anchor"
        )

    def _update_title(self, text) -> None:
        self.titles.append(text)

    def _screen_rects_logical(self):
        return self.screen_rects

    def _sync_choice_log_entry(self, opened: bool) -> None:
        self.entry_labels.append(opened)


def _patch_log_window(monkeypatch):
    monkeypatch.setattr(
        "renpy_overlay.stream.choice_log_window.ChoiceLogWindow", _FakeLogWindow
    )


def test_open_choice_log_uses_layout_position_when_onscreen(monkeypatch):
    _patch_log_window(monkeypatch)
    host = _ChoiceHost([(0, 0, 1920, 1080)])
    InjectionFlowMixin._open_choice_log(host, 150, 300, pos=(100, 100), locked=True)
    window = host._choice_log_window
    # 需求：读取 layout 启动时不再从功能图标右上角弹出
    assert window.moves == [QPoint(100, 100)]
    assert window.anchors == []
    assert window.initial_locks == 1  # 锁定态与双击锁定同一代码路径
    assert window.shown is True
    assert host.entry_labels[-1] is True


def test_open_choice_log_offscreen_falls_back_to_anchor(monkeypatch):
    _patch_log_window(monkeypatch)
    host = _ChoiceHost([(0, 0, 1920, 1080)])
    InjectionFlowMixin._open_choice_log(host, 150, 300, pos=(5000, 100))
    window = host._choice_log_window
    assert window.moves == []
    assert len(window.anchors) == 1
    assert window.shown is True


def test_open_choice_log_without_pos_anchors_like_menu_entry(monkeypatch):
    _patch_log_window(monkeypatch)
    host = _ChoiceHost([(0, 0, 1920, 1080)])
    InjectionFlowMixin._open_choice_log(host, 150, 300)
    window = host._choice_log_window
    assert len(window.anchors) == 1
    assert window.moves == []


def test_open_choice_log_skipped_when_already_open(monkeypatch):
    created: list = []

    class _TrackedLogWindow(_FakeLogWindow):
        def __init__(self, log, width, height):
            super().__init__(log, width, height)
            created.append(self)

    monkeypatch.setattr(
        "renpy_overlay.stream.choice_log_window.ChoiceLogWindow", _TrackedLogWindow
    )
    host = _ChoiceHost([(0, 0, 1920, 1080)])
    host._choice_log_window = object()
    InjectionFlowMixin._open_choice_log(host, 150, 300, pos=(0, 0))
    # 单例守卫：已打开时不重复构造
    assert created == []


# ---- InjectionFlowMixin._handle_injected 的 label 同步回归 -------------------------


class _InjectedHost:
    """鸭子宿主：绑定真实的 _handle_injected（入口 label 回归测试）。"""

    request_prebuild = None
    _locate_last_translation = None

    def __init__(self, choice_open: bool) -> None:
        self._closed = False
        self.skip_injection = False
        self._injected = False
        self._choice_log_window = object() if choice_open else None
        self.labels: list[bool] = []
        self.entries: list[list] = []
        self._title_window = SimpleNamespace(
            set_title_entries=lambda entries: self.entries.append(entries)
        )

    def _sync_translation_mode_entry(self) -> None:
        pass

    def _sync_choice_log_entry(self, opened: bool) -> None:
        self.labels.append(opened)


def test_injected_syncs_choice_log_entry_with_actual_window_state():
    host = _InjectedHost(choice_open=True)
    InjectionFlowMixin._handle_injected(host)
    assert host.labels == [True]  # 启动 layout 恢复已打开：label 不得复位


def test_injected_syncs_choice_log_entry_closed_when_no_window():
    host = _InjectedHost(choice_open=False)
    InjectionFlowMixin._handle_injected(host)
    assert host.labels == [False]


# ---- WindowFollowMixin._consume_pending_layout_position ---------------------------


class _FollowHost:
    """鸭子宿主：绑定真实的一次性 pending 定位。"""

    def __init__(self, skip_injection: bool = False, hwnd: int = 0) -> None:
        self._closed = False
        self.skip_injection = skip_injection
        self._locked = True
        self._visible = False
        self.width = 1000
        self.height = 250
        self._title_h = 20
        self._title_gap = 4
        self.dock = "top-center"
        self._user_offset = (12, 34)
        self._layout_pending_position = True
        self._body_hwnd = 111
        self._title_hwnd = 222
        self.target_pid = 4321
        self._target_hwnd = hwnd
        self.visibility: list[bool] = []
        self.resolved: list[int] = []

    def _resolve_target(self) -> int:
        self.resolved.append(self._target_hwnd)
        return self._target_hwnd

    def _virtual_screen_rect(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def _set_visible(self, visible: bool) -> None:
        self.visibility.append(visible)
        self._visible = visible


def _patch_follow_win32(monkeypatch) -> list[tuple]:
    monkeypatch.setattr(
        "renpy_overlay.stream.follow.win32api.window_rect",
        lambda hwnd: (0, 0, 1600, 900),
    )
    moves: list[tuple] = []
    monkeypatch.setattr(
        "renpy_overlay.stream.follow.win32api.move_window",
        lambda hwnd, x, y, w, h, **kwargs: moves.append((hwnd, x, y, w, h)),
    )
    return moves


def test_consume_pending_positions_once_and_shows(monkeypatch):
    moves = _patch_follow_win32(monkeypatch)
    host = _FollowHost(hwnd=777)
    WindowFollowMixin._consume_pending_layout_position(host)
    assert host._layout_pending_position is False  # 一次性：消费即清标志
    expected_total = compute_geometry(
        (0, 0, 1600, 900), (1000, 250 + 20 + 4), "top-center", (12, 34)
    )
    title_rect, body_rect = pair_layout(expected_total, 20, 4)
    assert moves[0] == (111, *body_rect)  # 正文窗先动（与跟随循环同序）
    assert moves[1] == (222, *title_rect)
    assert host.visibility == [True]  # 首次定位即显示（无旧位置闪烁）


def test_consume_pending_waits_for_game_window(monkeypatch):
    moves = _patch_follow_win32(monkeypatch)
    host = _FollowHost(hwnd=0)
    WindowFollowMixin._consume_pending_layout_position(host)
    # 游戏窗口尚未出现：保留标志下个 tick 再试，等待期间不显示（无闪烁）
    assert host._layout_pending_position is True
    assert moves == []
    assert host.visibility == []


def test_consume_pending_is_one_shot(monkeypatch):
    moves = _patch_follow_win32(monkeypatch)
    host = _FollowHost(hwnd=777)
    WindowFollowMixin._consume_pending_layout_position(host)
    moves.clear()
    WindowFollowMixin._consume_pending_layout_position(host)
    assert moves == []  # 已消费：重复调用 no-op


def test_consume_pending_skip_injection_uses_virtual_screen(monkeypatch):
    moves = _patch_follow_win32(monkeypatch)
    host = _FollowHost(skip_injection=True)
    WindowFollowMixin._consume_pending_layout_position(host)
    assert host._layout_pending_position is False
    assert host.resolved == []  # 未走游戏窗口定位路径
    assert len(moves) == 2  # 两窗均按虚拟屏参照定位


# ---- 启动鲁棒性与 win32 原语 ----------------------------------------------------


class _ExplodingApplyHost:
    """_apply_layout 必抛的 stub：验证恢复异常不阻塞启动。"""

    def __init__(self, game_dir: str) -> None:
        self._game_dir = game_dir
        self.applied = 0

    def _apply_layout(self, state) -> None:
        self.applied += 1
        raise RuntimeError("恢复组件故障")


def test_load_and_apply_layout_swallows_restore_failure(tmp_path):
    """布局恢复失败绝不阻塞启动（实测缺陷 164807 的兜底回归）。

    GetSystemMetrics 缺失曾使二次启动直接崩溃——恢复阶段任何异常只留
    痕并按默认布局继续（沿配置问题绝不导致崩溃的项目原则）。
    """
    path = layout.layout_path_for(str(tmp_path))
    layout.save_layout(
        path,
        layout.collect_layout(
            stream_width=800,
            stream_height=200,
            stream_locked=False,
            stream_user_offset=None,
            stream_abs_rect=None,
            screenshot_windows=[],
            hotkey_mode=False,
            choice_log_open=False,
            choice_log_rect=None,
        ),
    )
    host = _ExplodingApplyHost(str(tmp_path))
    StreamOverlayCore._load_and_apply_layout(host)  # 不得抛出
    assert host.applied == 1  # 确实走到了应用阶段（异常被吞而非提前返回）


def test_virtual_screen_rect_smoke_on_windows():
    """GetSystemMetrics 经 ctypes user32（pywin32 的 win32gui 无此函数，
    实测缺陷 164807）：真机返回非退化 ltrb；非 Windows 跳过。"""
    if not win32api.IS_WINDOWS:
        pytest.skip("仅 Windows")
    left, top, right, bottom = win32api.virtual_screen_rect()
    assert right > left
    assert bottom > top


# ---- _persist_layout：遥控器快照与未创建保留旧节点 --------------------------------


class _PersistHost:
    """_persist_layout 的鸭子宿主：固定各窗口状态，遥控器快照可配置。"""

    def __init__(
        self,
        game_dir: str,
        remote_created: bool,
        remote_snapshot: tuple | None,
    ) -> None:
        self._game_dir = game_dir
        self.width = 800
        self.height = 200
        self._locked = False
        self._user_offset = None
        self._title_hwnd = 0xA
        self._body_hwnd = 0xB
        self._choice_log_window = None
        self._hotkey_mode = SimpleNamespace(enabled=False)
        self._quick_menu = SimpleNamespace(screenshot_windows=lambda: [])
        self._remote_screenshot = SimpleNamespace(
            windows_created=remote_created,
            layout_snapshot=(lambda: remote_snapshot)
            if remote_created
            else (lambda: None),
        )


def _persist_monkeypatch(monkeypatch):
    """_persist_layout 读两窗物理矩形：换成固定值（离线不触真实窗口）。"""
    monkeypatch.setattr(
        "renpy_overlay.stream.host_core.win32api.window_rect",
        lambda hwnd: (0, 0, 900, 400),
    )


def _base_payload_kwargs():
    return dict(
        stream_width=800,
        stream_height=200,
        stream_locked=False,
        stream_user_offset=None,
        stream_abs_rect=None,
        screenshot_windows=[],
        hotkey_mode=False,
        choice_log_open=False,
        choice_log_rect=None,
    )


def test_persist_layout_records_remote_snapshot(tmp_path, monkeypatch):
    _persist_monkeypatch(monkeypatch)
    host = _PersistHost(
        str(tmp_path),
        remote_created=True,
        remote_snapshot=(10, 20, 60, 200, 300, 80, "#ff3232"),
    )
    StreamOverlayCore._persist_layout(host)
    state = layout.load_layout(layout.layout_path_for(str(tmp_path)))
    remote = state.remote_screenshot
    assert remote is not None
    assert (remote.a.x, remote.a.y, remote.a.diameter) == (10, 20, 60)
    assert (remote.b.x, remote.b.y, remote.b.diameter) == (200, 300, 80)
    assert remote.loaded_color == "#ff3232"


def test_persist_layout_keeps_previous_remote_node_when_not_created(
    tmp_path, monkeypatch
):
    """需求三.2：本次运行未启用遥控器，不得清空文件中已有的位置记录。"""
    _persist_monkeypatch(monkeypatch)
    path = layout.layout_path_for(str(tmp_path))
    layout.save_layout(
        path,
        layout.collect_layout(
            remote_screenshot=layout.collect_remote_screenshot_layout(
                a_x=1,
                a_y=2,
                a_diameter=30,
                b_x=3,
                b_y=4,
                b_diameter=40,
                loaded_color=None,
            ),
            **_base_payload_kwargs(),
        ),
    )
    host = _PersistHost(str(tmp_path), remote_created=False, remote_snapshot=None)
    StreamOverlayCore._persist_layout(host)
    state = layout.load_layout(path)
    assert state.remote_screenshot is not None  # 旧记录保留
    assert (state.remote_screenshot.a.x, state.remote_screenshot.a.y) == (1, 2)
    assert state.remote_screenshot.b.diameter == 40


def test_persist_layout_writes_null_remote_when_never_created(tmp_path, monkeypatch):
    _persist_monkeypatch(monkeypatch)
    host = _PersistHost(str(tmp_path), remote_created=False, remote_snapshot=None)
    StreamOverlayCore._persist_layout(host)
    raw = layout.load_layout_raw(layout.layout_path_for(str(tmp_path)))
    assert raw["remote_screenshot"] is None
