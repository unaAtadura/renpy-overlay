"""原图全尺寸查看弹窗：无边框置顶窗口，左键拖拽移动、单击任意位置关闭。

与缩略图浏览区共用同一套"位移极小的 press-release 判为单击"的容差惯例
（``CLICK_MOVE_TOLERANCE``）：拖拽移动与单击关闭互不误触。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QCloseEvent, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

logger = logging.getLogger("renpy_overlay.screenshot.viewer_window")

#: 按下—松开之间位移不超过该值（曼哈顿距离）才判为"单击关闭"，否则视为拖拽
CLICK_MOVE_TOLERANCE = 4


class ImageViewerWindow(QWidget):
    """独立原图窗口：完整显示原始截图，不受浏览区尺寸限制。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None

    def show_pixmap(self, pixmap: QPixmap) -> None:
        """显示完整原图：窗口尺寸 = 图片尺寸，超出屏幕可用区时等比缩小整图。

        缩放作用于 pixmap 本身（而非仅缩小窗口）：保证整张原图完整可见、
        不被裁剪（QLabel 固定为原尺寸时缩小窗口只会裁掉超出部分）。
        """
        target = pixmap.size()
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().adjusted(40, 40, -40, -40)
            if target.width() > available.width() or target.height() > available.height():
                target.scale(available.size(), Qt.AspectRatioMode.KeepAspectRatio)
        scaled = (
            pixmap
            if target == pixmap.size()
            else pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._label.setPixmap(scaled)
        self._label.adjustSize()
        self.resize(scaled.size())
        self.show()
        self.raise_()
        self.activateWindow()
        logger.debug(
            "原图查看弹窗已显示（原图 %dx%d，窗口 %dx%d）",
            pixmap.width(),
            pixmap.height(),
            self.width(),
            self.height(),
        )

    # ---- 左键拖拽移动 + 单击关闭 ---------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = QPoint(event.globalPosition().toPoint())
            self._drag_offset = self._press_global - self.frameGeometry().topLeft()
            event.accept()
            return
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        press_global = self._press_global
        self._press_global = None
        self._drag_offset = None
        if press_global is None:
            event.accept()
            return
        moved = event.globalPosition().toPoint() - press_global
        if moved.manhattanLength() <= CLICK_MOVE_TOLERANCE:
            # 单击任意位置（非拖拽）：关闭弹窗
            self.close()
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        event.accept()  # 真正关闭（隐藏）；单例由父窗口持有，可重复 show
