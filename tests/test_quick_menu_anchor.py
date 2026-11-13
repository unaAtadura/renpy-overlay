"""快捷菜单锚点窗口的离线验证（offscreen）：侧向定位纯函数 + 交互状态机。

需求（.raw_plans/future_右键快捷菜单_分离）：锚点单击松开即开合快捷菜单
（无双击延迟等待，锁定/解锁态一致）、双击切换锁定/解锁、解锁态左键按住
拖动；菜单按锚点与屏幕边缘的距离自适应选边（紧贴锚点边缘、不可覆盖锚点）。
定位/图标尺寸为纯函数直接离线验证；窗口交互经合成 QMouseEvent 驱动真实
QWidget（QT_QPA_PLATFORM=offscreen）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PyQt6.QtGui import QMouseEvent

from renpy_overlay.quick_menu_anchor import (
    CLICK_MOVE_TOLERANCE,
    CORNER_UNDERLAY_ALPHA,
    QuickMenuAnchorWindow,
    default_anchor_position,
    icon_size_for,
    menu_popup_position,
)

SCREEN = QRect(0, 0, 1920, 1080)
#: 合成事件的全局坐标偏移（与本地坐标保持固定差值，模拟屏幕上真实窗口位置）
_GLOBAL_OFFSET = 10_000


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _event(etype: QEvent.Type, x: int, y: int) -> QMouseEvent:
    """构造左键合成鼠标事件（global = local + 固定偏移）。"""
    return QMouseEvent(
        etype,
        QPointF(float(x), float(y)),
        QPointF(float(x + _GLOBAL_OFFSET), float(y + _GLOBAL_OFFSET)),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _press(window: QuickMenuAnchorWindow, x: int, y: int) -> None:
    window.mousePressEvent(_event(QEvent.Type.MouseButtonPress, x, y))


def _release(window: QuickMenuAnchorWindow, x: int, y: int) -> None:
    window.mouseReleaseEvent(_event(QEvent.Type.MouseButtonRelease, x, y))


def _move(window: QuickMenuAnchorWindow, x: int, y: int) -> None:
    window.mouseMoveEvent(_event(QEvent.Type.MouseMove, x, y))


def _double_click(window: QuickMenuAnchorWindow, x: int, y: int) -> None:
    window.mouseDoubleClickEvent(_event(QEvent.Type.MouseButtonDblClick, x, y))


# ---- 菜单侧向定位（纯函数） -----------------------------------------------------


def test_popup_right_side_when_fits():
    anchor = QRect(800, 500, 50, 50)
    pos = menu_popup_position(anchor, QSize(200, 300), SCREEN)
    assert pos.x() == anchor.right() + 1  # 右侧放得下：紧贴右缘
    assert pos.y() == anchor.top()  # 垂直与锚点顶对齐
    assert not QRect(pos, QSize(200, 300)).intersects(anchor)  # 不覆盖锚点


def test_popup_left_side_when_right_insufficient():
    anchor = QRect(1800, 500, 50, 50)  # 右侧剩余 70 < 菜单宽 200
    pos = menu_popup_position(anchor, QSize(200, 300), SCREEN)
    assert pos.x() == anchor.left() - 1 - 200  # 左侧放得下：紧贴左缘
    assert not QRect(pos, QSize(200, 300)).intersects(anchor)


def test_popup_clamped_to_side_with_more_space_when_neither_fits():
    anchor = QRect(950, 500, 50, 50)  # 右侧剩余 920 < 左侧剩余 950，均放不下
    pos = menu_popup_position(anchor, QSize(2000, 300), SCREEN)
    # 取剩余空间大的一侧（左侧）并钳入屏幕
    assert pos.x() == SCREEN.left()


def test_popup_vertical_clamped_to_screen_bottom():
    anchor = QRect(800, 1000, 50, 50)  # 锚点顶 + 菜单高 300 超出屏幕底
    pos = menu_popup_position(anchor, QSize(200, 300), SCREEN)
    assert pos.y() == SCREEN.bottom() + 1 - 300


def test_popup_vertical_clamped_to_screen_top():
    anchor = QRect(800, 0, 50, 50)
    pos = menu_popup_position(anchor, QSize(200, 1200), SCREEN)  # 菜单高于屏幕
    assert pos.y() == SCREEN.top()


def test_popup_at_left_screen_edge_opens_right():
    anchor = QRect(0, 500, 50, 50)  # 紧贴屏幕左缘：只能右侧
    pos = menu_popup_position(anchor, QSize(200, 300), SCREEN)
    assert pos.x() == anchor.right() + 1


# ---- 图标尺寸（纯函数） ---------------------------------------------------------


def test_icon_size_scales_with_diameter():
    assert icon_size_for(50) == 40
    assert icon_size_for(100) == 80
    assert icon_size_for(1) == 1  # 下限 1


def test_icon_rerendered_when_diameter_changes(qapp):
    window = QuickMenuAnchorWindow(50)
    assert window.width() == 50 and window.height() == 50
    if window._icon_pixmap is None:
        pytest.skip("QtSvg 不可用（图标降级为纯圆形底），跳过渲染断言")
    assert window._icon_pixmap.width() == icon_size_for(50)
    window.apply_layout(diameter=80, pos=(10, 20), locked=False)
    assert window.width() == 80 and window.height() == 80
    assert window._icon_pixmap.width() == icon_size_for(80)  # 随直径重渲染


# ---- layout 恢复入口 ------------------------------------------------------------


def test_apply_layout_applies_diameter_pos_locked(qapp):
    window = QuickMenuAnchorWindow(50)
    window.apply_layout(diameter=70, pos=(12, 34), locked=False)
    assert window.diameter == 70
    assert window.geometry().topLeft() == QPoint(12, 34)
    assert window.locked is False


def test_apply_layout_keeps_pos_when_none(qapp):
    window = QuickMenuAnchorWindow(50)
    window.move(QPoint(66, 88))
    window.apply_layout(diameter=50, pos=None, locked=True)
    assert window.geometry().topLeft() == QPoint(66, 88)  # 离屏回退语义：不移动
    assert window.locked is True


def test_default_anchor_position_centers_screen():
    center = SCREEN.center()
    assert default_anchor_position(SCREEN, 50) == QPoint(center.x() - 25, center.y() - 25)


# ---- 交互状态机（合成事件驱动真实 QWidget；单击松开即响应，无双击延迟） -----------


def test_default_locked_and_click_fires_immediately(qapp):
    """锁定态单击（位移在容差内）：松开立即触发菜单回调，无延迟定时器。"""
    calls: list[int] = []
    window = QuickMenuAnchorWindow(50, on_toggle_menu=lambda: calls.append(1))
    assert window.locked is True  # 默认锁定态
    _press(window, 25, 25)
    _release(window, 27, 23)  # 位移 2 ≤ 容差
    assert calls == [1]  # 松开即响应（release 调用栈内同步触发）


def test_unlocked_click_fires_immediately(qapp):
    """解锁态单击（无拖动位移）：同样立即开合快捷菜单。"""
    calls: list[int] = []
    window = QuickMenuAnchorWindow(50, on_toggle_menu=lambda: calls.append(1))
    window.toggle_lock()  # 解锁
    assert window.locked is False
    _press(window, 25, 25)
    _release(window, 25, 25)
    assert calls == [1]


def test_locked_move_over_tolerance_is_not_click(qapp):
    calls: list[int] = []
    window = QuickMenuAnchorWindow(50, on_toggle_menu=lambda: calls.append(1))
    _press(window, 25, 25)
    _release(window, 25 + CLICK_MOVE_TOLERANCE + 1, 25)
    assert calls == []  # 位移超容差：按住晃动不判定为单击


def test_locked_drag_does_not_move_window(qapp):
    window = QuickMenuAnchorWindow(50)
    origin = window.geometry().topLeft()
    _press(window, 25, 25)
    _move(window, 200, 200)
    _release(window, 200, 200)
    assert window.geometry().topLeft() == origin  # 锁定态拖动被禁用


def test_double_click_toggles_lock_without_menu_callback(qapp):
    """双击（DClick 事件）只切换锁定态，不触发菜单回调（兜底路径）。

    常规时序下双击第二击被模态菜单吞掉（见模块 docstring），锁定切换由
    宿主“菜单快速开合”判定完成；本用例驱动 DClick 兜底处理器本身。
    """
    calls: list[int] = []
    window = QuickMenuAnchorWindow(50, on_toggle_menu=lambda: calls.append(1))
    assert window.locked is True
    _double_click(window, 25, 25)
    assert window.locked is False  # 双击切换为解锁
    assert calls == []  # DClick 不触发菜单回调
    _double_click(window, 25, 25)  # 再双击切回锁定
    assert window.locked is True
    assert calls == []


def test_unlocked_drag_moves_window_without_menu(qapp):
    """解锁态按住拖动（位移超容差）：只移动锚点，不触发菜单开合。"""
    calls: list[int] = []
    window = QuickMenuAnchorWindow(50, on_toggle_menu=lambda: calls.append(1))
    window.toggle_lock()  # 解锁
    origin = window.geometry().topLeft()
    _press(window, 25, 25)
    _move(window, 65, 45)
    _release(window, 65, 45)
    moved = window.geometry().topLeft()
    assert moved == QPoint(origin.x() + 40, origin.y() + 20)  # 保持抓取点整体移动
    assert calls == []  # 拖动结束不触发菜单


# ---- 命中测试不穿透（需求：窗口内不可出现完全透明的区域） ------------------------


def test_corner_underlay_alpha_is_positive():
    # alpha=0 像素在 layered window 上命中穿透（mouse_lock_window 实测结论），
    # 全窗垫底层必须至少 1，保证圆形四角也可点击
    assert CORNER_UNDERLAY_ALPHA >= 1
