"""流式悬浮窗左侧的「绑定窗口」：注入强相关功能的入口容器。

与右键快捷菜单的分工（设计文档第二节/第六节）：快捷菜单承载可脱离注入
运行的入口（截图翻译 / 识曲 / 历史窗口等），本窗口只承载**强依赖注入**的
功能 —— 仅注入成功后显示，跳过注入模式不创建。当前注册一个入口
「预构建翻译缓存」。

可扩展结构：

- **入口抽象层**：:class:`BoundAction` 描述一个入口（id / 文案 / 可用谓词 /
  回调），新增注入相关功能 = 宿主向 :class:`BoundWindow` 注册一个 action，
  渲染与接线零改动（与 ``QuickMenu`` "宿主能力经构造回调注入"同构）；
- **渲染可替换**：渲染层实现 :class:`BoundEntryRenderer` 协议 —— 当前为
  :class:`TextButtonRenderer`（竖排文字按钮）；未来可换图标渲染
  （QIcon / QPainter 自绘）或单按钮弹菜单形态（承载扩展功能快捷菜单），
  切换不改 action 定义。

窗口本体：全透明、无边框、置顶、Tool、不抢焦点，鼠标事件按
``_StreamWindowBase`` 同一协议转发宿主（拖动与标题/正文窗整体联动）。
为避免与 ``stream_window`` 循环导入，标志设置在此复刻而非继承 ——
``_apply_overlay_flags`` 与 ``_StreamWindowBase.__init__`` 同步维护。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget

logger = logging.getLogger("renpy_overlay.bound_window")

#: 绑定窗口与标题/正文窗整体的间距（物理像素，与 _title_gap 同量级）
BOUND_GAP = 8
#: 按钮间与窗口边缘的留白（逻辑像素）
MARGIN = 6
#: 底板颜色与透明度（与标题/正文窗文字蒙版同一视觉语言：半透明黑色圆角板）
PANEL_COLOR = (0, 0, 0, 140)
PANEL_RADIUS = 10.0
#: 按钮最小宽度的额外余量（逻辑像素）：度量误差 + 边框/焦点线
BUTTON_WIDTH_SLACK = 12


def text_button_width(text_advance: int) -> int:
    """按钮保证文字完整显示的最小宽度（纯函数，离线测试用）。

    ``text_advance`` 为 QFontMetrics.horizontalAdvance(文字)；左右各留
    MARGIN，再加度量误差余量 —— 默认 sizeHint 在部分字体下偏小，会把
    「预构建翻译缓存」这类长文案截断（实测缺陷）。
    """
    return text_advance + 2 * MARGIN + BUTTON_WIDTH_SLACK


def logical_to_physical(
    width: int, height: int, dpr: float
) -> tuple[int, int]:
    """Qt 逻辑像素 → win32 物理像素（纯函数，离线测试用）。

    绑定窗口定位必须用物理尺寸缓存：若把逻辑值当物理值喂给 SetWindowPos
    再从 Qt 读回，缩放屏（dpr > 1）下会逐帧衰减并弹回，形成周期性振荡
    （实测缺陷）。
    """
    scale = max(1.0, float(dpr))
    return round(width * scale), round(height * scale)


# ------------------------------------------------------------------ 入口抽象层


@dataclass
class BoundAction:
    """一个注入相关功能入口（渲染层无关的功能描述）。"""

    id: str  # 稳定标识（日志与未来菜单化定位用）
    label: str  # 文字渲染形态的按钮文案
    on_click: Callable[[], None]
    enabled: Callable[[], bool] | None = None  # None = 恒可用

    def is_enabled(self) -> bool:
        if self.enabled is None:
            return True
        try:
            return bool(self.enabled())
        except Exception:  # pragma: no cover - 谓词异常按不可用处理
            return False


def button_specs(actions: list[BoundAction]) -> list[tuple[str, bool]]:
    """渲染前的纯规格视图：``[(文案, 是否可用), ...]``（离线测试用）。"""
    return [(action.label, action.is_enabled()) for action in actions]


class BoundEntryRenderer:
    """入口渲染协议：把 action 列表填充到一个容器布局上。

    协议只约定 :meth:`populate`；未来图标形态（QIcon / QPainter 自绘）或
    单按钮弹菜单形态实现同一协议即可替换，action 定义与宿主接线不变。
    """

    def populate(self, container: QWidget, actions: list[BoundAction]) -> None:
        raise NotImplementedError


class TextButtonRenderer(BoundEntryRenderer):
    """当前形态：QVBoxLayout 竖排文字按钮（点击即分发 action 回调）。"""

    def populate(self, container: QWidget, actions: list[BoundAction]) -> None:
        layout = QVBoxLayout(container)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(MARGIN)
        for action in actions:
            button = QPushButton(action.label, container)
            # 按字体度量锁最小宽：防止默认 sizeHint 偏小截断长文案（实测缺陷）
            button.setMinimumWidth(text_button_width(button.fontMetrics().horizontalAdvance(action.label)))
            button.clicked.connect(self._dispatch(action))
            layout.addWidget(button)
        layout.addStretch(1)

    @staticmethod
    def _dispatch(action: BoundAction) -> Callable[[], None]:
        def _run() -> None:
            if not action.is_enabled():
                logger.debug("绑定窗口入口不可用，忽略点击：%s", action.id)
                return
            logger.info("绑定窗口入口点击：%s", action.id)
            action.on_click()

        return _run


# ------------------------------------------------------------------ 窗口本体


def _apply_overlay_flags(widget: QWidget) -> None:
    """与 ``_StreamWindowBase.__init__`` 相同的悬浮窗标志（同步维护）。"""
    widget.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)


class BoundWindow(QWidget):
    """绑定窗口：窄条竖排入口按钮，鼠标事件按宿主协议转发（拖动整体联动）。"""

    def __init__(
        self,
        host,
        actions: list[BoundAction] | None = None,
        renderer: BoundEntryRenderer | None = None,
    ) -> None:
        super().__init__()
        _apply_overlay_flags(self)
        self.setWindowTitle("renpy-overlay · bound")
        self._host = host  # StreamOverlayWindow：拖动/置顶等事件的唯一处理者
        self._actions: list[BoundAction] = []
        # 渲染层可替换（工厂注入便于离线测试）；默认竖排文字按钮
        self._renderer = renderer or TextButtonRenderer()
        self._layout_host = QWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._layout_host)
        self.set_actions(actions or [])

    # ---- 渲染 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        """绘制半透明圆角底板。

        全透明外壳若从不产生任何渲染帧，真实 Windows 合成器下可能永远
        不被合成（实测教训：透明窗口必须进入 Qt 渲染管线才在屏幕上存在，
        与标题/正文窗的 paintEvent + 定时 update 同理）。底板同时让入口
        按钮条在游戏画面上有可见背景，不再依赖按钮默认样式的偶然对比度。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r, g, b, a = PANEL_COLOR
        panel = QColor(r, g, b, a)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(panel)
        painter.drawRoundedRect(QRectF(self.rect()), PANEL_RADIUS, PANEL_RADIUS)

    def showEvent(self, event) -> None:  # noqa: N802
        """显示即主动请求一帧渲染（不依赖合成器对空内容窗口的 expose 处理）。"""
        super().showEvent(event)
        self.update()

    # ---- 入口管理 -----------------------------------------------------------

    def set_actions(self, actions: list[BoundAction]) -> None:
        """整体重建入口（渲染层重新填充；重复注册同 id 的 action 覆盖）。"""
        unique: dict[str, BoundAction] = {item.id: item for item in self._actions}
        for action in actions:
            unique[action.id] = action
        self._actions = list(unique.values())
        self._renderer.populate(self._layout_host, self._actions)
        self.adjustSize()

    @property
    def actions(self) -> list[BoundAction]:
        return list(self._actions)

    # ---- 宿主协议（与 _StreamWindowBase 一致：窗口不含业务逻辑） ------------

    @property
    def hwnd(self) -> int:
        return int(self.winId())

    def dpr(self) -> float:
        return max(1.0, self.devicePixelRatioF())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._host.on_window_press(self, event)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._host.on_window_motion(self, event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._host.on_window_release(self, event)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._host.on_window_double_click(self, event)
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._host.on_window_context_menu(self, event)
        event.accept()
