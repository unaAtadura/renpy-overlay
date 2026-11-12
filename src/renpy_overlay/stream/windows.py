"""流式悬浮窗的透明双窗口渲染层：标题窗与正文窗（Qt 主线程专用）。

从 ``stream_window`` 原子化拆出的渲染实现，方法体零改动：

- ``_StreamWindowBase``：全透明无边框置顶底座 + 鼠标事件统一转发宿主；
- ``TitleWindow``：一次性整显标题（随交互状态动态更新），内嵌图标入口条；
- ``BodyWindow``：打字机逐字上屏（字符直接在最终位置绘制，无出现动画）、
  平滑滚动、滚轮回看、锁定态拖拽滚动。

滚动/蒙版/替换的计算逻辑在 :mod:`.scroll_logic` 与 :mod:`.text_logic`，
视觉常量在 :mod:`.visual`。窗口不含业务逻辑，全部经 ``host`` 回调。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PyQt6.QtWidgets import QWidget

from ..bound_window import BOUND_GAP, ENTRY_GAP, TitleEntryStrip, dispatch_action
from ..screenshot.window_visual import draw_edge_highlight
from .geometry import (
    RESIZE_EDGE_PX,
    entry_scroll_clamp,
    entry_scroll_max,
    entry_visible_width,
    entry_wheel_step,
    resize_band_rects,
    title_hidden_for_width,
)
from .scroll_logic import (
    DRAG_SCROLL_INTERVAL_MS,
    body_drag_scroll_rate,
    body_mask_bands,
    body_scroll_target,
    body_wheel_target,
)
from .text_logic import plan_stream_replace
from .visual import (
    BODY_MARGIN,
    MASK_ALPHA,
    MASK_COLOR,
    MASK_PAD_X,
    MASK_PAD_Y,
    MASK_RADIUS,
    RENDER_INTERVAL_MS,
    TITLE_MARGIN_X,
    TYPE_INTERVAL_MS,
)

logger = logging.getLogger("renpy_overlay.stream_window")


def _stream_font(font_px: int) -> QFont:
    """标题窗与正文窗共用的常规字体：固定家族 + 像素字号 + Black 字重。

    字重与排版度量（advance/height）直接决定换行、截断与蒙版宽度，
    不得改为更轻的字重。
    """
    font = QFont()
    font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
    font.setPixelSize(font_px)
    font.setWeight(QFont.Weight.Black)
    return font


class _StreamWindowBase(QWidget):
    """标题窗与正文窗的公共底座：全透明无边框置顶 + 鼠标事件转发给宿主。"""

    def __init__(self, title: str, font: QFont, host, fill_color: str) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # show() 不抢焦点：悬浮窗叠加在游戏上，激活会把焦点从游戏夺走
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._font = font
        self._host = host  # StreamOverlayWindow：业务事件的唯一处理者
        self._font_metrics = QFontMetricsF(font)
        self._hover_edge: str | None = None  # 悬停命中的拉伸边/角（八向拉伸提示）
        # 鼠标追踪：未按下（悬停）也接收 move，供宿主做边缘检测与光标切换
        self.setMouseTracking(True)
        # 文字填充色（config.body_fill_color）：paintEvent 每帧现取，
        # 宿主热重载时经 set_fill_color 更新（无状态风险，无需重启）
        self._fill_color = QColor(fill_color)

    @property
    def hwnd(self) -> int:
        return int(self.winId())

    @property
    def fm(self) -> QFontMetricsF:
        return self._font_metrics

    def dpr(self) -> float:
        return max(1.0, self.devicePixelRatioF())

    def set_fill_color(self, color: str) -> None:
        """热应用文字填充色（与当前值相同则跳过；纯绘制参数，重绘即生效）。"""
        updated = QColor(color)
        if updated == self._fill_color:
            return
        self._fill_color = updated
        self.update()

    # ---- 鼠标事件统一转发：窗口不含业务逻辑 ----

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
        """右键（标题/正文文字处）：转发宿主弹快捷菜单（创建/销毁截图窗等）。"""
        self._host.on_window_context_menu(self, event)
        event.accept()

    # ---- 八向拉伸提示（未锁定；interaction 驱动） ---------------------------

    def set_hover_highlight(self, edge: str | None) -> None:
        """悬停边缘高亮（interaction 驱动；None = 清除）；锁定态不显示。"""
        edge = None if getattr(self._host, "_locked", False) else edge
        if edge != self._hover_edge:
            self._hover_edge = edge
            self.update()

    def paint_hover_highlight(self, painter: QPainter) -> None:
        """绘制悬停边缘亮白反馈线（八向拉伸提示）；锁定态不绘制。"""
        if self._hover_edge is None or getattr(self._host, "_locked", False):
            return
        draw_edge_highlight(painter, self.rect(), self._hover_edge)

    def paint_resize_band(self, painter: QPainter) -> None:
        """绘制四向低 alpha 可命中条带（未锁定；分层窗口 alpha=0 像素命中
        穿透——边缘热区必须有绘制像素才能接收鼠标事件）。锁定态不绘制。"""
        if getattr(self._host, "_locked", False):
            return
        color = QColor(173, 216, 230, 28)  # #ADD8E6 低 alpha（可命中且不喧宾）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for x, y, w, h in resize_band_rects(self.width(), self.height(), RESIZE_EDGE_PX):
            painter.drawRect(x, y, w, h)


class TitleWindow(_StreamWindowBase):
    """标题窗：一次性整显完整标题（随交互状态动态更新，但无打字机动画）。

    注入确认后，标题文本右侧内嵌图标入口条（TitleEntryStrip）：与文本同一
    渲染帧实时跟随文本长度，点击图标分发入口回调，不参与窗口拖动/单击翻译。
    """

    def __init__(self, font_px: int, host, fill_color: str) -> None:
        font = _stream_font(font_px)
        super().__init__("renpy-overlay · stream title", font, host, fill_color)
        self._layout: list[tuple[str, float]] = []  # (字符, x 起点)，单行排版
        self._entry_strip = TitleEntryStrip()
        self._entry_rect: QRect | None = None  # 入口条可见热区（本地坐标，命中测试用）
        self._entry_press: object | None = None  # 图标按下的本地坐标（点击判定）
        self._title_hidden = False  # 仅图标态：宽度过窄时隐藏状态文本（含回滞）
        self._entry_scroll = 0  # 图标列表横向滚动偏移（px，≥0；锁定态拖拽/滚轮）
        self._entry_dragging_scroll = False  # 锁定态图标拖拽滚动手势进行中
        self._entry_drag_last_x = 0  # 拖拽滚动上一帧鼠标 x（跟手增量）

    def set_title_entries(self, actions: list) -> None:
        """注入确认时注入入口条并重排当前标题（可用宽扣除图标区）。"""
        self._entry_strip.set_actions(actions)
        self.set_text(self._host._title_text if getattr(self._host, "_title_text", "") else "")

    def _entry_hit(self, pos) -> bool:
        return self._entry_rect is not None and self._entry_rect.contains(pos)

    # ---- 入口条命中拦截：图标点击不参与拖动/单击翻译 -----------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._entry_hit(
            event.position().toPoint()
        ):
            self._entry_press = event.position().toPoint()
            # 锁定态图标拖拽滚动手势状态（未超阈值前不影响单击分发）
            self._entry_dragging_scroll = False
            self._entry_drag_last_x = self._entry_press.x()
            event.accept()
            return
        self._entry_press = None
        self._entry_dragging_scroll = False
        if event.button() == Qt.MouseButton.LeftButton:
            self._host.on_window_press(self, event)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._entry_press is not None:
            pos = event.position().toPoint()
            # 锁定态：按住图标移动超过既有单击互判阈值 → 图标列表横向滚动
            # （跟手、无惯性；未超阈值保持单击语义）。未锁定态不滚动。
            if getattr(self._host, "_locked", False):
                visible_w = self._entry_rect.width() if self._entry_rect else 0
                if (
                    not self._entry_dragging_scroll
                    and abs(pos.x() - self._entry_press.x()) > 12
                ):
                    self._entry_dragging_scroll = True
                    # 观测：进入手势即打一条（含可滚动上界）——可区分「未进
                    # 锁定/未进分支」与「进入但夹取为 0（条带未溢出）」
                    logger.debug(
                        "图标列表拖拽滚动：进入（scroll=%d max=%d）",
                        self._entry_scroll,
                        entry_scroll_max(self._entry_strip.width(), visible_w),
                    )
                if self._entry_dragging_scroll:
                    self._entry_scroll = entry_scroll_clamp(
                        self._entry_scroll - (pos.x() - self._entry_drag_last_x),
                        self._entry_strip.width(),
                        visible_w,
                    )
                    self.update()
                self._entry_drag_last_x = pos.x()
            event.accept()  # 图标按住期间不触发窗口拖动/拉伸
            return
        self._host.on_window_motion(self, event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        press = self._entry_press
        dragged = self._entry_dragging_scroll
        self._entry_press = None
        self._entry_dragging_scroll = False
        if press is not None and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if dragged:
                event.accept()  # 已进入拖拽滚动手势：松手不分发单击
                return
            if (pos - press).manhattanLength() <= 12:  # 未拖动：单击图标分发入口
                local_x = (
                    pos.x()
                    - (self._entry_rect.x() if self._entry_rect else 0)
                    + self._entry_scroll
                )
                dispatch_action(self._entry_strip.action_at(local_x))
            event.accept()
            return
        self._host.on_window_release(self, event)
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._entry_hit(event.position().toPoint()):
            event.accept()  # 图标上的双击不触发锁定/解锁
            return
        self._host.on_window_double_click(self, event)
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        """宽度变化（八向拉伸/跟随重摆）后重排标题并重估仅图标态。"""
        self.set_text(getattr(self._host, "_title_text", "") or "")
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        """图标列表区域横向滚轮滚动（一格一图标，边界夹取；锁定/未锁定
        均可）。鼠标不在图标可见区时 ignore——正文窗是独立顶层窗口，其
        滚轮回看与底部自动跟随不受影响。"""
        if self._entry_strip.is_empty():
            event.ignore()
            return
        pos = event.position().toPoint()
        if self._entry_rect is None or not self._entry_rect.contains(pos):
            event.ignore()
            return
        delta = event.angleDelta().y() or int(event.pixelDelta().y() * 8)
        if delta == 0:
            event.ignore()
            return
        step = entry_wheel_step(self._entry_strip.hotzone())
        visible_w = self._entry_rect.width() if self._entry_rect else 0
        target = entry_scroll_clamp(
            self._entry_scroll + int(round(-delta / 120.0 * step)),
            self._entry_strip.width(),
            visible_w,
        )
        if target != self._entry_scroll:
            self._entry_scroll = target
            self.update()
        event.accept()

    def set_text(self, text: str) -> None:
        """整显一条标题（单行）：超宽或换行后的内容直接截断。

        入口条非空时为其预留宽度：文本可用宽 = 窗宽 - 2×边距 - (条宽 + 列距)，
        文本与图标同一渲染帧排版，图标实时跟随文本长度。
        宽度不足时进入仅图标态（title_hidden_for_width，含回滞）：内部
        排版状态照常更新，仅绘制跳过，恢复宽度后即为最新内容。
        """
        self._layout.clear()
        fm = self.fm
        x = TITLE_MARGIN_X
        reserve = 0
        if not self._entry_strip.is_empty():
            reserve = self._entry_strip.width() + BOUND_GAP
        # 仅图标态评估：以「完整文本实测宽（未截断）」判定可用宽是否足够
        full_w = 0.0
        for ch in text:
            if ch == "\n":
                break
            full_w += fm.horizontalAdvance(ch)
        avail = max(0.0, self.width() - 2 * TITLE_MARGIN_X)
        hidden = title_hidden_for_width(
            avail,
            full_w,
            self._entry_strip.hotzone(),
            ENTRY_GAP,
            current_hidden=self._title_hidden,
        )
        if hidden != self._title_hidden:
            self._title_hidden = hidden
            logger.debug("标题窗仅图标态：%s（avail=%.0f text_w=%.0f）", hidden, avail, full_w)
        max_w = max(60.0, self.width() - 2 * TITLE_MARGIN_X - reserve)
        for ch in text:
            if ch == "\n":
                break
            advance = fm.horizontalAdvance(ch)
            if x + advance > max_w:
                break
            self._layout.append((ch, x))
            x += advance
        self.update()

    def content_width(self) -> int:
        """当前标题内容的排版宽度（逻辑像素；空标题为 0）。

        取最后一字符的右缘 + 右侧对称留白 —— 绑定窗口用它作为横向让位
        的驱动量（长内容右推、短内容左拉），正文窗不参与该联动。
        """
        if not self._layout:
            return 0
        last_ch, last_x = self._layout[-1]
        return int(last_x + self.fm.horizontalAdvance(last_ch)) + TITLE_MARGIN_X

    def entry_top_right_global(self, action_id: str) -> QPoint | None:
        """指定入口图标的右上角全局坐标（弹窗锚定用）；不可用返回 None。

        由入口条矩形（本地坐标）+ 条内 index 换算槽位右缘，再 mapToGlobal；
        图标列表滚动偏移同步参与换算（滚出的图标仍锚定到真实全局位）；
        入口条尚未排版（未绘制过）或 action 不存在时返回 None，调用方降级
        为默认位置。
        """
        if self._entry_rect is None:
            return None
        index = None
        for position, action in enumerate(self._entry_strip.actions):
            if action.id == action_id:
                index = position
                break
        if index is None:
            return None
        slot = self._entry_strip.hotzone() + ENTRY_GAP
        local = QPoint(
            self._entry_rect.x()
            + index * slot
            + self._entry_strip.hotzone()
            - self._entry_scroll,
            self._entry_rect.y(),
        )
        return self.mapToGlobal(local)

    def _paint_entry_strip(self, painter: QPainter, entry_x: int, entry_y: int) -> None:
        """绘制入口条（含横向滚动偏移与可见区裁剪），同步 _entry_rect 可见热区。

        entry_x/entry_y 统一取整：visual 常量为浮点（TITLE_MARGIN_X=16.0），
        QRect 与命中矩形不接受浮点参数（仅图标态直传常量曾致每帧
        CRITICAL TypeError）。"""
        entry_x = int(entry_x)
        entry_y = int(entry_y)
        visible_w = entry_visible_width(self.width(), entry_x, TITLE_MARGIN_X)
        strip_w = min(self._entry_strip.width(), visible_w)
        hot = self._entry_strip.hotzone()
        self._entry_rect = QRect(entry_x, entry_y, strip_w, hot)
        if strip_w <= 0:
            return
        painter.save()
        painter.setClipRect(QRectF(entry_x, entry_y, strip_w, hot))
        if self._entry_scroll:
            painter.translate(-self._entry_scroll, 0)
        self._entry_strip.paint(painter, entry_x, entry_y)
        painter.restore()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._layout:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fm = self.fm
        box_h = fm.height()
        center_y = self.height() / 2.0  # 单行垂直居中
        entry_y = int(center_y - self._entry_strip.hotzone() / 2.0)
        # 仅图标态：跳过蒙版与文本绘制，入口条左移顶到可用区起点（无空洞）
        if self._title_hidden:
            if not self._entry_strip.is_empty():
                self._paint_entry_strip(painter, TITLE_MARGIN_X, entry_y)
            self.paint_resize_band(painter)
            self.paint_hover_highlight(painter)
            return
        baseline_y = (self.height() - box_h) / 2.0 + fm.ascent()
        # 蒙版：覆盖整行文字范围（单行，无相邻行间隙问题），画在字符之下
        first_x = self._layout[0][1]
        last_ch, last_x = self._layout[-1]
        band_w = (last_x + fm.horizontalAdvance(last_ch)) - first_x + 2 * MASK_PAD_X
        band_h = box_h + 2 * MASK_PAD_Y
        mask = QColor(MASK_COLOR)
        mask.setAlpha(MASK_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mask)
        painter.drawRoundedRect(
            QRectF(first_x - MASK_PAD_X, center_y - band_h / 2.0, band_w, band_h),
            MASK_RADIUS,
            MASK_RADIUS,
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 常规字体直绘：统一填充色纯色，基线取"字形盒垂直居中"对应的 ascent 偏移
        painter.setFont(self._font)
        painter.setPen(self._fill_color)
        for ch, x in self._layout:
            painter.drawText(QPointF(x, baseline_y), ch)
        # 入口条：紧跟标题文本右侧（同一渲染帧，实时随文本长度移动）
        if not self._entry_strip.is_empty():
            text_right = last_x + fm.horizontalAdvance(last_ch)
            entry_x = int(text_right + BOUND_GAP)
            self._paint_entry_strip(painter, entry_x, entry_y)
        # 八向拉伸提示：常驻可命中条带（未锁定）+ 悬停边缘亮白反馈线
        self.paint_resize_band(painter)
        self.paint_hover_highlight(painter)


class _Glyph:
    """一个已排版字符（坐标为窗口内容坐标系，不含滚动偏移）。"""

    __slots__ = ("ch", "x", "baseline", "advance", "row")

    def __init__(
        self, ch: str, x: float, baseline: float, advance: float, row: int
    ) -> None:
        self.ch = ch
        self.x = x
        self.baseline = baseline
        self.advance = advance
        self.row = row


class BodyWindow(_StreamWindowBase):
    """正文窗：打字机式逐字上屏的流式文本，固定显示第一行 + 滚轮回看。"""

    def __init__(self, font_px: int, line_spacing: float, host, fill_color: str) -> None:
        font = _stream_font(font_px)
        super().__init__("renpy-overlay · stream body", font, host, fill_color)
        self._font_px = font_px
        self._line_h = font_px * max(1.0, line_spacing)

        self._glyphs: list[_Glyph] = []
        self._pending: list[str] = []  # 待显示字符队列（打字机节拍逐个弹出）
        self._line = 0  # 当前行号
        self._line_used = 0.0  # 当前行已用宽度
        self._line_empty = True
        self._scroll = 0.0  # 当前平滑滚动偏移（像素）
        self._scroll_target = 0.0  # 滚动目标：固定首行与滚轮回看共用的收敛点
        self._follow = True  # True=固定显示第一行/跟随尾部；滚轮回看时暂停
        # 显示锚点："start"=固定显示第一行（默认），"tail"=跟随内容末尾
        # （实时语音识别：倒数第 2 个字符始终可见，与语音同步显示）
        self._anchor = "start"
        # 当前流内容全文（已打字 + 待显示）：replace_stream 的差分基准，
        # 也是对话窗「捕获」按钮的内容来源
        self._stream_text = ""
        # 锁定态拖拽滚动（按住正文文字上下滑动）：档位速率由垂直偏移决定
        self._drag_scroll_active = False
        self._drag_scroll_origin_y = 0.0  # 按下点纵坐标（防抖基准）
        self._drag_scroll_rate = 0.0  # 当前档位速率（逻辑像素/秒；0=防抖区）

        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(TYPE_INTERVAL_MS)
        self._typing_timer.timeout.connect(self._tick_type)
        self._typing_timer.start()
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self.update)
        self._render_timer.start()
        self._drag_scroll_timer = QTimer(self)
        self._drag_scroll_timer.setInterval(DRAG_SCROLL_INTERVAL_MS)
        self._drag_scroll_timer.timeout.connect(self._tick_drag_scroll)

    # ---- 对外接口 ---------------------------------------------------------

    @property
    def drag_scroll_active(self) -> bool:
        """锁定态拖拽滚动进行中（宿主据此把 motion 喂给拖拽滚动而非窗口拖动）。"""
        return self._drag_scroll_active

    def begin_stream(self) -> None:
        """开始一段新的内容：清屏并恢复固定显示第一行（由宿主在内容切换时调用）。"""
        self.clear()

    def feed_text(self, text: str) -> None:
        """向待显示队列追加流式文本。"""
        self._pending.extend(text)
        self._stream_text += text

    @property
    def stream_text(self) -> str:
        """当前流内容全文（已打字 + 待显示；「捕获」按钮的内容来源）。"""
        return self._stream_text

    def set_anchor(self, anchor: str) -> None:
        """设置显示锚点并恢复跟随（宿主在实时识别开启/关闭时切换）。

        ``"start"``：固定显示第一行（默认）；``"tail"``：跟随内容末尾
        （实时语音识别：倒数第 2 个字符始终可见，与语音同步显示）。
        切换即重新进入跟随态，滚动目标由下帧按新锚点收敛。
        """
        self._anchor = "tail" if anchor == "tail" else "start"
        self._follow = True

    def replace_stream(self, text: str) -> None:
        """以整段文本替换当前流内容（实时语音识别专用：支持增加与修正）。

        依 :func:`plan_stream_replace` 差分：公共前缀的已排版字形原样保留，
        仅撤销/重喂差异尾部——已显示部分原地保留，观感为整段连续
        文字原地增长与修正（需求：不将每次修正结果直接追加在后面）。
        """
        old = self._stream_text
        typed_count = len(old) - len(self._pending)
        untype, pending_text = plan_stream_replace(old, typed_count, text)
        if untype:
            self._untype_tail(old[typed_count - untype : typed_count])
        self._pending.clear()
        self._pending.extend(pending_text)
        self._stream_text = text
        self.update()

    def _untype_tail(self, removed: str) -> None:
        """逆序撤销已排版尾部（修正时调用）：换行回退行号，普通字符弹出字形。"""
        for ch in reversed(removed):
            if ch == "\n":
                self._line -= 1
            elif self._glyphs:
                self._glyphs.pop()
            self._refresh_line_state()

    def _refresh_line_state(self) -> None:
        """按剩余末字形重建当前行状态（_Glyph 携带行号与 x 起点，可精确还原）。"""
        if self._glyphs and self._glyphs[-1].row == self._line:
            last = self._glyphs[-1]
            self._line_used = last.x - BODY_MARGIN + last.advance
            self._line_empty = False
        else:
            self._line_used = 0.0
            self._line_empty = True

    def clear(self) -> None:
        self._glyphs.clear()
        self._pending.clear()
        self._line = 0
        self._line_used = 0.0
        self._line_empty = True
        self._scroll = 0.0
        self._scroll_target = 0.0
        self._follow = True
        self._stream_text = ""
        self.end_drag_scroll()
        self.update()

    # ---- 打字机节拍 -------------------------------------------------------

    def _tick_type(self) -> None:
        pending = self._pending
        if not pending:
            return
        # 积压越多消化越快，既保留逐字观感又能跟上流式速度
        step = min(8, 1 + len(pending) // 100)
        for _ in range(step):
            if not pending:
                break
            self._append_char(pending.pop(0))
        self.update()

    def _append_char(self, ch: str) -> None:
        if ch == "\n":
            self._newline()
            return
        width = self.fm.horizontalAdvance(ch)
        if ch == " " and self._line_empty:
            return  # 行首空格丢弃
        if not self._line_empty and self._line_used + width > self._content_width():
            self._newline()
        baseline = BODY_MARGIN + self.fm.ascent() + self._line * self._line_h
        self._glyphs.append(
            _Glyph(ch, BODY_MARGIN + self._line_used, baseline, width, self._line)
        )
        self._line_used += width
        self._line_empty = False

    def _newline(self) -> None:
        self._line += 1
        self._line_used = 0.0
        self._line_empty = True

    def _content_width(self) -> float:
        return max(120.0, self.width() - 2 * BODY_MARGIN)

    def _max_scroll(self) -> float:
        """滚动下界：内容底部贴住窗口底部所需的最大偏移；不足一屏时为 0。"""
        content_h = (self._line + 1) * self._line_h + 2 * BODY_MARGIN
        return max(0.0, content_h - self.height())

    # ---- 渲染 -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # 八向拉伸提示：常驻可命中条带（未锁定）+ 悬停边缘亮白反馈线。均
        # 画在滚动变换之前——条带/高亮属窗口坐标，不随内容滚动平移
        self.paint_resize_band(painter)
        self.paint_hover_highlight(painter)

        # —— 滚动：start 锚点 = 固定显示第一行；tail 锚点 = 跟随内容末尾；
        # 滚轮回看时停在用户翻到的位置 ——
        self._scroll_target = body_scroll_target(
            self._follow, self._scroll_target, self._max_scroll(), self._anchor
        )
        self._scroll += (self._scroll_target - self._scroll) * 0.12
        if abs(self._scroll_target - self._scroll) < 0.5:
            self._scroll = self._scroll_target
        painter.translate(0.0, -self._scroll)

        # 蒙版：按已排版字符逐行绘制半透明条带（随滚动/拖动同步，行距区域透明），
        # 画在字符之下；打字机过程中只覆盖已出现字符所在的行
        fm = self.fm
        spans = [(glyph.row, glyph.x, glyph.advance) for glyph in self._glyphs]
        if spans:
            mask = QColor(MASK_COLOR)
            mask.setAlpha(MASK_ALPHA)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(mask)
            for x, y, w, h in body_mask_bands(spans, self._line_h, fm.height()):
                painter.drawRoundedRect(QRectF(x, y, w, h), MASK_RADIUS, MASK_RADIUS)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        # 逐字直绘：轮到显示的那一帧直接以最终位置与颜色呈现——无位移/缩放/
        # 透明度过渡，不产生半透明或偏移的中间帧（出现动画已移除）
        painter.setFont(self._font)
        painter.setPen(self._fill_color)
        for glyph in self._glyphs:
            painter.drawText(QPointF(glyph.x, glyph.baseline), glyph.ch)

    # ---- 滚轮回看 ----------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802
        """滚轮回看当前对话全文：下翻离开顶部暂停固定，滚回顶部恢复固定首行。"""
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y() * 8
        if delta == 0:
            event.ignore()
            return
        notches = delta / 120.0
        self._scroll_target, self._follow = body_wheel_target(
            self._scroll_target, notches, self._line_h, self._max_scroll(), self._anchor
        )
        event.accept()

    # ---- 锁定态拖拽滚动 -------------------------------------------------

    def begin_drag_scroll(self, y: float) -> None:
        """锁定态左键按住正文文本：进入拖拽滚动，记下防抖基准纵坐标。"""
        self._drag_scroll_active = True
        self._drag_scroll_origin_y = y
        self._drag_scroll_rate = 0.0

    def update_drag_scroll(self, y: float) -> None:
        """按当前纵坐标更新速度档位：离开防抖区后按档位速率持续滚动。"""
        if not self._drag_scroll_active:
            return
        self._drag_scroll_rate = body_drag_scroll_rate(
            y - self._drag_scroll_origin_y, self._line_h
        )
        if self._drag_scroll_rate == 0.0:
            self._drag_scroll_timer.stop()
            return
        self._follow = False  # 拖拽滚动 = 主动离开固定首行（滚回顶部再恢复）
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start()

    def end_drag_scroll(self) -> None:
        """结束拖拽滚动（未激活时为无害调用）：停步进、清档位。"""
        self._drag_scroll_active = False
        self._drag_scroll_rate = 0.0
        self._drag_scroll_timer.stop()

    def _tick_drag_scroll(self) -> None:
        """拖拽滚动步进：按当前档位速率推进滚动目标（钳在 [0, max_scroll]），
        滚回顶部即恢复固定首行；重绘由常开的渲染定时器统一驱动。"""
        if self._drag_scroll_rate == 0.0:
            return
        dt = DRAG_SCROLL_INTERVAL_MS / 1000.0
        self._scroll_target = min(
            max(self._scroll_target + self._drag_scroll_rate * dt, 0.0),
            self._max_scroll(),
        )
        # 拖拽滚动的跟随恢复点与锚点镜像：start 滚回顶部恢复，tail 滚回底部恢复
        if self._anchor == "tail":
            if self._scroll_target >= self._max_scroll() - 0.5:
                self._follow = True
        elif self._scroll_target <= 0.5:
            self._follow = True
