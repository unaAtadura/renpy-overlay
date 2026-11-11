"""选项副作用日志弹窗：半透明黑固定尺寸覆盖窗（纯视图，无业务逻辑）。

不进流式双窗体系：独立单例，由标题窗「选项副作用日志」入口图标开合；
打开时锚定图标右上角一次，此后不自动跟随标题窗移动，拖动位置保持到
关闭。数据读自宿主 :class:`~.choice_log.ChoiceEffectLog`（唯一累积点），
关闭即由宿主清空——窗口销毁不留历史。

视觉语言与流式双窗一致：全透明窗体 + 半透明黑圆角底板 + 淡蓝主色文字。
「预测」（菜单出现时的事前条目，灰）与「已发生」（玩家选择后翻转的条目，
淡蓝）用前缀徽标 + 文字色双重区分，一眼可辨哪条是事前预测、哪条已发生。
"""

from __future__ import annotations

import html
import logging
import time

from PyQt6.QtCore import QPoint, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QTextBrowser, QVBoxLayout, QWidget

from ..bound_window import ENTRY_BACKDROP_COLOR, ENTRY_BACKDROP_RADIUS
from .choice_log import ANCHOR_GAP, anchored_position, is_effect_parsed

logger = logging.getLogger("renpy_overlay.stream_window")

#: 顶部拖动把手高度（逻辑像素）：内容区（除滚动条）全域亦可拖动，把手仅作辅助热区
HANDLE_HEIGHT = 14

#: 主色（淡蓝，与标题/正文文字同一视觉语言）与预测态灰色
_COLOR_ACCENT = "#ADD8E6"
_COLOR_PREDICTED = "#9AA0A6"
_COLOR_MUTED = "#7A7F87"

#: 上文预览的最大字符数（窄幅弹窗下防止首行过宽）
_PROMPT_PREVIEW_CHARS = 20


