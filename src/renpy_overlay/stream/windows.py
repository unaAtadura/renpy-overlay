"""流式悬浮窗的透明双窗口渲染层：标题窗与正文窗（Qt 主线程专用）。

从 ``stream_window`` 原子化拆出的渲染实现，方法体零改动：

- ``_StreamWindowBase``：全透明无边框置顶底座 + 鼠标事件统一转发宿主；
- ``TitleWindow``：一次性整显标题（随交互状态动态更新），内嵌图标入口条；
- ``BodyWindow``：打字机逐字弹出、平滑滚动、滚轮回看、锁定态拖拽滚动。

滚动/蒙版/替换的计算逻辑在 :mod:`.scroll_logic` 与 :mod:`.text_logic`，
视觉常量在 :mod:`.visual`。窗口不含业务逻辑，全部经 ``host`` 回调。
"""

from __future__ import annotations

import logging
import math
import time

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PyQt6.QtWidgets import QWidget

from ..bound_window import BOUND_GAP, TitleEntryStrip, dispatch_action
from .scroll_logic import (
    DRAG_SCROLL_INTERVAL_MS,
    body_drag_scroll_rate,
    body_mask_bands,
    body_scroll_target,
    body_wheel_target,
)
from .text_logic import plan_stream_replace
from .visual import (
    BODY_FILL_COLOR,
    BODY_MARGIN,
    MASK_ALPHA,
    MASK_COLOR,
    MASK_PAD_X,
    MASK_PAD_Y,
    MASK_RADIUS,
    RENDER_INTERVAL_MS,
    SPAWN_DURATION,
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

    def __init__(self, title: str, font: QFont, host) -> None:
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

    @property
    def hwnd(self) -> int:
        return int(self.winId())

    @property
    def fm(self) -> QFontMetricsF:
        return self._font_metrics

    def dpr(self) -> float:
        return max(1.0, self.devicePixelRatioF())

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


class TitleWindow(_StreamWindowBase):
    """标题窗：一次性整显完整标题（随交互状态动态更新，但无打字机动画）。

    注入确认后，标题文本右侧内嵌图标入口条（TitleEntryStrip）：与文本同一
    渲染帧实时跟随文本长度，点击图标分发入口回调，不参与窗口拖动/单击翻译。
    """

    def __init__(self, font_px: int, host) -> None:
        font = _stream_font(font_px)
        super().__init__("renpy-overlay · stream title", font, host)
        self._layout: list[tuple[str, float]] = []  # (字符, x 起点)，单行排版
        self._entry_strip = TitleEntryStrip()
        self._entry_rect: QRect | None = None  # 入口条热区（本地坐标，命中测试用）
        self._entry_press: object | None = None  # 图标按下的本地坐标（点击判定）

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
            event.accept()
            return
        self._entry_press = None
        if event.button() == Qt.MouseButton.LeftButton:
            self._host.on_window_press(self, event)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._entry_press is not None:
            event.accept()  # 图标按住期间不触发窗口拖动
            return
        self._host.on_window_motion(self, event)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        press = self._entry_press
        self._entry_press = None
        if press is not None and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if (pos - press).manhattanLength() <= 12:  # 未拖动：单击图标分发入口
                local_x = pos.x() - (self._entry_rect.x() if self._entry_rect else 0)
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

    def set_text(self, text: str) -> None:
        """整显一条标题（单行）：超宽或换行后的内容直接截断。

        入口条非空时为其预留宽度：文本可用宽 = 窗宽 - 2×边距 - (条宽 + 列距)，
        文本与图标同一渲染帧排版，图标实时跟随文本长度。
        """
        self._layout.clear()
        fm = self.fm
        x = TITLE_MARGIN_X
        reserve = 0
        if not self._entry_strip.is_empty():
            reserve = self._entry_strip.width() + BOUND_GAP
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

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._layout:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fm = self.fm
        box_h = fm.height()
        center_y = self.height() / 2.0  # 单行垂直居中
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
        # 常规字体直绘：统一淡蓝色纯色，基线取"字形盒垂直居中"对应的 ascent 偏移
        painter.setFont(self._font)
        painter.setPen(QColor(BODY_FILL_COLOR))
        for ch, x in self._layout:
            painter.drawText(QPointF(x, baseline_y), ch)
        # 入口条：紧跟标题文本右侧（同一渲染帧，实时随文本长度移动）
        if not self._entry_strip.is_empty():
            text_right = last_x + fm.horizontalAdvance(last_ch)
            hot = self._entry_strip.hotzone()
            entry_x = int(text_right + BOUND_GAP)
            entry_y = int(center_y - hot / 2.0)
            self._entry_rect = QRect(entry_x, entry_y, self._entry_strip.width(), hot)
            self._entry_strip.paint(painter, entry_x, entry_y)


class _Glyph:
    """一个已排版字符（坐标为窗口内容坐标系，不含滚动偏移）。"""

    __slots__ = ("ch", "x", "baseline", "advance", "spawn", "row")

    def __init__(
        self, ch: str, x: float, baseline: float, advance: float, row: int
    ) -> None:
        self.ch = ch
        self.x = x
        self.baseline = baseline
        self.advance = advance
        self.spawn = time.monotonic()
        self.row = row


class BodyWindow(_StreamWindowBase):
    """正文窗：打字机式逐字弹出的流式文本，固定显示第一行 + 滚轮回看。"""

    def __init__(self, font_px: int, line_spacing: float, host) -> None:
        font = _stream_font(font_px)
        super().__init__("renpy-overlay · stream body", font, host)
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
        （倒数第 2 个字符始终可见）。切换即重新进入跟随态，滚动目标由
        下帧按新锚点收敛。
        """
        self._anchor = "tail" if anchor == "tail" else "start"
        self._follow = True

    def replace_stream(self, text: str) -> None:
        """以整段文本替换当前流内容（实时语音识别专用：支持增加与修正）。

        依 :func:`plan_stream_replace` 差分：公共前缀的已排版字形原样保留，
        仅撤销/重喂差异尾部——已显示部分不重播出现动画，观感为整段连续
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

        now = time.monotonic()
        ascent = fm.ascent()
        height = fm.height()
        c1, c3 = 1.70158, 2.70158  # easeOutBack 参数（弹出时轻微过冲）
        painter.setFont(self._font)
        for glyph in self._glyphs:
            age = now - glyph.spawn
            p = min(1.0, max(0.0, age / SPAWN_DURATION))
            if p < 1.0:  # 出现动画：放大过冲 + 跳起 + 淡入
                q = p - 1.0
                ease = 1.0 + c3 * q * q * q + c1 * q * q
                scale = 0.25 + 0.75 * ease
                dy = -30.0 * math.sin(math.pi * p) * (1.0 - p)
                alpha = min(1.0, 0.15 + p * 1.6)
            else:  # 静止：位置与外观完全固定（无漂浮动画）
                scale, dy, alpha = 1.0, 0.0, 1.0

            # 局部原点取"字形盒中心"（与原精灵中心对齐）：drawText 画在
            # (-advance/2, ascent - height/2)，scale=1/dy=0/alpha=1 时数学上
            # 等价于直接 drawText(glyph.x, glyph.baseline)
            center_x = glyph.x + glyph.advance / 2.0
            center_y = glyph.baseline - ascent + height / 2.0
            painter.save()
            painter.translate(center_x, center_y + dy)
            painter.scale(scale, scale)
            painter.setOpacity(alpha)
            painter.setPen(QColor(BODY_FILL_COLOR))
            painter.drawText(QPointF(-glyph.advance / 2.0, ascent - height / 2.0), glyph.ch)
            painter.restore()

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
