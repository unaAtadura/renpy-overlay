"""流式输出悬浮窗：全透明双窗口（标题 + 正文），常规字体流式呈现 API 译文。

唯一的悬浮窗实现（PyQt6 为硬依赖，缺失时启动即报清晰 ImportError）。关键设计：

1. **双窗口**。标题窗与正文窗是两个独立的顶层 QWidget，均使用
   ``WA_TranslucentBackground + FramelessWindowHint + WindowStaysOnTopHint
   + Tool`` 实现屏幕上只可见文字本身；文字以常规 QFont（Black 字重）+ 统一
   淡蓝色直接绘制，背后逐行绘制半透明黑色蒙版条带提升对比度。标题窗在正文窗
   上方、水平左对齐，一次性整显完整标题（无打字机动画）；正文窗以打字机节拍
   逐字弹出（积压越多消化越快），带出现动画，平滑滚动自动跟随最新内容，滚轮
   上翻可回看当前对话全文、回到底部恢复跟随。

2. **定位全部走 win32 物理像素**。跟随游戏窗口复用 :func:`compute_geometry`
   （整体高度 = 标题高 + 间距 + 正文高），经 :func:`pair_layout` 拆成两窗后用
   ``move_window`` 成对定位；锁定态周期性对两窗重申 TOPMOST。Qt 侧不做 Qt 坐标
   移动，拖动时把 Qt 鼠标逻辑坐标乘 ``devicePixelRatioF`` 换算回物理像素。

3. **交互**。左键按住标题或正文任一窗口拖动即整体联动；双击切换位置锁定
   （中止在途翻译）；锁定态单击把最近捕获的对话原文经 SSE 流式接口发给
   OpenAI 兼容服务，译文逐块流入正文窗；``auto_translate`` 开启时锁定状态
   按间隔自动翻译（两级缓存命中直接上屏）。单击/双击用"延迟判定"区分。

4. **线程模型**。Qt 只能在创建它的线程里操作，所有跨线程输入（IPC 回调、
   控制台命令、翻译 chunk / 结果）都进队列，由主线程 QTimer（80ms）统一消费；
   流式翻译在守护线程中执行，逐块投递，世代号（seq）校验使作废结果不再上屏，
   stop Event 让中止的请求在 SSE 行之间尽快退出。
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

from . import config, translation_store, translator, win32api
from .quick_menu import QuickMenu
from .screenshot import (
    ScreenshotHistoryWindow,
    capture_region,
    constrain_aspect_ratio,
    encode_jpeg_base64,
    encode_jpeg_bytes,
    make_thumbnail,
    open_store,
    scale_to_percent,
)
from .screenshot.vision import translate_image
from .translation_cache import TranslationCache

logger = logging.getLogger("renpy_overlay.stream_window")

DRAIN_INTERVAL_MS = 80
FOLLOW_INTERVAL_MS = 200
CLICK_DELAY_MS = win32api.double_click_time_ms() + 60
CLICK_MOVE_TOLERANCE = 4
CLICK_SUPPRESS_SECONDS = 0.5
#: SW_HIDE 返回后等待 DWM 合成移除旧帧的时间（秒）：约 10 个 60Hz 合成周期，
#  保证截图不会抓到刚隐藏的悬浮窗残影（否则与流式窗重叠时会把译文截进图里）
SCREENSHOT_DWM_SETTLE_SECONDS = 0.18

#: 视觉参数：取值沿用参考项目的已验证方案，按对话窗缩小边距
BODY_MARGIN = 24.0  # 正文窗文本边距（逻辑像素，参考项目全屏窗用 64）
SPAWN_DURATION = 0.55  # 单字出现动画时长（秒）
TYPE_INTERVAL_MS = 16  # 打字机节拍（~60 字/秒，积压越多消化越快）
RENDER_INTERVAL_MS = 16  # 动画刷新周期
WHEEL_LINES_PER_NOTCH = 3.0  # 滚轮每格回退/推进的行数
TITLE_MARGIN_X = 16.0  # 标题文本左起点（逻辑像素）
TITLE_GLOW_FACTOR = 1.7  # 标题窗高度余量的字号倍数（保持既有几何外观）
BODY_FILL_COLOR = "#ADD8E6"  # 标题与正文统一的淡蓝色填充

#: 文字背后的半透明黑色蒙版（提升游戏画面上的对比度）：仅覆盖有文字的行区域，
#: 行距区域保持透明；alpha 取 110/255 ≈ 43%（可读性与遮挡感的平衡取值）
MASK_COLOR = "#000000"
MASK_ALPHA = 110
MASK_PAD_X = 6.0  # 条带水平内边距：文字与蒙版边缘不直接贴合
MASK_PAD_Y = 3.0  # 条带垂直内收：保证相邻两行蒙版之间留出空隙
MASK_RADIUS = 4.0  # 条带圆角半径

MARGIN = 12
DOCK_CHOICES = (
    "top-center",
    "top-left",
    "top-right",
    "bottom-center",
    "bottom-left",
    "bottom-right",
)


def compute_geometry(
    game_rect: tuple[int, int, int, int],
    size: tuple[int, int],
    dock: str,
    user_offset: tuple[int, int] | None = None,
    margin: int = MARGIN,
) -> tuple[int, int, int, int]:
    """计算悬浮窗的目标几何 ``(x, y, 宽, 高)``（屏幕物理像素）。

    - ``user_offset`` 为 None：按 ``dock`` 贴靠游戏窗口；
    - ``user_offset`` 存在：用户手动拖动过，位置 = 游戏窗口左上角 + 偏移，
      尺寸仍按游戏窗口约束缩放 —— 游戏窗口移动/改变大小时悬浮窗跟随，
      但保持用户设定的相对位置。

    抽成纯函数便于离线验证坐标计算（见 ``tests/test_stream_window.py``）。
    """
    left, top, right, bottom = game_rect
    game_w, game_h = max(1, right - left), max(1, bottom - top)
    width = max(240, min(size[0], game_w - 2 * margin))
    height = max(80, min(size[1], game_h // 2))

    if user_offset is not None:
        return left + user_offset[0], top + user_offset[1], width, height

    if dock.startswith("top"):
        y = top + margin
    else:
        y = bottom - height - margin
    if dock.endswith("left"):
        x = left + margin
    elif dock.endswith("right"):
        x = right - width - margin
    else:
        x = left + (game_w - width) // 2
    return x, y, width, height


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


def pair_layout(
    total: tuple[int, int, int, int],
    title_h: int,
    gap: int,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """把整体几何 ``(x, y, 宽, 高)`` 拆成 ``(标题窗几何, 正文窗几何)``。

    两窗水平左对齐；标题窗在正文窗正上方，中间空出 ``gap`` 像素。
    抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    x, y, width, height = total
    title = (x, y, width, max(1, title_h))
    body = (x, y + title_h + gap, width, max(1, height - title_h - gap))
    return title, body


