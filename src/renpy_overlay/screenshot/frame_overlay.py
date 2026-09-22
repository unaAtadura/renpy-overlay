"""快捷键模式的框选提示窗口组：每个截屏区域一个等大小穿透窗口。

快捷键模式（布局锁定）期间截图窗口整体隐藏，本组件为每个锁定的截图
区域显示一个几何完全一致的提示窗口（宽、高、位置零偏移，不外扩也不
内缩），在窗口自身内沿绘制彩色边框标示截屏范围——几何即标示范围本身，
与 :mod:`mouse_lock_indicator` 同一做法。提示窗口整窗穿透，不影响游戏
画面与鼠标交互。

截屏一致性：提示窗口与截屏抓取范围（隐藏截图窗口的 ``GetWindowRect``）
精确重合、边框画在其内沿，抓屏时若仍显示就会把线框截进识别画面。截图
翻译与 AI 对话 OCR 的既有抓屏流程经 ``QuickMenu.hide_all`` 在抓屏前把
提示窗口与截图窗口一并隐藏、经同一 DWM 结算等待后抓屏，再由 finally
收尾的 ``QuickMenu.show_all`` 恢复显示（锁定期只恢复提示窗口、不放出
截图窗口）——隐藏与恢复成对、异常路径同样恢复。

关键约束：全部窗口标志在 ``show()`` 之前一次固定（含整窗穿透的
``WindowTransparentForInput``），运行时只做 show/hide、setGeometry 与
重绘、绝不切换标志——运行时动态切换会触发 Qt 隐藏窗口与原生样式重算，
是快捷键模式四轮穿透迭代实测无法收敛的根源（见
``window.set_clickthrough`` docstring）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

logger = logging.getLogger("renpy_overlay.screenshot.frame_overlay")

#: 边框视觉参数：与截图窗口边框一致（pen 2px、alpha 180，见 window.paintEvent）
FRAME_PEN_WIDTH = 2
FRAME_ALPHA = 180


def hint_geometry(rect: tuple[int, int, int, int]) -> QRect:
    """截图区域 ltrb → 提示窗口几何：精确贴合、零偏移（纯函数）。

    输入为 Qt 全局逻辑坐标（与截图窗口 ``geometry()`` 同参照系，进程
    Per-Monitor DPI Aware、与物理像素一致），输出直接喂 ``setGeometry``；
    宽高以 ``max(1, ...)`` 钳制退化矩形。
    """
    left, top, right, bottom = (int(value) for value in rect)
    return QRect(left, top, max(1, right - left), max(1, bottom - top))


class FrameHintWindow(QWidget):
    """单个截屏区域的提示窗口：与区域等大小、整窗穿透、内沿彩色边框。"""

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
        self._color = QColor(0, 0, 0)

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（周期置顶重申使用）。"""
        return int(self.winId())

    def update_frame(self, rect: tuple[int, int, int, int], color: QColor) -> None:
        """更新几何与边框色（运行期仅此两项可变，不动窗口标志）。

        几何精确等于截屏区域（宽、高、位置零偏移，需求）；颜色取各自
        截图窗口的边框色。
        """
        self.setGeometry(hint_geometry(rect))
        self._color = QColor(color)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QColor(self._color)
        frame.setAlpha(FRAME_ALPHA)
        pen = QPen(frame)
        pen.setWidth(FRAME_PEN_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 边框画在窗口自身内沿：几何即标示范围本身，不外扩一个像素（需求）
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))


class FrameOverlayLayer:
    """框选提示窗口组管理器：为每个截屏区域维护一个等大小提示窗口。

    窗口按需复用而非整体重建（数量不足补建、多余销毁，几何与颜色就地
    更新），避免锁定瞬间的显示空档；窗口标志只在构造时固定，之后全部
    操作只有 setGeometry/show/hide。
    """

    def __init__(self) -> None:
        self._windows: list[FrameHintWindow] = []

    def show_frames(self, frames: list[tuple[tuple[int, int, int, int], QColor]]) -> None:
        """按快照显示提示窗口组（快照由调用方在 lock_layout 时刻生成）。

        ``frames`` 为 ``(矩形 ltrb, 边框色)`` 列表，矩形使用 Qt 全局逻辑
        坐标（与截图窗口 ``geometry()`` 同参照系），每个矩形对应一个与
        其精确重合的提示窗口。
        """
        while len(self._windows) < len(frames):
            self._windows.append(FrameHintWindow())
        while len(self._windows) > len(frames):
            stale = self._windows.pop()
            stale.close()
            stale.deleteLater()
        for window, (rect, color) in zip(self._windows, frames, strict=True):
            window.update_frame(rect, color)
            window.show()
        logger.debug("框选提示窗口组已显示（%d 个）", len(self._windows))

    def hide_overlay(self) -> None:
        """隐藏全部提示窗口（解锁流程的视觉清除 / 截屏前隐藏共用）。"""
        for window in self._windows:
            window.hide()

    def restore(self) -> None:
        """截屏后恢复显示（几何与颜色保持 hide 前状态，仅重新 show）。"""
        for window in self._windows:
            window.show()

    def reassert_topmost(self) -> None:
        """把全部提示窗口重新压回最顶层（对抗独占全屏被激活时的覆盖）。"""
        from .. import win32api  # 局部导入：仅 Windows 存在

        for window in self._windows:
            try:
                win32api.set_topmost(window.hwnd)
            except Exception:  # pragma: no cover - 窗口销毁竞态
                return

    def destroy(self) -> None:
        """销毁全部提示窗口（宿主退出时调用）。"""
        for window in self._windows:
            window.close()
            window.deleteLater()
        self._windows.clear()