class _LogBrowser(QTextBrowser):
    """日志内容区：禁用全部文本交互，左键按下即拖动窗口（滚轮保留回看）。

    QTextBrowser 默认 ``TextSelectableByMouse`` 会把左键按下变成文本选择
    （I 形光标），窗口拖动因此失效——这里整体接管鼠标事件：左键按下/
    移动交给宿主窗口的拖动逻辑（不调 super，文本选择无从开始），释放
    结束；双击吞掉防选词；滚轮不重写（滚动回看保持原生）；滚动条是其
    兄弟子控件，事件不经过本类，拖动滚动条不会误触发窗口拖动。
    """

    def __init__(self, owner: ChoiceLogWindow) -> None:
        super().__init__()
        self._owner = owner
        # 需求：无复制/选中/点链接等任何文本交互
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # 只读 QTextEdit 的 viewport 默认 I 形光标：改回箭头，消除「可选中」暗示
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def setReadOnly(self, ro: bool) -> None:  # noqa: N802
        """只读切换后重新压回「无文本交互」。

        ``QTextEdit.setReadOnly`` 内部会按只读状态重设交互标志
        （只读 → ``TextSelectableByMouse``）——宿主构造在 ``__init__``
        之后调用本方法会把构造期设置覆盖回可选中，拖动随之失效（实测
        根因）；这里在每次只读切换后重新压回禁用态。
        """
        super().setReadOnly(ro)
        super().setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner.begin_window_drag(event.globalPosition().toPoint())
        event.accept()  # 非左键同样吞掉：无任何文本/菜单语义

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._owner.dragging:
            self._owner.update_window_drag(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._owner.end_window_drag()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        event.accept()  # 双击吞掉：防触发选词

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        event.accept()  # 无文本交互：吞掉 QTextEdit 标准编辑右键菜单


class _DragHandle(QWidget):
    """顶部拖动把手：与内容区共用宿主窗口的同一套拖动逻辑。"""

    def __init__(self, owner: ChoiceLogWindow) -> None:
        super().__init__(owner)
        self.setFixedHeight(HANDLE_HEIGHT)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().begin_window_drag(event.globalPosition().toPoint())
            event.accept()
            return
        event.ignore()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        window = self.window()
        if window.dragging:
            window.update_window_drag(event.globalPosition().toPoint())
            event.accept()
            return
        event.ignore()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.window().end_window_drag()
        event.accept()


def render_entries_html(entries) -> str:
    """把日志条目渲染为弹窗 HTML（纯函数，离线测试用）。

    每菜单一块：时间 + 上文预览 + 选项行（序号 + 徽标 + caption）+ 语句行。
    语句行按「是否解析成功」逐行二选一（条件单栏，互斥）：命中三类正则的
    行只显示摘要（沿用该选项预测/已发生配色，一眼可辨加减方向与布尔置
    位）；未命中（函数调用/条件表达式/多语句折叠等动态行）只显示完整原文
    （保留 $ 前缀与采集原样，不展示被截断的摘要而丢失语义）——不再出现
    「摘要 ｜ 原文」并列与孤立管道符。空日志给引导文案。
    """
    parts: list[str] = []
    for entry in entries:
        stamp = time.strftime("%H:%M:%S", time.localtime(entry.ts))
        prompt = (entry.what or "").strip()[:_PROMPT_PREVIEW_CHARS]
        head = f"{stamp} ｜ {html.escape(prompt)}" if prompt else stamp
        parts.append(f"<div style='color:{_COLOR_MUTED};margin-top:10px;'>{head}</div>")
        for position, item in enumerate(entry.items, 1):
            happened = item.state == "happened"
            badge = "已发生" if happened else "预测"
            color = _COLOR_ACCENT if happened else _COLOR_PREDICTED
            parts.append(
                f"<div style='color:{color};margin-top:4px;'>"
                f"<b>{position}.</b> [{badge}] {html.escape(item.caption)}</div>"
            )
            # summaries 与 effects 生成时一一对应；strict=False 容忍数据异常不崩渲染
            for summary, raw in zip(item.summaries, item.effects, strict=False):
                # quote=False：语句行是 div 文本内容而非属性，单引号无需转义
                if is_effect_parsed(raw):
                    text = html.escape(summary, quote=False)  # 解析成功：仅摘要
                else:
                    text = html.escape(raw, quote=False)  # 动态/未命中：仅完整原文（不截断）
                parts.append(
                    f"<div style='color:{color};margin-left:14px;'>{text}</div>"
                )
    if not parts:
        parts.append(
            f"<div style='color:{_COLOR_MUTED};margin-top:10px;'>"
            "暂无记录：选项出现时自动累积</div>"
        )
    return (
        "<html><body style='font-family: Microsoft YaHei UI; font-size:11px;'>"
        + "".join(parts)
        + "</body></html>"
    )


class ChoiceLogWindow(QWidget):
    """选项副作用日志弹窗：固定尺寸、半透明黑、置顶、不抢焦点、可拖动。

    生命周期由宿主（InjectionFlowMixin._toggle_choice_log）驱动：打开时
    创建并锚定，关闭时销毁并清空日志——本窗口自身无「最小化/关闭」语义，
    托盘隐藏/恢复（hide/show）不清空累积。
    """

    def __init__(self, log, width: int, height: int) -> None:
        super().__init__()
        self.setWindowTitle("renpy-overlay · choice log")
        self._log = log
        self._drag_offset: QPoint | None = None
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # show() 不抢焦点：弹窗叠加在游戏上，激活会把焦点从游戏夺走
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(width, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_DragHandle(self))
        self._browser = _LogBrowser(self)
        self._browser.setOpenExternalLinks(False)
        self._browser.setFrameShape(QFrame.Shape.NoFrame)
        self._browser.setReadOnly(True)
        self._browser.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        layout.addWidget(self._browser)
        self.refresh()

    # ---- 窗口拖动（把手与内容区共用同一套逻辑） ---------------------------

    @property
    def dragging(self) -> bool:
        """拖动进行中（把手与内容区的 move 事件据此转发而非各自为政）。"""
        return self._drag_offset is not None

    def begin_window_drag(self, global_pos: QPoint) -> None:
        """记录抓取点并进入拖动态：抓取点相对窗口位置保持不变，光标切移动样式。"""
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._browser.viewport().setCursor(Qt.CursorShape.SizeAllCursor)

    def update_window_drag(self, global_pos: QPoint) -> None:
        """按当前全局坐标移动窗口（拖动中由内容区/把手的 move 事件驱动）。"""
        if self._drag_offset is not None:
            self.move(global_pos - self._drag_offset)

    def end_window_drag(self) -> None:
        """结束拖动：清抓取点，光标恢复箭头（拖动期间的位置保持到关闭）。"""
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._browser.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    # ---- 对外接口 ---------------------------------------------------------

    def anchor_to(self, top_right: QPoint | None) -> None:
        """弹窗左下角贴合指定全局锚点（入口图标右上角），钳在所在屏内。"""
        if top_right is None:
            return
        screen = QApplication.screenAt(top_right) or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen is not None else None
        if geo is not None:
            x, y = anchored_position(
                top_right.x(),
                top_right.y(),
                self.width(),
                self.height(),
                geo.x(),
                geo.y(),
                geo.width(),
                geo.height(),
                ANCHOR_GAP,
            )
        else:
            x, y = anchored_position(
                top_right.x(),
                top_right.y(),
                self.width(),
                self.height(),
                0,
                0,
                0,
                0,
                ANCHOR_GAP,
            )
        self.move(x, y)

    def refresh(self) -> None:
        """全量重渲染日志条目（宿主在 choice / choice_pick 到达时调用）。

        贴底时跟随新内容；用户滚离底部回看时暂停跟随（滚回底部恢复）。
        """
        bar = self._browser.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        entries = self._log.entries()
        self._browser.setHtml(render_entries_html(entries))
        logger.debug(
            "副作用日志渲染：%d 条目 / %d 语句行（跟随底部=%s）",
            len(entries),
            sum(len(item.effects) for entry in entries for item in entry.items),
            follow,
        )
        if follow:
            bar.setValue(bar.maximum())

    # ---- 渲染 -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r, g, b, a = ENTRY_BACKDROP_COLOR
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(r, g, b, a))
        painter.drawRoundedRect(
            QRectF(self.rect()), ENTRY_BACKDROP_RADIUS, ENTRY_BACKDROP_RADIUS
        )