def pair_offset_from_body(
    body_xy: tuple[int, int],
    game_xy: tuple[int, int],
    title_h: int,
    gap: int,
) -> tuple[int, int]:
    """由正文窗左上角换算 ``user_offset``（参照整体几何顶点 = 标题窗左上角）。

    跟随循环的 ``compute_geometry`` 把偏移加在整体几何上，再经 :func:`pair_layout`
    把正文窗放在 ``y + title_h + gap``；若拖动结束时以正文窗为参照记偏移，
    松手后首次重定位会整体下移「标题高 + 间距」（表现为一次向下跳变）。
    抽成纯函数便于离线验证两套逻辑参照系一致（见 ``tests/test_stream_window.py``）。
    """
    return (body_xy[0] - game_xy[0], body_xy[1] - title_h - gap - game_xy[1])


def body_mask_bands(
    spans: list[tuple[int, float, float]],
    line_h: float,
    box_h: float,
) -> list[tuple[float, float, float, float]]:
    """按行聚合已排版字符，返回正文蒙版条带矩形 ``(x, y, w, h)``（内容坐标系）。

    ``spans`` 为 ``(行号, 字符 x 起点, 字符前进宽)`` 列表；条带只覆盖实际有
    文字的行（打字机过程中天然只盖住已出现的字符），高度基于
    ``min(字形盒高, 行高)`` 并向内收 ``MASK_PAD_Y``，保证相邻行蒙版之间留出
    透明空隙。抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    if not spans:
        return []
    band_h = max(1.0, min(box_h, line_h) - 2 * MASK_PAD_Y)
    rows: dict[int, list[float]] = {}
    for row, x, advance in spans:
        span = rows.get(row)
        if span is None:
            rows[row] = [x, x + advance]
        else:
            span[0] = min(span[0], x)
            span[1] = max(span[1], x + advance)
    bands: list[tuple[float, float, float, float]] = []
    for row in sorted(rows):
        x0, x1 = rows[row]
        center_y = BODY_MARGIN + row * line_h + box_h / 2.0
        bands.append(
            (x0 - MASK_PAD_X, center_y - band_h / 2.0, (x1 - x0) + 2 * MASK_PAD_X, band_h)
        )
    return bands


def choice_translation_text(items: list[str]) -> str:
    """分支选项的翻译输入：编号逐行拼接（与正文里的编号选项列表一致）。

    以它为翻译键 / 缓存键：同一菜单的选项组合稳定，回退重放与存档载入可
    命中两级缓存；保留序号让模型输出的译文与选项一一对应。空列表返回空串。
    """
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, 1))


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
    """标题窗：一次性整显完整标题（随交互状态动态更新，但无打字机动画）。"""

    def __init__(self, font_px: int, host) -> None:
        font = _stream_font(font_px)
        super().__init__("renpy-overlay · stream title", font, host)
        self._layout: list[tuple[str, float]] = []  # (字符, x 起点)，单行排版

    def set_text(self, text: str) -> None:
        """整显一条标题（单行）：超宽或换行后的内容直接截断。"""
        self._layout.clear()
        fm = self.fm
        x = TITLE_MARGIN_X
        max_w = max(60.0, self.width() - 2 * TITLE_MARGIN_X)
        for ch in text:
            if ch == "\n":
                break
            advance = fm.horizontalAdvance(ch)
            if x + advance > max_w:
                break
            self._layout.append((ch, x))
            x += advance
        self.update()

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
    """正文窗：打字机式逐字弹出的流式文本，平滑滚动 + 滚轮回看。"""

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
        self._scroll_target = 0.0  # 滚动目标：自动跟随与滚轮回顾共用的收敛点
        self._follow = True  # True=跟随最新文字；滚轮上翻回顾时暂停

        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(TYPE_INTERVAL_MS)
        self._typing_timer.timeout.connect(self._tick_type)
        self._typing_timer.start()
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self.update)
        self._render_timer.start()

    # ---- 对外接口 ---------------------------------------------------------

    def begin_stream(self) -> None:
        """开始一段新的内容：清屏并恢复自动跟随（由宿主在内容切换时调用）。"""
        self.clear()

    def feed_text(self, text: str) -> None:
        """向待显示队列追加流式文本。"""
        self._pending.extend(text)

    def clear(self) -> None:
        self._glyphs.clear()
        self._pending.clear()
        self._line = 0
        self._line_used = 0.0
        self._line_empty = True
        self._scroll = 0.0
        self._scroll_target = 0.0
        self._follow = True
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

        # —— 滚动：自动跟随最新文字 / 滚轮回顾历史，统一向 _scroll_target 缓动 ——
        max_scroll = self._max_scroll()
        if self._follow:
            # 吸附到整行，避免顶部裁出半行、底部露出空行
            self._scroll_target = math.floor(max_scroll / self._line_h) * self._line_h
        else:
            self._scroll_target = min(max(self._scroll_target, 0.0), max_scroll)
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
        """滚轮回顾当前对话全文：上翻回退并暂停自动跟随，回到底部恢复跟随。"""
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y() * 8
        if delta == 0:
            event.ignore()
            return
        notches = delta / 120.0
        self._scroll_target = min(
            max(self._scroll_target - notches * WHEEL_LINES_PER_NOTCH * self._line_h, 0.0),
            self._max_scroll(),
        )
        self._follow = notches < 0 and self._scroll_target >= self._max_scroll() - 0.5
        event.accept()


class StreamOverlayWindow:
    """流式悬浮窗门面：管理标题 + 正文双窗口，暴露队列化的对外接口。"""

    def __init__(
        self,
        target_pid: int,
        dock: str = "top-center",
        width: int = config.DEFAULT_STREAM_WINDOW_WIDTH,  # 正文窗尺寸来自 config.json
        height: int = config.DEFAULT_STREAM_WINDOW_HEIGHT,
        app_config: config.AppConfig | None = None,
        game_dir: str = "",
        on_quit=None,
    ):
        self.target_pid = target_pid
        self.dock = dock if dock in DOCK_CHOICES else "top-center"
        self.width = width
        self.height = height
        self.on_quit = on_quit
        self._config = app_config or config.AppConfig()
        self._title_gap = max(0, self._config.stream_window_title_gap)

        # QApplication 必须先于任何 QWidget；进程此前可能已创建（复用之）
        self._app = QApplication.instance() or QApplication([])
        self._body_window = BodyWindow(
            self._config.stream_window_font_size,
            self._config.stream_window_line_spacing,
            host=self,
        )
        self._title_window = TitleWindow(
            self._config.stream_window_title_font_size, host=self
        )
        self._body_window.resize(width, height)
        self._title_window.resize(width, 10)  # 真实高度随字体计算后由跟随循环设定
        self._body_window.move(100, 100)
        self._title_window.move(100, 100)

        # 标题窗物理高度：字体盒 + 上下余量（TITLE_GLOW_FACTOR，保持既有几何
        # 外观），按窗口 DPR 从逻辑像素换算
        title_h_logical = self._title_window.fm.height() + TITLE_GLOW_FACTOR * (
            self._config.stream_window_title_font_size
        )
        self._title_h = max(12, int(math.ceil(title_h_logical * self._body_window.dpr())))
        self._title_window.resize(width, self._title_h)

        self._body_hwnd = self._body_window.hwnd
        self._title_hwnd = self._title_window.hwnd
        logger.debug(
            "流式悬浮窗 HWND：正文=0x%X 标题=0x%X（标题高=%dpx 间距=%dpx）",
            self._body_hwnd,
            self._title_hwnd,
            self._title_h,
            self._title_gap,
        )

        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._target_hwnd = 0
        self._visible = False  # 首次定位成功后才显示，避免旧位置闪烁
        self._locked = False
        self._dragging = False
        self._drag_grab = (0, 0)
        self._drag_size = (width, height)
        self._drag_origin = (0, 0)
        self._user_offset: tuple[int, int] | None = None
        self._last_topmost_log = 0.0
        self._press_pos: tuple[int, int] | None = None
        self._click_suppress_until = 0.0
        self._title_text = ""
        self._last_say: dict | None = None
        self._translating = False
        self._translation_seq = 0
        self._stream_seq = 0  # 正文窗正在流式呈现的翻译世代号（0 = 非翻译内容）
        self._current_stop: threading.Event | None = None
        self._translation_cache = TranslationCache(
            max_bytes=self._config.translation_cache_size_kb * 1024
        )
        self._translation_store = translation_store.open_store(game_dir or None)
        # 截图翻译：独立数据库 + 快捷菜单（窗口池），历史窗口单例惰性创建
        self._screenshot_store = open_store(game_dir or None)
        self._quick_menu = QuickMenu(
            on_capture_click=self.request_screenshot_translation,
            on_open_history=self._open_history,
        )
        self._history_window: ScreenshotHistoryWindow | None = None
        self._screenshot_suppress = False  # 截图瞬间抑制跟随循环重新显示
        self._context_menu_open = False  # 右键菜单打开期间暂停周期性置顶重申
        self._last_translation_input: str | None = None
        self._lock_started_at = 0.0
        self._auto_skip_reason: str | None = None

        self._drain_timer = QTimer()
        self._drain_timer.setInterval(DRAIN_INTERVAL_MS)
        self._drain_timer.timeout.connect(self._drain)
        self._follow_timer = QTimer()
        self._follow_timer.setInterval(FOLLOW_INTERVAL_MS)
        self._follow_timer.timeout.connect(self._follow)
        self._click_timer = QTimer()
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(CLICK_DELAY_MS)
        self._click_timer.timeout.connect(self._on_delayed_click)
        self._auto_timer = QTimer()
        self._auto_timer.timeout.connect(self._auto_translate_tick)
        self._update_title("正在启动…")

    # ------------------------------------------------------------ 对外接口

    def set_status(self, text: str) -> None:
        self._queue.put(("status", text))

    def push_say(self, who: str, what: str, source: str = "", ts: float | None = None) -> None:
        self._queue.put(("say", {"who": who, "what": what, "src": source, "ts": ts or time.time()}))

    def push_choice(self, items: list[str], who: str = "", what: str = "") -> None:
        """分支选项出现（跨线程安全，经队列回主线程展示）。"""
        self._queue.put(("choice", {"items": list(items), "who": who, "what": what}))

    def push_choice_pick(self, index: int, caption: str) -> None:
        """玩家做出选择（跨线程安全，经队列回主线程展示）。"""
        self._queue.put(("choice_pick", {"index": index, "caption": caption}))

    def hint(self, text: str) -> None:
        self._queue.put(("hint", text))

    def toggle_visible(self) -> None:
        self._queue.put(("cmd", "toggle"))

    def reset_position(self) -> None:
        """恢复"自动停靠"（清除手动拖动锁定的位置）。"""
        self._queue.put(("cmd", "dock"))

    def request_close(self) -> None:
        self._queue.put(("cmd", "quit"))

    @property
    def hwnd(self) -> int:
        return self._body_hwnd

    @property
    def visible(self) -> bool:
        return self._visible

    # ------------------------------------------------------------ 主循环

    def run(self) -> None:
        """启动 Qt 事件循环（阻塞）。返回即代表窗口已销毁。"""
        self._drain_timer.start()
        self._follow_timer.start()
        self._start_auto_translate()
        try:
            self._app.exec()
        finally:
            self._closed = True
            logger.debug("流式悬浮窗已销毁")

    def close(self) -> None:
        """从任意线程请求关闭（清理兜底路径）。"""
        try:
            self._app.quit()
        except Exception:  # pragma: no cover - 已销毁
            pass

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._translation_seq += 1  # 在途翻译的结果作废
        if self._translating:
            self._abort_translation("窗口关闭")
        self._cancel_pending_click()
        for timer in (
            self._drain_timer,
            self._follow_timer,
            self._click_timer,
            self._auto_timer,
        ):
            try:
                timer.stop()
            except Exception:  # pragma: no cover
                pass
        if self._translation_store is not None:
            self._translation_store.close()
            self._translation_store = None
        if self._screenshot_store is not None:
            self._screenshot_store.close()
            self._screenshot_store = None
        self._quick_menu.close_all()
        if self._history_window is not None:
            try:
                self._history_window.deleteLater()
            except Exception:  # pragma: no cover
                pass
            self._history_window = None
        for window in (self._title_window, self._body_window):
            try:
                window.close()
            except Exception:  # pragma: no cover
                pass
        try:
            self._app.quit()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 队列消费

    def _drain(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "say":
                    self._handle_say(payload)
                elif kind == "choice":
                    self._handle_choice(payload)
                elif kind == "choice_pick":
                    self._handle_choice_pick(payload)
                elif kind == "hint":
                    # 正文只保留最新一条内容：提示语同样整段替换显示
                    self._display_text(payload)
                elif kind == "status":
                    self._update_title(payload)
                elif kind == "chunk":
                    self._handle_chunk(payload)
                elif kind == "translation":
                    self._handle_translation(payload)
                elif kind == "screenshot_done":
                    self._handle_screenshot_done(payload)
                elif kind == "screenshot_fail":
                    self._handle_screenshot_fail(payload)
                elif kind == "cmd":
                    self._handle_command(payload)
        except queue.Empty:
            pass
        except Exception:  # pragma: no cover - UI 异常不应终止循环
            logger.exception("处理界面消息时出错")

    def _handle_command(self, command: str) -> None:
        if command == "toggle":
            self._set_visible(not self._visible)
        elif command == "show":
            self._set_visible(True)
        elif command == "hide":
            self._set_visible(False)
        elif command == "dock":
            self._reset_to_dock()
        elif command == "quit":
            if callable(self.on_quit):
                try:
                    self.on_quit()
                except Exception:  # pragma: no cover
                    logger.exception("退出回调失败")
            self._shutdown()

    def _handle_say(self, payload: dict) -> None:
        who = str(payload.get("who") or "")
        what = str(payload.get("what") or "")
        self._last_say = {"who": who, "what": what}
        preview = what.strip()[:20]
        if preview:
            # 标题提醒：这条对话就是翻译时将发送的文本（不受显示开关影响）
            self._update_title(preview)
        if self._config.show_original_text:
            self._display_text((who + "\n" if who.strip() else "") + what + "\n")
            logger.debug("显示对话：[%s] %s", who or "-", what[:60])
        else:
            # 正文区不刷新，但原文已记录，翻译/自动翻译链路不受影响
            logger.debug(
                "已捕获对话（show_original_text=false，正文区不更新）：[%s] %s",
                who or "-",
                what[:60],
            )

    def _display_text(self, text: str) -> None:
        """用打字机方式整段替换正文内容（窗口只保留最新一条，不累积历史）。"""
        self._stream_seq = 0
        self._body_window.begin_stream()
        self._body_window.feed_text(text)

    def _handle_choice(self, payload: dict) -> None:
        """分支选项出现：标题显示选项预览，翻译目标切换为选项文本本身。

        正文展示与对话原文同受 ``show_original_text`` 约束：为 true 时整段替换
        为「上文 + 编号选项列表」（打字机呈现）；为 false 时选项原文不刷新到
        正文（正文保持上一条内容），但标题预览、翻译目标（选项文本编号拼接，
        兼作缓存键）与去重重置照常进行 —— 玩家仍能拿到选项译文，选择后的
        「→ 已选择：…」提示也不受影响。
        """
        items = [
            str(item).strip()
            for item in (payload.get("items") or [])
            if str(item).strip()
        ]
        who = str(payload.get("who") or "").strip()
        what = str(payload.get("what") or "").strip()
        choice_text = choice_translation_text(items)
        if choice_text:
            self._last_say = {"who": "", "what": choice_text}
            # 允许自动翻译重新评估该选项文本：同一菜单重放/载入时缓存命中
            # 直接上屏，首次出现则发起流式翻译（下个轮询间隔内生效）
            self._last_translation_input = None
        if self._config.show_original_text:
            lines = ([what] if what else []) + [
                f"{number}. {item}" for number, item in enumerate(items, 1)
            ]
            if any(line.strip() for line in lines):
                self._display_text("\n".join(lines) + "\n")
        if items:
            self._update_title("选项：" + " / ".join(items))
        else:
            self._update_title("出现选项")
        if not self._config.show_original_text:
            logger.debug(
                "已捕获分支选项（show_original_text=false，正文区不更新）："
                "%s",
                " / ".join(items) or "<空>",
            )
        logger.info(
            "出现分支选项（%d 项）：%s；上文上下文：[%s] %s；翻译目标=%d 字",
            len(items),
            " / ".join(items) or "<空>",
            who or "-",
            what[:60],
            len(choice_text),
        )

    def _handle_choice_pick(self, payload: dict) -> None:
        """玩家做出选择：正文追加结果提示行，标题同步更新（不清屏）。"""
        try:
            index = int(payload.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        caption = str(payload.get("caption") or "").strip()
        label = f"{index}. {caption}" if index >= 0 and caption else (caption or "未知选项")
        self._body_window.feed_text(f"→ 已选择：{label}\n")
        self._update_title(f"已选择：{label}")
        logger.info("玩家做出选择：%s", label)

    def _handle_chunk(self, payload: dict) -> None:
        seq = payload.get("seq")
        if seq != self._translation_seq:
            logger.debug("翻译增量已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        if self._stream_seq != seq:  # 该轮译文的第一个增量：清屏开新流
            self._stream_seq = seq
            self._body_window.begin_stream()
        self._body_window.feed_text(str(payload.get("text") or ""))

    def _update_title(self, text: str) -> None:
        """标题窗状态文案的唯一出口：随交互状态动态更新，变化时写入日志。"""
        if self._title_text == text:
            return
        self._title_text = text
        self._title_window.set_text(text)
        logger.debug("标题窗状态更新：%s", text)

    def _set_visible(self, visible: bool) -> None:
        try:
            if visible:
                self._title_window.show()
                self._body_window.show()
            else:
                self._title_window.hide()
                self._body_window.hide()
        except Exception:  # pragma: no cover
            return
        self._visible = visible
        logger.debug("流式悬浮窗可见性：%s", visible)

    # ------------------------------------------------------------ 跟随与定位

    def _resolve_target(self) -> int:
        hwnd = self._target_hwnd
        if hwnd and win32api.is_window_valid(hwnd):
            return hwnd
        hwnd = win32api.find_main_window(self.target_pid) or 0
        if hwnd != self._target_hwnd:
            logger.info("已定位游戏窗口：HWND=0x%X", hwnd)
        self._target_hwnd = hwnd
        return hwnd

    def _follow(self) -> None:
        if self._closed:
            return
        try:
            self._follow_once()
        except Exception:  # pragma: no cover - 窗口在跟随过程中关闭
            logger.exception("跟随游戏窗口时出错")

    def _follow_once(self) -> None:
        if self._screenshot_suppress:
            # 截图瞬间：不解析目标、不重新显示，防止 200ms 跟随循环把隐藏的窗口拉回
            return
        hwnd = self._resolve_target()
        if not hwnd:
            self._update_title(f"等待 pid={self.target_pid} 的游戏窗口…")
            self._hide_if_visible()
            return
        if win32api.is_minimized(hwnd):
            self._hide_if_visible()
            return
        if self._locked or self._dragging:
            # 位置已锁定 / 拖动中：跟随逻辑不移动窗口，但锁定态必须周期性对两窗
            # 重申 TOPMOST（独占全屏的游戏窗口被激活时会盖住其它 topmost 窗口）。
            if self._locked and not self._dragging:
                self._reassert_topmost()
            return

        rect = win32api.window_rect(hwnd)
        total = compute_geometry(
            rect,
            (self.width, self.height + self._title_h + self._title_gap),
            self.dock,
            self._user_offset,
        )
        title_rect, body_rect = pair_layout(total, self._title_h, self._title_gap)
        if not self._visible:
            # 显示必须走 Qt show()：win32 的 SWP_SHOWWINDOW 只能在 OS 层面把原生
            # 窗口标记为可见，绕过 Qt 的 show 流程后 Qt 不会启动渲染管线，
            # 对全透明窗口来说等于不存在（实测表现为"窗口从未出现"）。
            self._set_visible(True)
        win32api.move_window(self._body_hwnd, *body_rect, topmost=True)
        win32api.move_window(self._title_hwnd, *title_rect, topmost=True)

    def _hide_if_visible(self) -> None:
        if self._visible:
            self._set_visible(False)

    def _reassert_topmost(self) -> None:
        """把两窗重新压回最顶层（不改位置/尺寸/焦点）；日志节流避免刷屏。"""
        if self._context_menu_open:
            return  # 右键菜单打开期间不与菜单竞争 TOPMOST 层组的顶部位置
        try:
            win32api.set_topmost(self._body_hwnd)
            win32api.set_topmost(self._title_hwnd)
            self._quick_menu.reassert_topmost()  # 截图窗锁定态同样对抗独占全屏覆盖
        except Exception:  # pragma: no cover - pywin32 缺失等
            return
        now = time.time()
        if now - self._last_topmost_log > 10.0:
            self._last_topmost_log = now
            logger.debug("锁定态周期性重申置顶（应对独占全屏被激活时的覆盖）")

    def _reset_to_dock(self) -> None:
        """清除手动拖动的位置，回到自动停靠。"""
        if self._user_offset is None:
            logger.info("当前已是自动停靠位置，无需恢复。")
            return
        self._user_offset = None
        logger.info("已恢复自动停靠位置（dock=%s）", self.dock)
        try:
            self._follow_once()
        except Exception:  # pragma: no cover
            logger.debug("恢复停靠时立即定位失败", exc_info=True)

    # ------------------------------------------------------------ 鼠标拖动

    def _physical_mouse(self, window: QWidget, event) -> tuple[int, int]:
        """Qt 逻辑全局坐标 → 屏幕物理像素（与 win32 坐标系对齐）。"""
        point = event.globalPosition() * window.dpr()
        return int(point.x()), int(point.y())

    def on_window_press(self, window: QWidget, event) -> None:
        """左键按下：记录点击候选；未锁定时开始整体拖动（两窗联动）。"""
        if self._closed:
            return
        pos = self._physical_mouse(window, event)
        self._press_pos = pos
        if self._locked:
            logger.debug("位置已锁定，忽略拖动按下")
            return
        left, top, right, bottom = win32api.window_rect(self._body_hwnd)
        self._dragging = True
        self._drag_grab = (pos[0] - left, pos[1] - top)
        self._drag_size = (max(1, right - left), max(1, bottom - top))
        self._drag_origin = (left, top)
        logger.info(
            "开始拖动流式悬浮窗：正文窗位于 (%d, %d)，鼠标抓取点 (%d, %d)",
            left,
            top,
            pos[0] - left,
            pos[1] - top,
        )

    def on_window_motion(self, window: QWidget, event) -> None:
        """拖动中：两窗跟随鼠标整体移动（保持抓取点相对位置不变）。"""
        if not self._dragging:
            return
        pos = self._physical_mouse(window, event)
        body_x = pos[0] - self._drag_grab[0]
        body_y = pos[1] - self._drag_grab[1]
        try:
            win32api.move_window(
                self._body_hwnd,
                body_x,
                body_y,
                self._drag_size[0],
                self._drag_size[1],
                topmost=True,
                resize=False,
            )
            win32api.move_window(
                self._title_hwnd,
                body_x,
                body_y - self._title_h - self._title_gap,
                self._drag_size[0],
                self._title_h,
                topmost=True,
                resize=False,
            )
        except Exception:  # pragma: no cover - 窗口在拖动中销毁
            self._dragging = False

    def on_window_release(self, window: QWidget, event) -> None:
        if self._closed:
            return
        if self._dragging:
            self._finish_drag()
        press_pos = self._press_pos
        self._press_pos = None
        if press_pos is None:
            return
        pos = self._physical_mouse(window, event)
        if (
            abs(pos[0] - press_pos[0]) > CLICK_MOVE_TOLERANCE
            or abs(pos[1] - press_pos[1]) > CLICK_MOVE_TOLERANCE
        ):
            return  # 移动过 = 拖动，不算单击
        if time.time() < self._click_suppress_until:
            return  # 双击/三击的余波，不调度单击
        self._schedule_click()

    def _finish_drag(self) -> None:
        """结束拖动：把当前位置换算成相对游戏窗口的偏移并锁定（供跟随逻辑使用）。"""
        self._dragging = False
        try:
            rect = win32api.window_rect(self._body_hwnd)
        except Exception:  # pragma: no cover - 窗口已销毁
            return
        if (rect[0], rect[1]) == self._drag_origin:
            logger.debug("拖动结束：位置未变化，保持原有跟随方式")
            return
        hwnd = self._resolve_target()
        if not hwnd:
            logger.warning("拖动结束：未能定位游戏窗口，本次位置不参与跟随")
            return
        game = win32api.window_rect(hwnd)
        # user_offset 必须与跟随循环的整体几何顶点（标题窗左上角）同参照系，
        # 否则松手后首次重定位会整体下移「标题高 + 间距」（见 pair_offset_from_body）
        offset = pair_offset_from_body(
            (rect[0], rect[1]),
            (game[0], game[1]),
            self._title_h,
            self._title_gap,
        )
        self._user_offset = offset
        logger.info(
            "拖动结束：位置已锁定为相对游戏窗口的偏移 (%+d, %+d)（控制台输入 d 可恢复自动停靠）",
            offset[0],
            offset[1],
        )

    # ------------------------------------------------------------ 双击锁定与单击翻译

    def on_window_double_click(self, window: QWidget, event) -> None:
        """双击：中止在途翻译并切换位置锁定。"""
        if self._closed:
            return
        self._cancel_pending_click()
        self._click_suppress_until = time.time() + CLICK_SUPPRESS_SECONDS
        if self._dragging:
            # 双击的第二次按下可能顺带开了拖动，直接取消，避免它的 release 产生副作用
            self._dragging = False
        self._abort_translation("双击")
        self._toggle_lock()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        if self._locked:
            self._lock_started_at = time.time()
            if self._config.auto_translate:
                logger.info("已锁定悬浮窗位置（自动翻译开启：等一个轮询间隔后开始）")
                self._update_title("已锁定，开始自动翻译")
            else:
                logger.info("已锁定悬浮窗位置（双击解锁；锁定状态下单击触发翻译）")
                self._update_title("已锁定，单击启动翻译")
        else:
            logger.info("已解锁悬浮窗位置（恢复鼠标拖动与自动跟随；自动翻译停用）")
            self._update_title("已解锁，中止翻译")

    def _schedule_click(self) -> None:
        """延迟判定单击：等一个系统双击间隔，双击会取消该任务。"""
        self._cancel_pending_click()
        self._click_timer.start()

    def _cancel_pending_click(self) -> None:
        self._click_timer.stop()

    def _on_delayed_click(self) -> None:
        if not self._locked:
            logger.debug("未锁定状态，单击不触发翻译")
            return
        logger.debug("锁定状态下的单击：请求翻译当前对话原文")
        self._start_translation()

    # ------------------------------------------------------------ 翻译（流式）

    def _start_translation(self, origin: str = "manual") -> None:
        """发起流式翻译请求（仅主线程调用）。同一时刻只允许一条在途请求。"""
        label = "自动触发" if origin == "auto" else "单击触发"
        if self._translating:
            logger.info("已有翻译请求在途，忽略本次%s翻译", label)
            return
        say = self._last_say or {}
        what = str(say.get("what") or "").strip()
        if not what:
            logger.info("暂无可翻译的捕获文本，跳过翻译请求")
            return
        who = str(say.get("who") or "").strip()
        self._translating = True
        self._translation_seq += 1
        seq = self._translation_seq
        self._last_translation_input = what  # 记录已触发的原文，自动翻译据此去重
        logger.info("发起流式翻译请求（%s，seq=%d，原文 %d 字）：%s", label, seq, len(what), what[:40])
        self._update_title("翻译中...")
        api_options = {  # 连接参数来自 config.json（未配置时为与历史一致的默认值）
            "base_url": self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": self._config.model,
            "system_prompt": self._config.system_prompt,
            "api_key": self._config.api_key,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
        }
        thread = threading.Thread(
            target=self._translate_worker,
            args=(seq, who, what, origin, api_options),
            name=f"stream-translate-{seq}",
            daemon=True,
        )
        thread.start()

    def _translate_worker(
        self, seq: int, who: str, what: str, origin: str, api_options: dict
    ) -> None:
        """后台线程：SSE 流式翻译，增量逐块入队，结果只经队列回主线程。"""
        stop = threading.Event()
        self._current_stop = stop
        chunks: list[str] = []
        try:
            for piece in translator.translate_text_stream(what, **api_options):
                if stop.is_set():
                    return  # 已作废：停止喂块，也不投递结束消息
                chunks.append(piece)
                self._queue.put(("chunk", {"seq": seq, "text": piece}))
            message = {
                "seq": seq,
                "ok": True,
                "who": who,
                "what": "".join(chunks),
                "input": what,  # 原文随结果回传，供主线程写缓存
                "origin": origin,
            }
        except Exception as exc:  # 网络/协议错误统一收敛为失败结果
            if stop.is_set():
                return
            logger.debug("翻译请求失败（seq=%d）：%s", seq, exc)
            message = {"seq": seq, "ok": False, "who": who, "error": str(exc), "origin": origin}
        self._queue.put(("translation", message))

    def _handle_translation(self, item: dict) -> None:
        """主线程：校验世代号后收尾（正文已由增量流式呈现完整译文）。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("翻译结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        if not item.get("ok"):
            logger.warning("翻译失败：%s", item.get("error"))
            self._update_title("翻译失败")  # 部分流内容保留在正文区，便于排查
            return
        who = str(item.get("who") or "").strip()
        text = str(item.get("what") or "").strip()
        if not text:
            logger.warning("翻译返回空文本，保持当前显示")
            self._update_title("翻译失败")
            return
        logger.info("翻译完成（seq=%s），译文已流式呈现（%d 字）", seq, len(text))
        if self._stream_seq != seq:  # 兜底：全程无有效增量时整段补显
            self._display_text((who + "\n" if who else "") + text + "\n")
        self._update_title("翻译完成")
        # 成功后同步写入两级缓存（手动 / 自动一致）：内存覆盖视为新写入，
        # 数据库 UPSERT 覆盖同一原文的旧译文；失败由各自模块内部降级并记日志
        input_text = str(item.get("input") or "").strip()
        if input_text:
            self._translation_cache.put(input_text, text)
            if self._translation_store is not None:
                self._translation_store.put(input_text, text)
            logger.debug(
                "翻译结果已写入缓存（%s触发，内存占用 %d/%d 字节）",
                "自动" if item.get("origin") == "auto" else "手动",
                self._translation_cache.size(),
                self._translation_cache.max_bytes,
            )
        else:  # pragma: no cover - 正常流程必然带原文
            logger.warning("翻译结果缺少原文，跳过缓存写入")

    def _abort_translation(self, reason: str) -> None:
        """作废在途翻译：世代号递增使结果不再上屏，stop 让网络等待尽快结束。"""
        if not self._translating:
            return
        self._translation_seq += 1
        self._translating = False
        stop = self._current_stop
        if stop is not None:
            stop.set()
        logger.info("已中止在途翻译（%s）：结果作废，界面保持当前状态", reason)

    # -- 截图翻译（右键菜单创建截图窗；锁定态单击触发；始终走 API 不检索缓存）

    def on_window_context_menu(self, window: QWidget, event) -> None:
        """右键转发终点：菜单构建与动作分发全部在 QuickMenu 内。

        exec 阻塞期间置位 ``_context_menu_open``，暂停锁定态每 200ms 的
        周期性置顶重申 —— 否则流式窗会被 reassert 到 TOPMOST 层组顶部，
        反过来盖住刚弹出的菜单。
        """
        self._context_menu_open = True
        try:
            self._quick_menu.show_context_menu(event.globalPos())
        finally:
            self._context_menu_open = False
            self._reassert_topmost()  # 菜单关闭后立即恢复两窗的置顶层级

    def _open_history(self) -> None:
        """打开截图历史窗口（单例复用，每次刷新数据）。"""
        if self._screenshot_store is None:
            self.hint("截图历史不可用：未定位到游戏目录或数据库初始化失败。")
            return
        if self._history_window is None:
            self._history_window = ScreenshotHistoryWindow(self._screenshot_store)
        self._history_window.refresh()
        self._history_window.show()
        self._history_window.raise_()
        self._history_window.activateWindow()
        logger.info("已打开截图历史窗口")

    def request_screenshot_translation(self, window) -> None:
        """截图窗口锁定态单击（主线程）：互斥检查 → 隐藏 → 截图 → 恢复 → 后台翻译。

        与单击/自动翻译共用 ``_translating`` 互斥：存在任意在途翻译则既不
        调 API 也不截图（防止译文被意外覆盖）。截图始终走 API、不检索缓存，
        成功不写两级缓存（需求）。
        """
        if self._closed:
            return
        if self._translating:
            logger.info("已有翻译请求在途（单击/自动/截图），忽略本次截图翻译")
            return
        try:
            bbox = win32api.window_rect(window.hwnd)
        except Exception:  # pragma: no cover - 窗口销毁竞态
            logger.warning("无法定位截图窗口区域，忽略本次截图翻译")
            return
        self._translating = True
        self._translation_seq += 1
        seq = self._translation_seq
        stop = threading.Event()
        self._current_stop = stop
        self._update_title("截图翻译中，翻译速度会更慢...")
        # 截图时短暂隐藏截图窗口与流式窗口，截图后发送 API 前恢复（需求时序）。
        # 记录 hide 前的物理矩形：show() 可能重应用 Qt 逻辑几何，而锁定态下跟随
        # 循环不做 move_window 矫正，漂移会永久残留（表现为正文窗宽度突变）
        self._screenshot_suppress = True
        self._quick_menu.hide_all()
        was_visible = self._visible
        saved_rects = None
        if was_visible:
            try:
                saved_rects = (
                    win32api.window_rect(self._body_hwnd),
                    win32api.window_rect(self._title_hwnd),
                )
            except Exception:  # pragma: no cover - 窗口销毁竞态
                saved_rects = None
            self._set_visible(False)
        try:
            # SW_HIDE 返回后 DWM 合成树可能尚未移除窗口（屏幕 DC 仍可抓到旧帧），
            # 与流式窗重叠时截图会混入悬浮窗画面（模型看到已有中文译文后不再翻译）。
            # 处理一拍事件并等待数个合成周期后再抓屏
            QApplication.processEvents()
            time.sleep(SCREENSHOT_DWM_SETTLE_SECONDS)
            image = capture_region(bbox)
        except Exception as exc:
            logger.warning("屏幕截图失败：%s", exc)
            image = None
        finally:
            self._screenshot_suppress = False
            self._quick_menu.show_all()
            if was_visible:
                self._set_visible(True)
                if saved_rects is not None:
                    # 用 hide 前的物理矩形原样恢复（锁定态无人矫正几何，必须显式还原）
                    try:
                        win32api.move_window(self._body_hwnd, *saved_rects[0], topmost=True)
                        win32api.move_window(self._title_hwnd, *saved_rects[1], topmost=True)
                    except Exception:  # pragma: no cover - 窗口销毁竞态
                        pass
        if image is None:
            self._translating = False
            self._update_title("翻译失败")
            return
        logger.info("发起截图翻译（seq=%d，区域 %dx%d）", seq, image.width, image.height)
        api_options = {  # 连接参数来自 config.json；模型用截图专用配置（可回退 model）
            "base_url": self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": self._config.screenshot_model,
            "api_key": self._config.api_key,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
        }
        thread = threading.Thread(
            target=self._screenshot_worker,
            args=(seq, image, stop, api_options),
            name=f"screenshot-translate-{seq}",
            daemon=True,
        )
        thread.start()

    def _screenshot_worker(self, seq: int, image, stop: threading.Event, api_options: dict) -> None:
        """后台线程：宽高比约束副本 → 压缩至配置百分比 → vision API → 结果入队。

        原始截图与副本均为线程局部变量：翻译失败随 return 自然清除（需求）；
        成功时在回传前生成 ≤10w 像素缩略图，主线程只负责展示与落库。
        """
        try:
            ratio_image = constrain_aspect_ratio(image)
            api_image = scale_to_percent(ratio_image, self._config.screenshot_compress_percent)
            jpeg_base64 = encode_jpeg_base64(api_image)
            if stop.is_set():
                return  # 已作废：不再发起请求
            text = translate_image(jpeg_base64, **api_options)
            if stop.is_set():
                return
            thumbnail = make_thumbnail(image)
            self._queue.put(
                (
                    "screenshot_done",
                    {
                        "seq": seq,
                        "text": text,
                        "original_jpeg": encode_jpeg_bytes(image),
                        "thumb_jpeg": encode_jpeg_bytes(thumbnail),
                    },
                )
            )
        except Exception as exc:  # 网络/协议/图像错误统一收敛为失败结果
            if stop.is_set():
                return
            logger.debug("截图翻译失败（seq=%d）：%s", seq, exc)
            self._queue.put(("screenshot_fail", {"seq": seq, "error": str(exc)}))

    def _handle_screenshot_done(self, item: dict) -> None:
        """主线程：校验世代号 → 正文显示译文 → 原图/缩略图落库（不写两级缓存）。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("截图翻译结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        text = str(item.get("text") or "").strip()
        if not text:
            logger.warning("截图翻译返回空文本，保持当前显示")
            self._update_title("翻译失败")
            return
        self._display_text(text + "\n")
        self._update_title("翻译完成")
        logger.info("截图翻译完成（seq=%d，译文 %d 字），已显示在正文窗", seq, len(text))
        if self._screenshot_store is None:
            return  # 数据库不可用：仅显示不入库
        record_id = self._screenshot_store.insert(
            ts=int(time.time() * 1000),  # 13 位毫秒时间戳（需求）
            ocr_text=text,
            img_original=bytes(item.get("original_jpeg") or b""),
            img_thumbnail=bytes(item.get("thumb_jpeg") or b""),
        )
        if record_id is None:
            logger.warning("截图记录入库失败，仅保留正文显示")

    def _handle_screenshot_fail(self, item: dict) -> None:
        """主线程：截图翻译失败收尾（提示与单击/自动一致；内存图像已随 worker 释放）。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("截图翻译失败结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        logger.warning("截图翻译失败：%s", item.get("error"))
        self._update_title("翻译失败")

    # -- 自动翻译（config.json: auto_translate / auto_translate_interval）

    def _start_auto_translate(self) -> None:
        """启动自动翻译轮询（仅在配置开启时）。"""
        if not self._config.auto_translate:
            logger.info("自动翻译未启用（config.json: auto_translate=false）")
            return
        interval_ms = self._auto_interval_ms()
        logger.info("自动翻译已启用：每 %.1f 秒轮询一次（仅锁定状态生效）", interval_ms / 1000.0)
        self._auto_timer.setInterval(interval_ms)
        self._auto_timer.start()

    def _auto_interval_ms(self) -> int:
        return max(200, int(self._config.auto_translate_interval * 1000))

    def _auto_translate_tick(self) -> None:
        """自动翻译轮询（主线程 QTimer，与 drain/follow 同模式）。"""
        if self._closed:
            return
        try:
            self._auto_translate_check()
        except Exception:  # pragma: no cover - 轮询异常不应终止循环
            logger.exception("自动翻译轮询出错")

    def _auto_translate_check(self) -> None:
        """条件全部满足时自动发起一次翻译；任一不满足则跳过（原因去重记日志）。"""
        if not self._config.auto_translate:
            return
        if not self._locked:
            self._auto_note_skip("未锁定")
            return
        if time.time() - self._lock_started_at < self._config.auto_translate_interval:
            self._auto_note_skip("刚进入锁定，等待一个完整轮询间隔")
            return
        say = self._last_say or {}
        who = str(say.get("who") or "").strip()
        what = str(say.get("what") or "").strip()
        if not what:
            self._auto_note_skip("暂无可翻译的捕获文本")
            return
        if what == self._last_translation_input:
            self._auto_note_skip("该条原文已翻译过")
            return

        # 缓存命中优先：依次查内存与数据库，任一级命中则直接上屏并记录"已翻译"
        cached = self._translation_cache.get(what)
        if cached is not None:
            self._auto_use_cached(what, who, cached, "缓存")
            return
        logger.debug("自动翻译内存缓存未命中，继续查数据库（原文 %d 字）", len(what))
        stored = self._translation_store.get(what) if self._translation_store is not None else None
        if stored is not None:
            if self._translation_cache.put(what, stored):
                logger.info("已将数据库译文回填内存缓存（原文 %d 字）", len(what))
            self._auto_use_cached(what, who, stored, "数据库")
            return
        logger.debug("自动翻译两级缓存均未命中，改走流式 API 请求（原文 %d 字）", len(what))

        if self._translating:
            self._auto_note_skip("已有翻译请求在途")
            return
        self._auto_skip_reason = None
        self._start_translation(origin="auto")

    def _auto_use_cached(self, what: str, who: str, translated: str, source: str) -> None:
        """自动翻译缓存命中（内存或数据库）：直接上屏，不发起网络请求。"""
        self._auto_skip_reason = None
        self._last_translation_input = what
        if self._translating:
            # 在途请求针对的是旧原文：作废它，避免其结果稍后覆盖本次命中内容
            self._abort_translation(f"自动翻译{source}命中")
        logger.info("自动翻译%s命中（原文 %d 字）：直接上屏，未发起网络请求", source, len(what))
        self._update_title("翻译完成")
        self._display_text((who + "\n" if who else "") + translated + "\n")

    def _auto_note_skip(self, reason: str) -> None:
        if reason == self._auto_skip_reason:
            return
        self._auto_skip_reason = reason
        logger.debug("自动翻译跳过：%s", reason)
