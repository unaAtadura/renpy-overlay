"""流式悬浮窗的 style=2 渲染层：半透明黑色无边框双窗（标题 + 正文）。

由 config.json 的 ``translate_window_style=2`` 启用（经
:func:`resolve_window_classes` 工厂在宿主装配点切换）；style=1 走
:mod:`.windows` 既有实现，本模块不改动其任何代码。

- **视觉**（需求 future_流式输出窗口_窗口样式，取值沿用参考项目
  screen_translaor_4.0 的已验证方案）：整窗半透明黑色圆角背景（替代逐行
  蒙版条带）、白色文字、无蓝色外边框、无缩放手柄/边缘高亮/尺寸标签、
  无发音按钮、不支持拖拽改变窗口大小；
- **文本**：无打字机逐字特效与逐字出现动画，整段内容立即排版上屏；
  普通翻译固定显示第一行（start 锚点），实时语音识别保留 tail 锚点
  （已确认决议：实时识别体验与 style=1 一致）；
- **交互**：全部继承 style=1（拖拽移动/双击锁定解锁/单击翻译/滚轮回看/
  锁定态拖拽滚动/右键快捷菜单/标题窗入口条图标），仅重写视觉与上屏方式；
- **配置复用**：窗口尺寸/字号/行距/标题字号/间距沿用 ``stream_window_*``
  六键，不新增重复语义配置项。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter

from ..bound_window import BOUND_GAP
from .scroll_logic import body_scroll_target
from .windows import BodyWindow, TitleWindow

#: 整窗半透明黑色背景（参考项目 QScrollArea 的 rgba(0,0,0,160)）
STYLE2_BG_COLOR = "#000000"
STYLE2_BG_ALPHA = 160
#: 整窗背景圆角半径（逻辑像素）
STYLE2_RADIUS = 8.0
#: 标题与正文统一的白色文字（参考项目 QLabel 白字，替代 style=1 淡蓝）
STYLE2_TEXT_COLOR = "#FFFFFF"


def _draw_style2_backdrop(painter: QPainter, widget) -> None:
    """绘制 style=2 的整窗半透明黑圆角背景（标题窗与正文窗共用）。"""
    bg = QColor(STYLE2_BG_COLOR)
    bg.setAlpha(STYLE2_BG_ALPHA)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(bg)
    painter.drawRoundedRect(QRectF(widget.rect()), STYLE2_RADIUS, STYLE2_RADIUS)
    painter.setBrush(Qt.BrushStyle.NoBrush)


class Style2TitleWindow(TitleWindow):
    """标题窗 style=2 变体：整窗半透明黑圆角 + 白色标题（仅重写绘制）。

    排版（set_text）、入口条注入与命中分发、单双击/右键事件转发、
    ``fm``/``hwnd``/``dpr`` 全部继承 style=1；空标题时也绘制背景，
    保证标题条常驻可见（区别于 style=1 的空标题全透明）。
    """

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _draw_style2_backdrop(painter, self)
        if not self._layout:
            return
        fm = self.fm
        box_h = fm.height()
        center_y = self.height() / 2.0  # 单行垂直居中
        baseline_y = (self.height() - box_h) / 2.0 + fm.ascent()
        # 白色直绘（无蒙版条带：整窗背景已提供对比度）
        painter.setFont(self._font)
        painter.setPen(QColor(STYLE2_TEXT_COLOR))
        for ch, x in self._layout:
            painter.drawText(QPointF(x, baseline_y), ch)
        # 入口条：紧跟标题文本右侧（同一渲染帧，图标实时随文本长度移动）
        if not self._entry_strip.is_empty():
            last_ch, last_x = self._layout[-1]
            text_right = last_x + fm.horizontalAdvance(last_ch)
            hot = self._entry_strip.hotzone()
            entry_x = int(text_right + BOUND_GAP)
            entry_y = int(center_y - hot / 2.0)
            self._entry_rect = QRect(entry_x, entry_y, self._entry_strip.width(), hot)
            self._entry_strip.paint(painter, entry_x, entry_y)


class Style2BodyWindow(BodyWindow):
    """正文窗 style=2 变体：整段立即排版上屏（无打字机节拍与出现动画）。

    滚轮回看、锁定态拖拽滚动、锚点切换、清屏/流全文、鼠标事件转发全部
    继承 style=1；打字机定时器继承保留但恒空转（``_pending`` 恒空）。
    """

    # ---- 对外接口（上屏方式与 style=1 的差异点） ---------------------------

    def feed_text(self, text: str) -> None:
        """整段追加并立即排版上屏（无逐字节拍、无出现动画）。

        ``_stream_text`` 语义与 style=1 一致（对话窗「捕获」按钮的内容
        来源）；翻译增量在 style=2 下不经过本方法（整段汇总上屏），
        实际调用方为整段替换与单行追加两类。
        """
        if not text:
            return
        self._stream_text += text
        for ch in text:
            self._append_char(ch)
        self.update()

    def replace_stream(self, text: str) -> None:
        """整段替换（实时语音识别专用）：差分保留公共前缀后立即重排上屏。

        继承 :func:`plan_stream_replace` 差分逻辑；style=2 无打字机队列，
        差分产出的待显示尾部直接冲刷排版，不重播出现动画。
        """
        super().replace_stream(text)
        while self._pending:
            self._append_char(self._pending.pop(0))
        self.update()

    # ---- 渲染 -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _draw_style2_backdrop(painter, self)
        # 滚动：沿用 body_scroll_target 纯函数收敛目标；style=2 无平滑
        # 插值，直接落位（与参考项目 QScrollArea 的即时滚动观感一致）。
        # start 锚点固定显示第一行（普通翻译），tail 锚点跟随末尾（实时
        # 语音识别，已确认决议保留）。
        self._scroll_target = body_scroll_target(
            self._follow, self._scroll_target, self._max_scroll(), self._anchor
        )
        self._scroll = self._scroll_target
        painter.translate(0.0, -self._scroll)
        # 白色直绘已排版字形（无蒙版条带、无出现动画：字形数据仅作排版结果）
        painter.setFont(self._font)
        painter.setPen(QColor(STYLE2_TEXT_COLOR))
        for glyph in self._glyphs:
            painter.drawText(QPointF(glyph.x, glyph.baseline), glyph.ch)


def resolve_window_classes(style: int) -> tuple[type, type]:
    """配置样式 → ``(标题窗类, 正文窗类)``；非法值防御性回退 style=1。

    配置层已保证取值仅为 1/2，此处为纵深防御：仅 ``style == 2`` 启用
    新样式，其余（含 1 与任何意外值）一律走既有实现。
    """
    if style == 2:
        return Style2TitleWindow, Style2BodyWindow
    return TitleWindow, BodyWindow
