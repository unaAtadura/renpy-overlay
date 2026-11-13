"""快捷菜单锚点窗口：独立圆形半透明悬浮钮，承载快捷菜单的开关入口。

需求（.raw_plans/future_右键快捷菜单_分离）：流式输出窗口与截图区域同时
居中时，右键快捷菜单容易误入截图区域干扰识别，故把快捷菜单从流式双窗上
拆出 —— 标题/正文窗不再响应右键，改由本锚点承载：左键单击松开即打开/关闭
快捷菜单，双击在锁定/解锁两态间切换，解锁态可左键按住拖动位置。

视觉与交互惯例：

- 窗口标志与遥控截图小圆窗（``remote_screenshot_window``）一致：无边框、
  置顶、Tool、透明背景、不抢焦点（``WA_ShowWithoutActivating``）；仅 Qt
  主线程使用，显示一律走 Qt ``show()``；
- 全窗口矩形垫一层 alpha≥1 的极低透明底：Windows 对 layered window
  （``WA_TranslucentBackground``）的命中测试是逐像素的，alpha=0 的像素
  直接把鼠标落到下层窗口（mouse_lock_window 同款实测结论）——垫层保证
  圆形四角不存在完全透明区域，点击不会穿透到下层游戏窗口；
- 图标经 QSvgRenderer 矢量渲染 ``assets/icons/设置_setting.svg``（保留
  素材原 #333 描边，即「保持现有颜色风格」），窗口尺寸变化时按比例重渲染；
  QtSvg 缺失/加载失败降级为纯圆形底（不影响交互）；
- 单击判定只看位移容差：press 与 release 位移均在容差内即单击，松开立即
  触发菜单开合（不做双击延迟等待 —— 取舍：优先单击即时性，双击的第二击
  会被模态菜单作为“菜单外点击”吞掉并关闭刚打开的菜单，开合闪烁为已接受
  代价，此时的锁定切换由宿主按“菜单快速开合”兜底判定，见
  ``host_core._open_menu_from_anchor``；本窗口的 DClick 处理器仅作为菜单
  未能打开等边缘时序的兜底）。

菜单弹出位置的侧向自适应（贴边、不覆盖锚点）见 :func:`menu_popup_position`。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

try:
    from PyQt6.QtSvg import QSvgRenderer

    _HAS_QTSVG = True
except ImportError:  # 打包缺 QtSvg DLL 等极端场景：锚点降级为纯圆形底
    QSvgRenderer = None
    _HAS_QTSVG = False

logger = logging.getLogger("renpy_overlay.quick_menu_anchor")

#: 锚点圆形底色（主题淡蓝 #ADD8E6，与标题/正文文字同一视觉语言），半透明
ANCHOR_FILL_COLOR = QColor(173, 216, 230, 110)
#: 全窗垫底层 alpha：必须大于 0（alpha=0 像素命中穿透，见模块 docstring），
#: 取最小值 1 保证四角无视觉干扰
CORNER_UNDERLAY_ALPHA = 1
#: 解锁态虚线描边（提示可拖动；锁定态无描边）
UNLOCKED_BORDER_COLOR = QColor(70, 90, 110, 170)
UNLOCKED_BORDER_WIDTH = 2
#: 图标边长 = 直径的比例（需求：不超出窗口的前提下尽量覆盖满）
ICON_RELATIVE_SIZE = 0.8
#: 判定"点击"的最大位移（像素）：超过视为拖动（与流式窗/遥控窗同容差）
CLICK_MOVE_TOLERANCE = 4
#: 图标资产（素材描边 #333 原样保留；路径基准同 stream/icons.py）
ANCHOR_ICON_PATH = Path(__file__).parent / "assets" / "icons" / "设置_setting.svg"


def icon_size_for(diameter: int) -> int:
    """直径 → 图标边长（纯函数，离线测试用）：直径 × 0.8，下限 1。"""
    return max(1, int(diameter * ICON_RELATIVE_SIZE))


def default_anchor_position(screen: QRect, diameter: int) -> QPoint:
    """首次运行（无 layout 记录）的锚点默认位置（纯函数）：屏幕可用区域中心。"""
    return QPoint(
        screen.center().x() - diameter // 2,
        screen.center().y() - diameter // 2,
    )


def menu_popup_position(anchor: QRect, menu_size: QSize, screen: QRect) -> QPoint:
    """快捷菜单弹出位置（纯函数，离线测试用）：按锚点与屏幕边缘的距离选边。

    规则（需求：自适应出现在锚点哪一侧、紧贴锚点边缘、不可覆盖锚点）：

    - 右侧放得下 → 紧贴锚点右缘（间隙 1px）；否则左侧放得下 → 紧贴左缘；
    - 两侧均放不下（菜单过宽）：取剩余空间大的一侧并钳入屏幕内 —— 该极端
      情况下完整可见优先于不覆盖（放哪都会与锚点水平重叠）；
    - 垂直：菜单顶与锚点顶对齐，底边超出屏幕时上移、越顶时钳到屏幕顶。

    坐标全部为 Qt 全局逻辑坐标（与锚点 ``geometry()`` 同口径）。放得下的
    分支在构造上保证菜单矩形与锚点矩形不相交。
    """
    gap = 1
    width = max(1, menu_size.width())
    height = max(1, menu_size.height())
    right_space = screen.right() - anchor.right()
    left_space = anchor.left() - screen.left()
    if right_space >= width + gap:
        x = anchor.right() + gap
    elif left_space >= width + gap:
        x = anchor.left() - gap - width
    elif right_space >= left_space:
        x = min(anchor.right() + gap, screen.right() - width + 1)
    else:
        x = max(anchor.left() - gap - width, screen.left())
    y = anchor.top()
    if y + height > screen.bottom() + 1:
        y = screen.bottom() + 1 - height
    if y < screen.top():
        y = screen.top()
    return QPoint(x, y)


class QuickMenuAnchorWindow(QWidget):
    """快捷菜单锚点：圆形半透明悬浮钮 + 双击锁定/解锁 + 拖动/单击开合菜单。

    默认锁定态（单击即开菜单，核心用途）；``apply_layout`` 为 layout 启动
    恢复入口（直径/位置/锁定态一次应用）。窗口不含业务逻辑，开合菜单经
    ``on_toggle_menu`` 回调交宿主处理（菜单构建在 QuickMenu，定位经
    ``menu_popup_position``）。
    """

    def __init__(
        self,
        diameter: int,
        on_toggle_menu=None,
        initial_pos: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(None)
        # 标志必须在 show 之前一次固定（与遥控截图小圆窗同惯例）
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("renpy-overlay · quick menu anchor")
        self._on_toggle_menu = on_toggle_menu
        self._locked = True  # 默认锁定态：单击即开快捷菜单（拖动需先双击解锁）
        self._diameter = max(1, int(diameter))
        self.setFixedSize(self._diameter, self._diameter)
        self._icon_pixmap: QPixmap | None = None
        self._render_icon()
        if initial_pos is not None:
            self.move(QPoint(int(initial_pos[0]), int(initial_pos[1])))
        self._press_global: QPoint | None = None
        self._dragging = False
        self._drag_grab = QPoint()

    # ---- 对外接口 -----------------------------------------------------------

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def diameter(self) -> int:
        return self._diameter

    def apply_layout(
        self,
        diameter: int,
        pos: tuple[int, int] | None = None,
        locked: bool = False,
    ) -> None:
        """layout 启动恢复入口：直径/位置/锁定态一次应用（直径恢复优先于 config）。"""
        self._locked = bool(locked)
        self._diameter = max(1, int(diameter))
        self.setFixedSize(self._diameter, self._diameter)
        if pos is not None:
            self.move(QPoint(int(pos[0]), int(pos[1])))
        # setFixedSize 尺寸不变时不触发 resizeEvent，显式重渲染保证图标随直径
        self._render_icon()
        self.update()

    def toggle_lock(self) -> None:
        """切换锁定态并重绘（DClick 兕底与宿主“菜单快速开合”判定共用）。"""
        self._locked = not self._locked
        logger.info("快捷菜单锚点位置已%s（双击切换）", "锁定" if self._locked else "解锁")
        self.update()

    # ---- 鼠标交互 -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        self._press_global = QPoint(event.globalPosition().toPoint())
        if not self._locked:
            # 解锁态：按住即进入拖动（保持抓取点相对位置不变整体移动）
            self._dragging = True
            self._drag_grab = self._press_global - self.geometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_grab)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        press_global = self._press_global
        self._press_global = None
        self._dragging = False
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        if press_global is not None:
            moved = event.globalPosition().toPoint() - press_global
            if (
                abs(moved.x()) <= CLICK_MOVE_TOLERANCE
                and abs(moved.y()) <= CLICK_MOVE_TOLERANCE
            ):
                # 单击（按下与松开的位移均在容差内）：立即开合快捷菜单（松开
                # 即响应，锁定/解锁态一致；拖动位移超容差则不触发，见上）。
                # 双击的第二击由模态菜单作为“菜单外点击”关闭刚打开的菜单，
                # 开合闪烁为已接受取舍，锁定切换由宿主快速开合判定兜底
                logger.debug("快捷菜单锚点单击：请求开合快捷菜单")
                if callable(self._on_toggle_menu):
                    self._on_toggle_menu()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """双击切换锁定态（兜底路径；常规时序下菜单模态期 DClick 不可达）。

        锁定/解锁态单击都立即弹菜单（模态 exec 持续鼠标 grab），双击的第二
        击被菜单吞掉、DClick 不再派发到本窗口——该场景的锁定切换由宿主
        “菜单快速开合”判定完成；本处理器仅覆盖菜单未能打开等边缘时序。
        """
        self._dragging = False  # 双击的第二次按下可能顺带进入拖动，直接取消
        self._press_global = None
        self.toggle_lock()
        event.accept()

    # ---- 渲染 ---------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窗口尺寸变化（layout 恢复直径）：图标按新尺寸矢量重渲染。"""
        super().resizeEvent(event)
        self._render_icon()

    def _render_icon(self) -> None:
        """按当前直径矢量渲染设置图标（QSvgRenderer；失败置 None 降级）。"""
        self._icon_pixmap = None
        if not _HAS_QTSVG:
            logger.debug("QtSvg 不可用，锚点图标降级为纯圆形底")
            return
        size = icon_size_for(self._diameter)
        try:
            renderer = QSvgRenderer(ANCHOR_ICON_PATH.read_bytes())
        except Exception as exc:  # 文件缺失等：降级纯圆形底，不抛出
            logger.debug("锚点图标加载失败（%s）：%s", ANCHOR_ICON_PATH, exc)
            return
        if not renderer.isValid():
            logger.debug("锚点图标 SVG 无效，降级为纯圆形底")
            return
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        renderer.render(painter)
        painter.end()
        self._icon_pixmap = pixmap

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        # 全窗垫底（alpha≥1）：圆形四角不存在全透明像素，命中测试不穿透
        underlay = QColor(ANCHOR_FILL_COLOR)
        underlay.setAlpha(CORNER_UNDERLAY_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(underlay)
        painter.drawRect(rect)
        # 圆形半透明底
        painter.setBrush(QColor(ANCHOR_FILL_COLOR))
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        # 解锁态虚线描边提示可拖动（锁定态无描边）
        if not self._locked:
            pen = QPen(UNLOCKED_BORDER_COLOR)
            pen.setWidth(UNLOCKED_BORDER_WIDTH)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        # 图标居中（尽量覆盖满窗口，不超出）
        if self._icon_pixmap is not None:
            size = self._icon_pixmap.width()
            painter.drawPixmap(
                (self._diameter - size) // 2,
                (self._diameter - size) // 2,
                self._icon_pixmap,
            )
