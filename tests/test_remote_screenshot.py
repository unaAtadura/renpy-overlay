"""遥控截图控制器的离线验证：纯函数 + stub 注入，不构造真实 QWidget。

覆盖：矩形相交判定、装填决策（首个进入驻留、离开恢复圆形、多区域忽略、
目标失效重选）、快捷键模式开/关的窗口生成与销毁（配置开关、幂等、未创建
安全）、A 单击分发（已装填 → 宿主 trigger 调用一次；未装填 → 忽略）、
截图期间隐藏/恢复与 shutdown 的 no-op 安全、初始摆放（左右并排）。

在途 API 互斥由宿主 request_screenshot_translation 负责（同一时刻只允许
一个在途），控制器不重复实现——故此处无互斥相关用例。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QColor

from renpy_overlay.screenshot.remote_screenshot import (
    INITIAL_GAP,
    RemoteScreenshotController,
    rects_intersect,
    resolve_armed,
)

#: 摆放参照点（注入的屏幕中心源返回值）
_CENTER = (800, 450)


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """控制器不构造真实 QWidget：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


# ---- stub：窗口与几何 -----------------------------------------------------------


class _FakeGeometry:
    """geometry() 返回值的鸭子 stub（Qt QRect 风格只读访问器）。"""

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        self._rect = (x, y, w, h)

    def x(self) -> int:
        return self._rect[0]

    def y(self) -> int:
        return self._rect[1]

    def width(self) -> int:
        return self._rect[2]

    def height(self) -> int:
        return self._rect[3]


class _StubWindow:
    """A/B 或截图窗口的 stub：几何 + show/hide/close/deleteLater 记录。"""

    def __init__(self, rect: tuple[int, int, int, int], border: QColor | None = None):
        self._geo = _FakeGeometry(*rect)
        self.border_color = border
        self.calls: list[str] = []
        self.on_event = None  # 工厂注入的事件回调（A 单击 / B 拖动）
        self.armed = "unset"  # set_armed 未被调用过的哨兵

    def geometry(self) -> _FakeGeometry:
        return self._geo

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def close(self) -> None:
        self.calls.append("close")

    def deleteLater(self) -> None:
        self.calls.append("deleteLater")

    def move(self, x: int, y: int) -> None:
        self._geo = _FakeGeometry(x, y, self._geo.width(), self._geo.height())

    def set_armed(self, color: QColor | None) -> None:
        self.armed = color


class _Factory:
    """stub 工厂：记录 kind/diameter，产出直径即尺寸的 stub 窗口。"""

    def __init__(self) -> None:
        self.kinds: list[tuple[str, int]] = []
        self.created: list[_StubWindow] = []

    def __call__(self, kind: str, diameter: int, on_event) -> _StubWindow:
        self.kinds.append((kind, diameter))
        window = _StubWindow((0, 0, diameter, diameter))
        window.on_event = on_event
        self.created.append(window)
        return window


def _make_controller(enable=True, locked=None, factory=None, trigger=None):
    locked_windows = list(locked or [])
    controller = RemoteScreenshotController(
        enable=enable,
        diameter_a=100,
        diameter_b=100,
        locked_windows_provider=lambda: locked_windows,
        trigger=trigger,
        window_factory=factory,
        screen_center_provider=lambda: _CENTER,
    )
    return controller, locked_windows


# ---- rects_intersect：矩形相交判定（纯函数） ------------------------------------


def test_rects_intersect_overlapping_and_containing():
    assert rects_intersect((0, 0, 10, 10), (5, 5, 10, 10))
    assert rects_intersect((0, 0, 10, 10), (0, 0, 10, 10))  # 重合
    assert rects_intersect((0, 0, 20, 20), (5, 5, 3, 3))  # 包含


def test_rects_intersect_edge_touch_or_apart_not_intersect():
    assert not rects_intersect((0, 0, 10, 10), (10, 0, 10, 10))  # 仅贴边
    assert not rects_intersect((0, 0, 10, 10), (0, 10, 10, 10))
    assert not rects_intersect((0, 0, 10, 10), (20, 20, 5, 5))  # 相离


# ---- resolve_armed：装填决策（纯函数） ------------------------------------------


def test_resolve_armed_empty_or_no_hit_returns_none():
    assert resolve_armed((0, 0, 10, 10), [], None) is None
    window = object()
    assert resolve_armed((0, 0, 10, 10), [((50, 50, 10, 10), window)], None) is None


def test_resolve_armed_first_candidate_wins():
    first, second = object(), object()
    candidates = [((0, 0, 10, 10), first), ((5, 5, 10, 10), second)]
    assert resolve_armed((0, 0, 10, 10), candidates, None) is first


def test_resolve_armed_current_stays_ignoring_others():
    """驻留语义（需求）：current 仍相交时不响应后续进入的区域。"""
    current, other = object(), object()
    candidates = [((0, 0, 10, 10), current), ((0, 0, 10, 10), other)]
    assert resolve_armed((0, 0, 10, 10), candidates, current) is current


def test_resolve_armed_leave_then_enter_next_or_none():
    """离开驻留区域后允许进入下一个；全部离开恢复 None（圆形）。"""
    current, other = object(), object()
    candidates = [((0, 0, 10, 10), current), ((100, 100, 10, 10), other)]
    assert resolve_armed((100, 100, 10, 10), candidates, current) is other
    assert resolve_armed((200, 200, 10, 10), candidates, current) is None


