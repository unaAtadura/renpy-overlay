"""暂停帧放大查看窗：该帧图像的放大显示，拖拽移动、滚轮缩放、单击关闭。

需求（future_视频录制.txt）：暂停时在视频播放窗口左键单击放大这一帧，
按住左键拖动、滚轮放大缩小、左键单击关闭；显示层级要能够显示在截图窗口
下，确保截图识别功能可用。

层级方案：窗口本身仍属 TOPMOST 层组（对抗独占全屏被盖），显示后经
:func:`win32api.place_below` 以截图窗口为参照插入其后的 z 序位 —— 位于
游戏之上、截图窗口之下；截图窗口的周期置顶重申只抬高截图窗自身，不影响
本窗在其下的相对位置。

拖拽/单击沿用项目「位移极小的 press-release 判为单击」容差惯例
（与 :mod:`renpy_overlay.screenshot.viewer_window` 一致）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QCloseEvent, QImage, QPixmap, QWheelEvent
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

logger = logging.getLogger("renpy_overlay.video.frame_viewer")

#: 按下—松开位移不超过该值（曼哈顿距离）判为单击关闭（与原图查看窗一致）
CLICK_MOVE_TOLERANCE = 4
#: 滚轮单格缩放系数（>1 放大）
ZOOM_STEP = 1.1
#: 缩放范围：最小不小于原始尺寸的 0.2 倍，最大 8 倍
MIN_SCALE = 0.2
MAX_SCALE = 8.0


def next_scale(current: float, delta_y: int) -> float:
    """滚轮缩放的下一个倍率（纯函数，离线测试用）：上滚放大、下滚缩小。"""
    factor = ZOOM_STEP if delta_y > 0 else 1 / ZOOM_STEP
    return max(MIN_SCALE, min(MAX_SCALE, current * factor))


class FrameViewerWindow(QWidget):
    """帧放大窗（按需创建，非单例：一次放大一个实例，单击关闭销毁）。"""

    def __init__(
        self,
        image: QImage,
        *,
        lower_than=None,
        place_below_fn=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self._base_pixmap = QPixmap.fromImage(image)
        self._scale = 1.0
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drag_offset: QPoint | None = None
        self._press_global: QPoint | None = None
        #: 参照窗口 hwnd 提供者（返回截图窗口句柄列表；None/空则跳过压层）
        self._lower_than = lower_than
        #: place_below 注入点（离线测试 stub；默认 win32api.place_below）
        if place_below_fn is None:
            from .. import win32api  # 局部导入：仅 Windows 存在

            place_below_fn = win32api.place_below
        self._place_below = place_below_fn

    def show_frame(self) -> None:
        """显示放大帧：初始尺寸 = 图像尺寸（超屏等比缩小），随后压低层级。"""
        target = self._base_pixmap.size()
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().adjusted(40, 40, -40, -40)
            if target.width() > available.width() or target.height() > available.height():
                target.scale(available.size(), Qt.AspectRatioMode.KeepAspectRatio)
        pixmap = (
            self._base_pixmap
            if target == self._base_pixmap.size()
            else self._base_pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._label.setPixmap(pixmap)
        self._label.adjustSize()
        self.resize(pixmap.size())
        # 显式居中到主屏可用区：顶层窗口不 move 时位置由系统决定，落到
        # 不可预期位置会加重"看不到"的排查难度
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.move(
                available.x() + (available.width() - self.width()) // 2,
                available.y() + (available.height() - self.height()) // 2,
            )
        self.show()
        self.raise_()
        self.activateWindow()
        self._lower_z_order()
        logger.debug(
            "帧放大窗已显示（帧 %dx%d，窗口 %dx%d）",
            self._base_pixmap.width(),
            self._base_pixmap.height(),
            self.width(),
            self.height(),
        )

    def _lower_z_order(self) -> None:
        """以截图窗口为参照插入其后的 z 序位（需求：显示在截图窗口下）。"""
        if self._lower_than is None:
            return
        try:
            hwnds = [int(hwnd) for hwnd in self._lower_than() if int(hwnd)]
        except Exception:  # pragma: no cover - 窗口销毁竞态
            return
        if not hwnds:
            return
        try:
            self._place_below(int(self.winId()), hwnds[0])
        except Exception as exc:  # pragma: no cover - win32 调用失败不影响显示
            logger.warning("帧放大窗压低层级失败：%s", exc)

    # ---- 滚轮缩放 ------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept()
            return
        new_scale = next_scale(self._scale, delta)
        if new_scale == self._scale:
            event.accept()
            return
        self._scale = new_scale
        target = self._base_pixmap.size() * self._scale
        scaled = self._base_pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.adjustSize()
        self.resize(scaled.size())
        event.accept()

    # ---- 左键拖拽移动 + 单击关闭（与原图查看窗同一惯例） -----------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = QPoint(event.globalPosition().toPoint())
            self._drag_offset = self._press_global - self.frameGeometry().topLeft()
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
            self.close()  # 单击任意位置（非拖拽）：关闭
        event.accept()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        event.accept()
        self.deleteLater()  # 非单例：关闭即销毁
