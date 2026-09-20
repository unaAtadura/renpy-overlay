"""快捷键模式的框选提示覆盖层：全虚拟屏透明窗口，仅绘制彩色线框。

快捷键模式（布局锁定）期间截图窗口整体隐藏，本组件在屏幕上绘制与各
截图区域外观一致、向外偏移的彩色线框，向用户提示当前框选范围；线框
以外像素全透明，不影响游戏画面与识别截图。

关键约束：全部窗口标志在 ``show()`` 之前一次固定（含整窗穿透的
``WindowTransparentForInput``），运行时只做 show/hide 与重绘、绝不切换
标志——运行时动态切换会触发 Qt 隐藏窗口与原生样式重算，是快捷键模式
四轮穿透迭代实测无法收敛的根源（见 ``window.set_clickthrough`` docstring）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

logger = logging.getLogger("renpy_overlay.screenshot.frame_overlay")

#: 线框视觉参数：与截图窗口边框一致（pen 2px、alpha 180，见 window.paintEvent）
FRAME_PEN_WIDTH = 2
FRAME_ALPHA = 180
#: 线框相对截图区域的留缝（内沿距区域外边缘的像素）：取 3 保证含抗锯齿
#: 溢出在内、线框完全绘制在截图区域之外，不污染识别截图
FRAME_GAP_PX = 3
#: 线框中心线的外扩量 = 留缝 + 半线宽（QPen 沿矩形路径向两侧各延半宽）
FRAME_OFFSET_PX = FRAME_GAP_PX + FRAME_PEN_WIDTH // 2


def offset_frame_rect(
    rect: tuple[int, int, int, int], offset: int = FRAME_OFFSET_PX
) -> tuple[int, int, int, int]:
    """把截图区域矩形 (left, top, right, bottom) 向四周外扩 offset（纯函数）。

    返回值为线框中心线矩形：内沿距截图区域 ``offset - FRAME_PEN_WIDTH / 2``
    （即留缝），外沿再加半线宽——线框整体位于截图区域之外。
    """
    left, top, right, bottom = rect
    return (left - offset, top - offset, right + offset, bottom + offset)


class FrameOverlayLayer(QWidget):
    """全虚拟屏透明覆盖层：按快照绘制各截图区域的彩色线框（整窗穿透）。

    ``show_frames(frames)`` 的 ``frames`` 为 ``(矩形 ltrb, 边框色)`` 列表，
    矩形使用 Qt 全局逻辑坐标（与截图窗口 ``geometry()`` 同参照系，进程为
    Per-Monitor DPI Aware、逻辑与物理像素一致）；绘制时减去虚拟屏原点
    换算为覆盖层本地坐标。
    """

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
        self._frames: list[tuple[tuple[int, int, int, int], QColor]] = []

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（置顶重申周期使用）。"""
        return int(self.winId())

    def show_frames(self, frames: list[tuple[tuple[int, int, int, int], QColor]]) -> None:
        """按快照显示线框覆盖层（快照由调用方在 lock_layout 时刻生成）。"""
        self._frames = list(frames)
        self.setGeometry(self._virtual_geometry())
        self.show()
        logger.debug("框选提示覆盖层已显示（%d 个线框）", len(self._frames))

    def hide_overlay(self) -> None:
        """隐藏覆盖层（清除全部线条的视觉等效操作）。"""
        self.hide()

    @staticmethod
    def _virtual_geometry() -> QRect:
        screen = QApplication.primaryScreen()
        if screen is not None:
            return screen.virtualGeometry()
        return QRect(0, 0, 0, 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._frames:
            return
        origin = self._virtual_geometry().topLeft()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen()
        pen.setWidth(FRAME_PEN_WIDTH)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for rect, color in self._frames:
            frame_color = QColor(color)
            frame_color.setAlpha(FRAME_ALPHA)
            pen.setColor(frame_color)
            painter.setPen(pen)
            left, top, right, bottom = offset_frame_rect(rect)
            painter.drawRect(
                QRect(
                    left - origin.x(),
                    top - origin.y(),
                    right - left,
                    bottom - top,
                )
            )