def test_resolve_armed_stale_current_reselects():
    """current 不在候选（目标失效）→ 直接按锁定序重新选择。"""
    stale, active = object(), object()
    assert resolve_armed((0, 0, 10, 10), [((0, 0, 10, 10), active)], stale) is active


# ---- 控制器：快捷键模式生命周期 --------------------------------------------------


def test_mode_on_creates_windows_once():
    factory = _Factory()
    controller, _ = _make_controller(factory=factory)
    controller.on_hotkey_mode_changed(True)
    assert factory.kinds == [("A", 100), ("B", 100)]
    a, b = factory.created
    assert a.calls == ["show"] and b.calls == ["show"]
    controller.on_hotkey_mode_changed(True)  # 幂等：重复通知不重建
    assert len(factory.created) == 2


def test_mode_on_places_windows_side_by_side():
    factory = _Factory()
    controller, _ = _make_controller(factory=factory)
    controller.on_hotkey_mode_changed(True)
    a, b = factory.created
    total = a.geometry().width() + INITIAL_GAP + b.geometry().width()
    assert a.geometry().x() == _CENTER[0] - total // 2
    assert b.geometry().x() == a.geometry().x() + a.geometry().width() + INITIAL_GAP
    assert a.geometry().y() == _CENTER[1] - a.geometry().height() // 2
    assert b.geometry().y() == _CENTER[1] - b.geometry().height() // 2


def test_mode_off_destroys_windows_idempotent():
    factory = _Factory()
    controller, _ = _make_controller(factory=factory)
    controller.on_hotkey_mode_changed(True)
    a, b = factory.created
    controller.on_hotkey_mode_changed(False)
    assert a.calls == ["show", "close", "deleteLater"]
    assert b.calls == ["show", "close", "deleteLater"]
    controller.on_hotkey_mode_changed(False)  # 幂等
    assert a.calls == ["show", "close", "deleteLater"]


def test_mode_off_without_create_is_safe():
    controller, _ = _make_controller()
    controller.on_hotkey_mode_changed(False)  # 未创建时 no-op，不抛异常
    controller.shutdown()


def test_enable_false_creates_nothing():
    factory = _Factory()
    controller, _ = _make_controller(enable=False, factory=factory)
    controller.on_hotkey_mode_changed(True)
    assert factory.created == []


# ---- 控制器：装填状态与触发分发 --------------------------------------------------


def test_arm_flow_and_trigger():
    factory = _Factory()
    red = QColor(255, 50, 50)
    locked_window = _StubWindow((100, 100, 300, 200), border=red)
    triggered: list[object] = []
    controller, _ = _make_controller(
        factory=factory, locked=[locked_window], trigger=triggered.append
    )
    controller.on_hotkey_mode_changed(True)
    a, b = factory.created
    b.on_event()  # B 尚未进入任何锁定区域：状态不变（不调 set_armed）
    assert b.armed == "unset"
    b.move(150, 150)  # B 拖进红色锁定区域
    b.on_event()
    assert b.armed == red  # 正方形形态，边框色 = 目标区域色
    a.on_event()  # A 单击 → trigger 收到装填的目标截图窗口
    assert triggered == [locked_window]


def test_trigger_without_arm_ignored():
    factory = _Factory()
    triggered: list[object] = []
    controller, _ = _make_controller(factory=factory, trigger=triggered.append)
    controller.on_hotkey_mode_changed(True)
    a, _b = factory.created
    a.on_event()  # 未装填：忽略截图翻译请求（需求），trigger 不被调用
    assert triggered == []


def test_arm_cleared_after_target_gone():
    """装填目标从锁定列表消失 → 解除装填恢复圆形。"""
    factory = _Factory()
    red = QColor(255, 50, 50)
    locked_window = _StubWindow((100, 100, 300, 200), border=red)
    controller, locked = _make_controller(factory=factory, locked=[locked_window])
    controller.on_hotkey_mode_changed(True)
    _a, b = factory.created
    b.move(150, 150)
    b.on_event()
    assert b.armed == red
    locked.clear()
    b.on_event()
    assert b.armed is None


# ---- 控制器：隐藏/恢复与 shutdown ------------------------------------------------


def test_hide_show_windows_roundtrip():
    factory = _Factory()
    controller, _ = _make_controller(factory=factory)
    controller.hide_windows()  # 未创建时 no-op
    controller.show_windows()
    controller.on_hotkey_mode_changed(True)
    a, b = factory.created
    controller.hide_windows()
    assert a.calls[-1] == "hide" and b.calls[-1] == "hide"
    controller.show_windows()
    assert a.calls[-1] == "show" and b.calls[-1] == "show"


def test_shutdown_destroys_windows_idempotent():
    factory = _Factory()
    controller, _ = _make_controller(factory=factory)
    controller.on_hotkey_mode_changed(True)
    a, _b = factory.created
    controller.shutdown()
    assert a.calls == ["show", "close", "deleteLater"]
    controller.shutdown()  # 幂等
    assert a.calls == ["show", "close", "deleteLater"]
