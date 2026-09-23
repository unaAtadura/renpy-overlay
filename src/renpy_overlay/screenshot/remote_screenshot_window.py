"""遥控截图窗口 A/B：快捷键模式期间存在的小圆形触发/瞄准悬浮窗（PyQt6）。

需求（future_遥控截图功能）：点击截图时截图区域过分遮挡屏幕、快捷键截图
又完全无法用鼠标操作，本功能提供折中的第三种截图方式 —— 两个可独立拖动
的小圆形窗口：

- 窗口 A（触发器）：无边框纯色半透明圆形（#F7A8B8），可拖拽；左键单击
  下发截图翻译请求（单击与拖拽以位移容差区分，无双击行为故无需延迟判定）；
- 窗口 B（瞄准器）：有外边框（#F7A8B8）、内部黑色半透明蒙版的圆形，可
  拖拽；拖入已双击锁定的截图区域变为正方形（边长 = 圆形直径、中心点重合
  → 包围盒几何不变，仅切换绘制），边框色变为该截图区域窗口的颜色，即
  "装填"完成可接受触发；离开恢复圆形。

包围盒命中：圆形窗口四角透明像素仍按矩形接收鼠标事件（Qt 顶层窗口按包围
盒做命中测试），拖拽热区即包围盒。

窗口标志与截图窗口一致（无边框、置顶、Tool、透明背景、不抢焦点）；显示
一律走 Qt ``show()``（透明悬浮窗经 win32 SWP_SHOWWINDOW 显示有残影陷阱）。
仅 Qt 主线程使用。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

logger = logging.getLogger("renpy_overlay.screenshot.remote_screenshot_window")

#: 遥控截图主题色（需求：#F7A8B8，窗口 A 填充与窗口 B 圆形态边框共用）
REMOTE_COLOR = QColor(247, 168, 184)
#: 窗口 A 纯色填充的透明度（需求仅要求半透明，取 50%）
TRIGGER_FILL_ALPHA = 128
#: 窗口 B 边框透明度（与截图窗口边框一致的可视强度）
ARM_BORDER_ALPHA = 180
#: 窗口 B 内部黑色半透明蒙版（与截图窗口蒙版一致）
ARM_MASK_COLOR = QColor(0, 0, 0, 20)
#: 判定"点击"的最大位移（像素）：超过视为拖动（与截图窗口同容差）
CLICK_MOVE_TOLERANCE = 4


def trigger_fill_color() -> QColor:
    """窗口 A 的纯色填充色（含半透明 alpha）。"""
    color = QColor(REMOTE_COLOR)
    color.setAlpha(TRIGGER_FILL_ALPHA)
    return color


def draw_trigger_circle(painter: QPainter, rect, fill: QColor) -> None:
    """窗口 A 圆形态绘制：无边框纯色半透明内切圆。"""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(fill))
    painter.drawEllipse(rect.adjusted(1, 1, -1, -1))


def _arm_pen(border: QColor) -> QPen:
    """窗口 B 的边框画笔（统一线宽与透明度）。"""
    color = QColor(border)
    color.setAlpha(ARM_BORDER_ALPHA)
    pen = QPen(color)
    pen.setWidth(2)
    return pen


def draw_arm_circle(painter: QPainter, rect, border: QColor) -> None:
    """窗口 B 圆形态绘制：黑色半透明蒙版内圆 + 主题色外边框。"""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    inner = rect.adjusted(1, 1, -1, -1)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ARM_MASK_COLOR))
    painter.drawEllipse(inner)
    painter.setPen(_arm_pen(border))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(inner)


def draw_arm_square(painter: QPainter, rect, border: QColor) -> None:
    """窗口 B 正方形形态绘制：边长 = 直径、中心重合（即整个包围盒内缩一线宽）。

    需求"正方形边长为圆形直径，正方形中心点与原圆形中心点重合"在包围盒
    坐标下等价于以包围盒为正方形绘制，窗口几何尺寸不变。
    """
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    inner = rect.adjusted(1, 1, -1, -1)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ARM_MASK_COLOR))
    painter.drawRect(inner)
    painter.setPen(_arm_pen(border))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(inner)


class _RemoteWindow(QWidget):
    """遥控截图小圆窗公共基类：固定尺寸 + 按住内部拖拽移动。"""

    def __init__(self, diameter: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(diameter, diameter)
        self._dragging = False
        self._drag_offset = QPoint()
        self._press_global: QPoint | None = None

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（控制器周期性重申置顶用）。"""
        return int(self.winId())

    # ---- 拖拽（与截图窗口同模式：顶层窗口 move 即屏幕坐标，保持抓取点不动） ----

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        self._dragging = True
        self._press_global = QPoint(event.globalPosition().toPoint())
        self._drag_offset = event.globalPosition().toPoint() - self.geometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._after_drag_move()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        press_global = self._press_global
        self._press_global = None
        if self._dragging:
            self._dragging = False
            self._on_release(press_global, event)
        event.accept()

    def _on_release(self, press_global: QPoint | None, event) -> None:
        """释放收尾：基类无额外语义，子类按需覆写。"""

    def _after_drag_move(self) -> None:
        """拖拽移动后的钩子：子类按需覆写。"""


class RemoteTriggerWindow(_RemoteWindow):
    """窗口 A（触发器）：纯色半透明圆形，可拖拽，左键单击下发截图翻译请求。"""

    def __init__(
        self,
        diameter: int,
        on_click=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(diameter, parent)
        self._on_click = on_click

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        draw_trigger_circle(painter, self.rect(), trigger_fill_color())

    def _on_release(self, press_global: QPoint | None, event) -> None:
        if press_global is None:
            return
        moved = event.globalPosition().toPoint() - press_global
        if abs(moved.x()) > CLICK_MOVE_TOLERANCE or abs(moved.y()) > CLICK_MOVE_TOLERANCE:
            return  # 移动过 = 拖动，不算单击
        logger.debug("遥控截图窗口 A 单击：下发截图翻译请求")
        if callable(self._on_click):
            self._on_click()


class RemoteArmWindow(_RemoteWindow):
    """窗口 B（瞄准器）：主题色边框 + 黑色蒙版圆形；拖入已锁定截图区域变正方形。

    形态切换不改几何（正方形边长 = 圆形直径且中心重合，包围盒尺寸不变），
    仅切换绘制与边框色：``armed_color`` 为 None 时圆形形态（主题色边框），
    非 None 时正方形形态（目标截图区域窗口的边框色）。
    """

    def __init__(
        self,
        diameter: int,
        on_drag_move=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(diameter, parent)
        self._on_drag_move = on_drag_move
        self._armed_color: QColor | None = None

    @property
    def armed_color(self) -> QColor | None:
        return QColor(self._armed_color) if self._armed_color is not None else None

    def set_armed(self, color: QColor | None) -> None:
        """切换圆形/正方形形态（None = 圆形）；状态不变则不重绘。"""
        if color is None and self._armed_color is None:
            return
        if (
            color is not None
            and self._armed_color is not None
            and QColor(color) == self._armed_color
        ):
            return
        self._armed_color = QColor(color) if color is not None else None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        if self._armed_color is None:
            draw_arm_circle(painter, self.rect(), REMOTE_COLOR)
        else:
            draw_arm_square(painter, self.rect(), self._armed_color)

    def _after_drag_move(self) -> None:
        if callable(self._on_drag_move):
            self._on_drag_move()
