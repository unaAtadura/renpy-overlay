"""单个截图窗口：半透明识别框风格的可拖拽 / 八向拉伸悬浮窗（PyQt6）。

视觉与交互移植自参考实现 ``screen_translaor_4.0/recognize_window.py``：
半透明黑色遮罩 + 彩色边框（创建顺序分配红橙黄绿青蓝紫黑）+ 鼠标靠近可
拉伸边缘时亮白反馈 + 四角手柄 + 拉伸中的实时尺寸标签。按需求裁剪：
无识别按钮、不显示识别框 ID。

交互约定（与流式悬浮窗一致）：
- 未锁定：左键按住内部拖动 / 边缘八向拉伸；
- 双击：仅切换自身锁定（不通知宿主 —— 双击解锁截图窗口不打断截图翻译）；
- 锁定：禁止拖动，内部单击经延迟判定（等一个系统双击间隔）后回调
  ``on_click``，由 :class:`~renpy_overlay.quick_menu.QuickMenu` 转发宿主
  触发截图翻译。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .. import win32api
from .window_visual import draw_edge_highlight, draw_grip_handles, draw_size_label, edge_at

logger = logging.getLogger("renpy_overlay.screenshot.window")

#: 单击延迟判定：等一个系统双击间隔再认定"单击"（双击会取消），与宿主同模式
CLICK_DELAY_MS = win32api.double_click_time_ms() + 60
#: 判定"点击"的最大位移（像素）：超过视为拖动/拉伸
CLICK_MOVE_TOLERANCE = 4


class ScreenshotWindow(QWidget):
    """一个截图区域框：彩色边框 + 八向拉伸 + 锁定态单击触发截图翻译。"""

    def __init__(
        self,
        index: int,
        border_color: QColor,
        on_click=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._border_color = QColor(border_color)
        self._on_click = on_click

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.resize(300, 200)
        self.setMinimumSize(80, 60)

        self._locked = False
        self._dragging = False
        self._resizing = False
        self._resize_edge: str | None = None
        self._drag_offset = QPoint()
        self._drag_pos = QPoint()
        self._hovered_edge: str | None = None
        self._press_pos: QPoint | None = None
        self._press_global: QPoint | None = None

        # 启用鼠标追踪：悬停（未按下）状态下也触发 mouseMoveEvent 做边缘检测
        self.setMouseTracking(True)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(CLICK_DELAY_MS)
        self._click_timer.timeout.connect(self._on_delayed_click)

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（宿主用 ``win32api.window_rect`` 取物理像素截图区域）。"""
        return int(self.winId())

    @property
    def index(self) -> int:
        return self._index

    @property
    def border_color(self) -> QColor:
        return QColor(self._border_color)

    # ---- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # 半透明黑色遮罩（与参考实现一致：轻微压暗 + 不完全遮挡截图区域）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 20))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        # 彩色边框（alpha 180：与参考实现一致的可视强度）
        border = QColor(self._border_color)
        border.setAlpha(180)
        pen = QPen(border)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        if self._locked:
            return  # 锁定态：不绘制手柄与边缘高亮，保持画面干净

        draw_grip_handles(painter, rect)
        if self._hovered_edge is not None and not self._resizing:
            draw_edge_highlight(painter, rect, self._hovered_edge)
        if self._resizing:
            draw_size_label(painter, rect, self.width(), self.height())

    # ---- 鼠标事件：拖拽 / 拉伸 / 延迟单击 / 双击自锁 -------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        pos = event.position().toPoint()
        self._press_pos = QPoint(pos)
        self._press_global = QPoint(event.globalPosition().toPoint())
        if self._locked:
            # 锁定态：边缘仍可拉伸，内部点击交给延迟判定触发截图翻译
            edge = edge_at(self, pos)
            if edge:
                self._start_resize(edge, event.globalPosition().toPoint())
            event.accept()
            return
        edge = edge_at(self, pos)
        if edge:
            self._start_resize(edge, event.globalPosition().toPoint())
        else:
            self._dragging = True
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
        if self._locked:
            event.accept()
            return
        self._sync_hover_edge(event.position().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_resizing = self._resizing
        self._dragging = False
        self._resizing = False
        self._resize_edge = None
        press_global = self._press_global
        self._press_pos = None
        self._press_global = None
        if was_resizing:
            # 拉伸结束后重算悬停边缘并重绘（清除尺寸标签）
            self._sync_hover_edge(self.mapFromGlobal(event.globalPosition().toPoint()))
            event.accept()
            return
        if press_global is None:
            event.accept()
            return
        moved = event.globalPosition().toPoint() - press_global
        if abs(moved.x()) > CLICK_MOVE_TOLERANCE or abs(moved.y()) > CLICK_MOVE_TOLERANCE:
            event.accept()
            return  # 移动过 = 拖动，不算单击
        if self._locked and not self._click_timer.isActive():
            self._click_timer.start()  # 延迟判定：双击会取消该任务
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._click_timer.stop()  # 双击取消单击判定
        if self._dragging:
            self._dragging = False
        self._toggle_lock()
        event.accept()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        if self._locked:
            self._dragging = False
            self._resizing = False
            self._resize_edge = None
            self._hovered_edge = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            logger.info("截图窗口 #%d 已锁定（单击触发截图翻译；双击解锁）", self._index)
        else:
            logger.info("截图窗口 #%d 已解锁（恢复拖动与拉伸）", self._index)
        self.update()

    def _on_delayed_click(self) -> None:
        if not self._locked:
            return
        logger.debug("截图窗口 #%d 锁定态单击：请求截图翻译", self._index)
        if callable(self._on_click):
            self._on_click(self)

    # ---- 拉伸与边缘检测（视觉细节在 window_visual 模块） ---------------------

    def _start_resize(self, edge: str, global_pos: QPoint) -> None:
        self._resizing = True
        self._resize_edge = edge
        self._drag_pos = QPoint(global_pos)

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
        """按拖动增量执行八向拉伸（参考实现 _do_resize 的等价移植）。"""
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
        if not self._locked:
            self._sync_hover_edge(self.mapFromGlobal(event.globalPosition().toPoint()))
        super().enterEvent(event)

    # ---- 供 QuickMenu 管理的辅助 -------------------------------------------

    def reposition_cascade(self, center: QPoint, cascade_step: int = 30) -> None:
        """把窗口摆到 ``center`` 为中心的级联位置（第 index 个偏移一步）。"""
        w, h = self.width(), self.height()
        offset = self._index * cascade_step
        self.move(center.x() - w // 2 + offset, center.y() - h // 2 + offset)
