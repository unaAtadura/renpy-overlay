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
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

try:
    from PyQt6.QtSvg import QSvgRenderer

    _HAS_QTSVG = True
except ImportError:  # 打包缺 QtSvg DLL 等极端场景：停顿图标降级为无图标
    QSvgRenderer = None
    _HAS_QTSVG = False

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
#: 停顿（稳定观察）旋转图标：边长 = 窗口直径 × 0.36（直径 50 → 18px、
#: 100 → 36px；矢量渲染任意直径不模糊，且小于半圆不遮中心视觉）
SETTLE_ICON_RELATIVE_SIZE = 0.36
#: 停顿图标不透明度：1.0（完全不透明）——对比度由深色衬圈保证，降透明度
#: 只会损失对比（同色系粉图标在半透明粉底上实测对比仅 1.3~2.3:1 不可辨）
SETTLE_ICON_OPACITY = 1.0
#: 停顿图标撞色亮黄（H≈50°，与主题粉 #F7A8B8 H≈348° 高亮高饱和撞色；
#: 配深色衬圈后暗/亮/白背景对比 4.5/2.5/2.3:1，均达可辨标准）
SETTLE_ICON_COLOR = "#FDE047"
#: 停顿图标深色衬圈：图标下垫半透明深蓝灰圆，把局部底压暗至与游戏背景
#: 无关的稳定区间（任意背景下图标的可辨识性不再随画面亮度剧烈波动）；
#: 直径 = 图标 × 1.4（18px 图标 → 25px 衬圈，不溢出最小窗口内切区）
SETTLE_ICON_SCRIM_COLOR = QColor(31, 36, 48, 110)
SETTLE_ICON_SCRIM_SCALE = 1.4
#: 旋转动画帧间隔与每帧步长：30fps + 12°/帧 → 1.0 秒/圈（平滑且开销极小）
SETTLE_SPIN_INTERVAL_MS = 33
SETTLE_SPIN_STEP_DEG = 12
#: 停顿图标资产（素材 stroke 为撞色亮黄 #FDE047；渲染时以 SETTLE_ICON_COLOR
#: 运行时替换 stroke，调色只需改常量；路径基准同 stream/icons.py）
SETTLE_ICON_PATH = (
    Path(__file__).parent.parent / "assets" / "icons" / "刷新_refresh.svg"
)


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
    """窗口 A（触发器）：纯色半透明圆形，可拖拽，左键单击下发截图翻译请求。

    停顿（稳定观察）视觉提示：自动化稳定窗口期间叠加主题色刷新图标并
    360° 持续循环旋转（``set_settle(True)``）；停顿结束立即停转并把角度
    复位为 0，不残留中间角度。图标为纯叠加绘制，不触碰点击/拖拽事件。
    """

    def __init__(
        self,
        diameter: int,
        on_click=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(diameter, parent)
        self._on_click = on_click
        self._settle_active = False  # 停顿提示显示中（旋转动画运行）
        self._settle_angle = 0  # 旋转角度（0-359；停顿结束复位为 0，不残留）
        self._settle_icon_pixmap: QPixmap | None = None  # 矢量渲染缓存（直径×0.36）
        self._settle_timer = QTimer(self)  # 父对象=窗口：随销毁自动停止（生命周期兜底）
        self._settle_timer.setInterval(SETTLE_SPIN_INTERVAL_MS)
        self._settle_timer.timeout.connect(self._on_settle_spin)

    def set_settle(self, active: bool) -> None:
        """停顿提示开关：True 显示旋转图标，False 停转复位隐藏（幂等）。

        图标缺失或 SVG 渲染不可用时降级为无图标（DEBUG 日志），不影响
        自动化流程与窗口交互。
        """
        active = bool(active)
        if active == self._settle_active:
            return
        if active:
            self._settle_angle = 0
            self._render_settle_icon()
            if self._settle_icon_pixmap is None:
                logger.debug("停顿图标不可用（缺失或渲染失败），降级为无图标提示")
                self._settle_active = False
                return
            self._settle_active = True
            self._settle_timer.start()
        else:
            self._settle_timer.stop()
            self._settle_angle = 0  # 复位初始角度：不残留旋转中间角度
            self._settle_active = False
        self.update()

    def _render_settle_icon(self) -> None:
        """按当前窗口尺寸矢量渲染停顿图标（QSvgRenderer；失败置 None 降级）。"""
        self._settle_icon_pixmap = None
        if not _HAS_QTSVG:
            logger.debug("QtSvg 不可用，停顿图标降级为无图标提示")
            return
        size = max(1, int(self.width() * SETTLE_ICON_RELATIVE_SIZE))
        try:
            # stroke 颜色以 SETTLE_ICON_COLOR 为单点真值（素材 #FDE047 运行时替换）
            svg_data = SETTLE_ICON_PATH.read_bytes()
            if SETTLE_ICON_COLOR != "#FDE047":
                svg_data = svg_data.replace(b"#FDE047", SETTLE_ICON_COLOR.encode("ascii"))
            renderer = QSvgRenderer(svg_data)
        except Exception as exc:  # 文件缺失等：降级无图标，不抛出
            logger.debug("停顿图标加载失败（%s）：%s", SETTLE_ICON_PATH, exc)
            return
        if not renderer.isValid():
            logger.debug("停顿图标 SVG 无效，降级为无图标提示")
            return
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(SETTLE_ICON_OPACITY)
        renderer.render(painter)
        painter.end()
        self._settle_icon_pixmap = pixmap

    def _on_settle_spin(self) -> None:
        """旋转动画帧（仅停顿期间运行）：角度步进并触发重绘。"""
        self._settle_angle = (self._settle_angle + SETTLE_SPIN_STEP_DEG) % 360
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窗口尺寸变化：重渲染停顿图标（矢量渲染，任意尺寸自适应不模糊）。"""
        super().resizeEvent(event)
        if self._settle_active:
            self._render_settle_icon()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        draw_trigger_circle(painter, self.rect(), trigger_fill_color())
        if self._settle_active and self._settle_icon_pixmap is not None:
            # 停顿提示：深色衬圈先压暗局部底（对比不随游戏背景波动），再
            # 居中旋转叠加撞色图标（纯绘制，不影响交互）
            icon_size = self._settle_icon_pixmap.width()
            scrim = int(icon_size * SETTLE_ICON_SCRIM_SCALE)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(SETTLE_ICON_SCRIM_COLOR)
            painter.drawEllipse(self.rect().center(), scrim // 2, scrim // 2)
            painter.restore()
            painter.save()
            painter.translate(self.rect().center())
            painter.rotate(self._settle_angle)
            painter.drawPixmap(-icon_size // 2, -icon_size // 2, self._settle_icon_pixmap)
            painter.restore()

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
