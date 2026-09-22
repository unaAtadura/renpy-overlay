"""锁鼠标区域锁定态的提示窗口：等大小鼠标穿透窗口，精确绘制范围边框。

需求（future_限制鼠标范围）：范围窗口双击锁定后隐藏，用等大小的鼠标
穿透窗口替换，边框同色；与快捷键模式的线框覆盖层（frame_overlay）不同
—— 这里必须准确显示框选范围，不可外扩增加像素，故边框画在本窗口自身
内沿，几何即限制范围本身。

关键约束（与 :mod:`frame_overlay` 同源，快捷键模式四轮穿透迭代的结论）：
全部窗口标志在 ``show()`` 之前一次固定（含整窗穿透的
``WindowTransparentForInput``），运行时只做 show/hide 与重绘、绝不切换
标志 —— 运行时动态切换会触发 Qt 隐藏窗口与原生样式重算，穿透状态无法
收敛。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .mouse_lock_window import BORDER_COLOR

logger = logging.getLogger("renpy_overlay.screenshot.mouse_lock_indicator")

#: 边框视觉参数：与范围窗口边框一致（pen 2px、alpha 180）
BORDER_PEN_WIDTH = 2
BORDER_ALPHA = 180


class MouseLockIndicatorWindow(QWidget):
    """锁定态范围提示窗口：与限制区域等大小、整窗穿透、仅绘制灰色边框。"""

    def __init__(self) -> None:
        super().__init__(None)
        # 标志必须在 show 之前一次固定（见模块 docstring），此后绝不切换
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（周期置顶重申使用）。"""
        return int(self.winId())

    def show_region(self, rect: tuple[int, int, int, int]) -> None:
        """显示与限制区域等大小的提示窗口（rect 为 Qt 全局逻辑坐标 ltrb）。

        ``setGeometry`` 只认 Qt 全局逻辑坐标 —— DPR≠1 的屏幕上与物理像素
        数值并不相等（进程 Per-Monitor DPI Aware 只保证系统 API 拿到物理
        值，不等于 Qt 逻辑坐标）。控制器从范围窗口 ``geometry()`` 取本
        矩形，与喂给 ClipCursor 的 ``GetWindowRect`` 物理矩形各归各的坐标
        系，渲染后与限制范围精确重合于同一物理区域；边框画在自身内沿，
        无任何外扩偏移（需求）。历史上把物理值当逻辑值喂本方法，导致
        提示窗放大 DPR 倍并右下偏移（见 logs/renpy_overlay_20260922_211428.log）。
        """
        left, top, right, bottom = (int(value) for value in rect)
        self.setGeometry(
            QRect(left, top, max(1, right - left), max(1, bottom - top))
        )
        self.show()
        logger.debug("锁鼠标范围提示窗口已显示（区域 %r）", (left, top, right, bottom))

    def hide_indicator(self) -> None:
        """隐藏提示窗口（解锁流程的视觉清除操作）。"""
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = QColor(BORDER_COLOR)
        border.setAlpha(BORDER_ALPHA)
        pen = QPen(border)
        pen.setWidth(BORDER_PEN_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 边框画在窗口自身内沿：几何即限制范围本身，不外扩一个像素（需求）
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
