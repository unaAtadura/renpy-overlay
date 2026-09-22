"""锁鼠标区域功能的范围框选窗口：灰色边框 + 八向拉伸 + 双击锁定生效。

需求（future_限制鼠标范围）：窗口化运行游戏时把鼠标活动范围限制在游戏
画面内，防止鼠标点出游戏窗口。本窗口是框选阶段的载体 —— 拖拽摆放、
八向拉伸确定范围，双击后由 :class:`~renpy_overlay.screenshot.mouse_lock.
MouseLockController` 隐藏本窗口、以等大小穿透提示窗口替换并对区域施加
Win32 ``ClipCursor``。

视觉与交互参考截图窗口（``window.ScreenshotWindow``）：边缘检测、四角
手柄、悬停高亮与拉伸尺寸标签复用 :mod:`window_visual`；差异点：

- 边框固定灰色 #808080（需求），alpha 180 与截图窗口同强度；
- 内部画一层与截图窗口同惯例的极低 alpha 遮罩（:data:`MASK_COLOR`）：
  Windows 对 layered window（``WA_TranslucentBackground``）的命中测试是
  逐像素的 —— alpha=0 的像素直接把鼠标落到下层窗口，内部完全透明会让
  双击锁定与内部拖动全部失效（仅边缘因边框像素可拉伸的实测缺陷）；
  alpha 20 观感与截图窗口一致，不构成可感知压暗；
- 双击即锁定生效（回调 ``on_lock``），没有锁定态单击翻译语义。

与截图布局锁定的边界（需求约束）：本窗口由 MouseLockController 持有，
不进入 QuickMenu 截图窗口池，``lock_layout`` / ``unlock_layout`` /
``locked_windows`` 三组接口不会触及。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .window_visual import (
    draw_edge_highlight,
    draw_grip_handles,
    draw_size_label,
    edge_at,
)

logger = logging.getLogger("renpy_overlay.screenshot.mouse_lock_window")

#: 边框颜色（需求：灰色 #808080），alpha 180 与截图窗口边框同强度
BORDER_COLOR = QColor(128, 128, 128)

#: 内部遮罩（与截图窗口遮罩同惯例的极低 alpha）：alpha 必须大于 0 ——
#: Windows layered window 对 alpha=0 像素命中穿透，全透明内部会让双击
#: 锁定与内部拖动失效；锁定后本窗口隐藏，遮罩无视觉代价
MASK_COLOR = QColor(0, 0, 0, 20)


class MouseLockRegionWindow(QWidget):
    """锁鼠标区域的框选窗口：灰边框 + 内部拖动 + 八向拉伸 + 双击锁定。"""

    def __init__(self, on_lock=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_lock = on_lock

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.resize(800, 600)
        self.setMinimumSize(80, 60)

        self._dragging = False
        self._resizing = False
        self._resize_edge: str | None = None
        self._drag_pos = QPoint()
        self._hovered_edge: str | None = None

        # 启用鼠标追踪：悬停（未按下）状态下也触发 mouseMoveEvent 做边缘检测
        self.setMouseTracking(True)

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（控制器用 ``win32api.window_rect`` 取物理像素范围）。"""
        return int(self.winId())

    @property
    def border_color(self) -> QColor:
        return QColor(BORDER_COLOR)

    # ---- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # 半透明黑色遮罩（与截图窗口同惯例）：不仅轻微圈出范围，更是命中
        # 测试的前提 —— 内部像素 alpha=0 时 Windows 把鼠标直接落到下层窗口
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(MASK_COLOR))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        # 灰色边框（alpha 180：与截图窗口边框同强度）
        border = QColor(BORDER_COLOR)
        border.setAlpha(180)
        pen = QPen(border)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        draw_grip_handles(painter, rect)
        if self._hovered_edge is not None and not self._resizing:
            draw_edge_highlight(painter, rect, self._hovered_edge)
        if self._resizing:
            draw_size_label(painter, rect, self.width(), self.height())

    # ---- 鼠标事件：拖拽 / 拉伸 / 双击锁定 -----------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        pos = event.position().toPoint()
        edge = edge_at(self, pos)
        if edge:
            self._resizing = True
            self._resize_edge = edge
            self._drag_pos = QPoint(event.globalPosition().toPoint())
        else:
            self._dragging = True
            # 顶层窗口：全局坐标 - 左上角 = 抓取点偏移
            self._drag_pos = event.globalPosition().toPoint() - self.geometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self._drag_pos = event.globalPosition().toPoint()
            self._do_resize(delta)
            event.accept()
            return
        if self._dragging:
            # 顶层窗口的 move() 即屏幕坐标：保持抓取点相对位置不变整体移动
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        self._sync_hover_edge(event.position().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_resizing = self._resizing
        self._dragging = False
        self._resizing = False
        self._resize_edge = None
        if was_resizing:
            # 拉伸结束后重算悬停边缘并重绘（清除尺寸标签）
            self._sync_hover_edge(self.mapFromGlobal(event.globalPosition().toPoint()))
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        logger.info("锁鼠标区域窗口已双击：请求锁定生效")
        if callable(self._on_lock):
            self._on_lock(self)
        event.accept()

    # ---- 拉伸与边缘检测（视觉细节在 window_visual 模块） ---------------------

    def _sync_hover_edge(self, pos: QPoint) -> None:
        edge = edge_at(self, pos)
        self._update_cursor_for_edge(edge)
        if edge != self._hovered_edge:
            self._hovered_edge = edge
            self.update()

    def _update_cursor_for_edge(self, edge: str | None) -> None:
        cursors = {
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
            "n": Qt.CursorShape.SizeVerCursor,
            "s": Qt.CursorShape.SizeVerCursor,
            "w": Qt.CursorShape.SizeHorCursor,
            "e": Qt.CursorShape.SizeHorCursor,
        }
        self.setCursor(cursors.get(edge, Qt.CursorShape.ArrowCursor))

    def _do_resize(self, delta: QPoint) -> None:
        """按拖动增量执行八向拉伸（与截图窗口 _do_resize 同逻辑）。"""
        edge = self._resize_edge
        if edge is None:
            return
        geo = self.geometry()
        new_x, new_y = geo.x(), geo.y()
        new_w, new_h = geo.width(), geo.height()
        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        dx, dy = delta.x(), delta.y()
        if "e" in edge:
            new_w = max(min_w, geo.width() + dx)
        if "s" in edge:
            new_h = max(min_h, geo.height() + dy)
        if "w" in edge:
            new_w = max(min_w, geo.width() - dx)
            new_x = geo.x() + geo.width() - new_w
        if "n" in edge:
            new_h = max(min_h, geo.height() - dy)
            new_y = geo.y() + geo.height() - new_h
        self.setGeometry(new_x, new_y, new_w, new_h)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._hovered_edge is not None:
            self._hovered_edge = None
            self.update()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._sync_hover_edge(self.mapFromGlobal(event.globalPosition().toPoint()))
        super().enterEvent(event)
