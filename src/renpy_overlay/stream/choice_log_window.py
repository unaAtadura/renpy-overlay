"""选项副作用日志弹窗：半透明黑固定尺寸覆盖窗（纯视图，无业务逻辑）。

不进流式双窗体系：独立单例，由标题窗「选项副作用日志」入口图标开合；
打开时锚定图标右上角一次，此后不自动跟随标题窗移动，拖动位置保持到
关闭。数据读自宿主 :class:`~.choice_log.ChoiceEffectLog`（唯一累积点），
关闭即由宿主清空——窗口销毁不留历史。

视觉语言与流式双窗一致：全透明窗体 + 半透明黑圆角底板 + 淡蓝主色文字。
「预测」（菜单出现时的事前条目，灰）与「已发生」（玩家选择后翻转的条目，
淡蓝）用前缀徽标 + 文字色双重区分，一眼可辨哪条是事前预测、哪条已发生。

交互与流式正文窗同构：**双击锁定 / 双击解锁**（把手区或内容区，滚动条
除外）——未锁定时按住左键拖动移动整个窗口；锁定后按住左键上下拖动改
为滚动日志内容（窗口不动，档位速率沿用 scroll_logic.body_drag_scroll_rate，
方向一致：下拖内容向上滚、上拖内容向下滚）。锁定状态存于窗口实例，
关闭销毁后重新打开回到未锁定。按下仅记录起点、位移超过启动阈值才进入
正式拖动——双击的两次按击位移极低，与拖动互不误判。任何状态都没有文
本交互（无选中/复制/I 形光标），滚轮回看与底部自动跟随不受影响。
"""

from __future__ import annotations

import html
import logging
import time

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QTextBrowser, QVBoxLayout, QWidget

from ..bound_window import ENTRY_BACKDROP_COLOR, ENTRY_BACKDROP_RADIUS
from .choice_log import ANCHOR_GAP, anchored_position, is_effect_parsed
from .scroll_logic import DRAG_SCROLL_INTERVAL_MS, body_drag_scroll_rate

logger = logging.getLogger("renpy_overlay.stream_window")

#: 顶部拖动把手高度（逻辑像素）：内容区（除滚动条）全域亦可拖动，把手仅作辅助热区
HANDLE_HEIGHT = 14

#: 拖动启动阈值（逻辑像素）：按下后位移超过该值才进入正式拖动（移动窗口
#: 或滚动内容）；双击的两次按击位移低于该值，与拖动互不误判
DRAG_START_THRESHOLD_PX = 12

#: 主色（淡蓝，与标题/正文文字同一视觉语言）与预测态灰色
_COLOR_ACCENT = "#ADD8E6"
_COLOR_PREDICTED = "#9AA0A6"
_COLOR_MUTED = "#7A7F87"

#: 上文预览的最大字符数（窄幅弹窗下防止首行过宽）
_PROMPT_PREVIEW_CHARS = 20


def exceeds_drag_threshold(dx: int, dy: int, threshold: int = DRAG_START_THRESHOLD_PX) -> bool:
    """按下后位移是否超过拖动启动阈值（纯函数，离线测试用）。

    双击的两次按击相对第一击的位置几乎不变（位移低于阈值）→ 不进入拖动；
    真正的拖动位移超过阈值 → 也不会被 Qt 判成双击（双击本身要求位置接近），
    单击拖动与双击锁定由此互不误判。
    """
    return abs(dx) > threshold or abs(dy) > threshold


def scroll_drag_rate(dy: float, line_h: float) -> float:
    """锁定态拖拽滚动的档位速率（逻辑像素/秒，含方向；纯函数，离线测试用）。

    沿用流式正文窗同款换算（scroll_logic.body_drag_scroll_rate）：
    防抖区（|dy| ≤ 12px）为 0；下拖（dy > 0）速率为负 → 内容向上滚回看
    上文；上拖（dy < 0）速率为正 → 内容向下滚看后文——方向与流式窗口
    完全一致。``line_h`` 取内容区字体行高（弹窗无固定行高概念）。
    """
    return body_drag_scroll_rate(dy, line_h)


class _LogBrowser(QTextBrowser):
    """日志内容区：禁用全部文本交互，鼠标事件转发宿主统一分流。

    QTextBrowser 默认 ``TextSelectableByMouse`` 会把左键按下变成文本选择
    （I 形光标）——这里整体接管鼠标事件并转发宿主状态机（on_press/on_move/
    on_release/on_double_click），文本选择无从开始；双击交给锁定切换；
    滚轮不重写（滚动回看保持原生）；滚动条是其兄弟子控件，事件不经过本类，
    拖动滚动条不会误触发窗口拖动或内容滚动。
    """

    def __init__(self, owner: ChoiceLogWindow) -> None:
        super().__init__()
        self._owner = owner
        # 需求：无复制/选中/点链接等任何文本交互
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # 只读 QTextEdit 的 viewport 默认 I 形光标：改回箭头（锁定/拖动态由宿主切换）
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
            self._owner.on_press(event.globalPosition().toPoint())
        event.accept()  # 非左键同样吞掉：无任何文本/菜单语义

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._owner.on_move(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._owner.on_release()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._owner.on_double_click()
        event.accept()  # 双击交给锁定切换（不触发选词）

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        event.accept()  # 无文本交互：吞掉 QTextEdit 标准编辑右键菜单


class _DragHandle(QWidget):
    """顶部拖动把手：与内容区共用宿主状态机，锁定态同为滚动热区。"""

    def __init__(self, owner: ChoiceLogWindow) -> None:
        super().__init__(owner)
        self.setFixedHeight(HANDLE_HEIGHT)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def apply_lock(self, locked: bool) -> None:
        """锁定态光标切换：解锁 = 移动样式（SizeAll），锁定 = 可滚动手形。"""
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if locked else Qt.CursorShape.SizeAllCursor
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().on_press(event.globalPosition().toPoint())
            event.accept()
            return
        event.ignore()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self.window().on_move(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.window().on_release()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.window().on_double_click()
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

    交互（与流式正文窗同构）：双击（把手或内容区，滚动条除外）切换锁定
    ——未锁定按住左键拖动移动整窗；锁定后按住左键上下拖动改为滚动日志
    （窗口不动），再次双击解锁恢复。锁定状态存于实例，关闭销毁即消亡，
    重新打开回到未锁定。
    """

    def __init__(self, log, width: int, height: int) -> None:
        super().__init__()
        self.setWindowTitle("renpy-overlay · choice log")
        self._log = log
        self._locked = False
        self._pending_press: QPoint | None = None  # 按下点（未超启动阈值）
        self._drag_active = False  # 已进入正式拖动（移动窗口或滚动内容）
        self._drag_offset: QPoint | None = None
        self._scroll_origin_y = 0.0  # 滚动拖动的按下基准（防抖/档位判定）
        self._scroll_rate = 0.0  # 档位速率（逻辑像素/秒，update 时换算）
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
        self._handle = _DragHandle(self)
        layout.addWidget(self._handle)
        self._browser = _LogBrowser(self)
        self._browser.setOpenExternalLinks(False)
        self._browser.setFrameShape(QFrame.Shape.NoFrame)
        self._browser.setReadOnly(True)
        self._browser.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        layout.addWidget(self._browser)
        # 锁定态拖拽滚动的步进定时器（与流式正文窗同节拍）；parent=self，
        # 窗口销毁随级联销毁，hideEvent 再兜底停止，无定时器/惯性残留
        self._scroll_drag_timer = QTimer(self)
        self._scroll_drag_timer.setInterval(DRAG_SCROLL_INTERVAL_MS)
        self._scroll_drag_timer.timeout.connect(self._tick_scroll_drag)
        self.refresh()

    # ---- 鼠标状态机（把手与内容区共用；locked 决定拖动语义） ---------------

    def on_press(self, global_pos: QPoint) -> None:
        """左键按下：仅记录按下点，位移超过启动阈值才进入正式拖动。

        双击的两次按击位移低于阈值，不会进入拖动；真正的拖动位移超过
        阈值，也不会被 Qt 判成双击——单击拖动与双击锁定互不误判。
        """
        self._pending_press = QPoint(global_pos)
        self._drag_active = False

    def on_move(self, global_pos: QPoint) -> None:
        """移动：正式拖动中按锁定态分流（滚动/移动）；未激活时超阈值才启动。"""
        if self._drag_active:
            if self._locked:
                self._update_scroll_drag(global_pos.y())
            else:
                self.update_window_drag(global_pos)
            return
        if self._pending_press is None:
            return
        if exceeds_drag_threshold(
            global_pos.x() - self._pending_press.x(),
            global_pos.y() - self._pending_press.y(),
        ):
            if self._locked:
                self._begin_scroll_drag(self._pending_press.y())
            else:
                self.begin_window_drag(self._pending_press)
            self._drag_active = True

    def on_release(self) -> None:
        """释放：结束进行中的拖动/滚动；未启动的按下（双击的一半）直接清掉。"""
        if self._drag_active:
            self._end_drag()
        self._pending_press = None

    def on_double_click(self) -> None:
        """双击：切换锁定态；同时取消未生效的按下与进行中的拖动（防互判）。"""
        if self._drag_active:
            self._end_drag()
        self._pending_press = None
        self._set_locked(not self._locked)

    # ---- 两种拖动的实现 ----------------------------------------------------

    def begin_window_drag(self, global_pos: QPoint) -> None:
        """移动窗口：记录抓取点并切移动光标（抓取点相对窗口位置保持不变）。"""
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self._apply_drag_cursor(Qt.CursorShape.SizeAllCursor)

    def update_window_drag(self, global_pos: QPoint) -> None:
        """按当前全局坐标移动窗口（拖动中由 move 事件驱动）。"""
        if self._drag_offset is not None:
            self.move(global_pos - self._drag_offset)

    def _begin_scroll_drag(self, origin_y: float) -> None:
        """锁定态滚动拖动：记按下基准并启动步进定时器（窗口保持不动）。"""
        self._scroll_origin_y = float(origin_y)
        self._scroll_rate = 0.0
        self._apply_drag_cursor(Qt.CursorShape.ClosedHandCursor)
        if not self._scroll_drag_timer.isActive():
            self._scroll_drag_timer.start()

    def _update_scroll_drag(self, current_y: float) -> None:
        """按当前纵坐标换算档位速率：防抖区停表，离开防抖区启动/提速。"""
        self._scroll_rate = scroll_drag_rate(
            current_y - self._scroll_origin_y, self._line_height()
        )
        if self._scroll_rate == 0.0:
            self._scroll_drag_timer.stop()
            return
        if not self._scroll_drag_timer.isActive():
            self._scroll_drag_timer.start()

    def _tick_scroll_drag(self) -> None:
        """滚动步进：按档位速率增量推进滚动条（钳制由滚动条范围自负）。"""
        if not self._locked or self._scroll_rate == 0.0:
            self._scroll_drag_timer.stop()
            return
        bar = self._browser.verticalScrollBar()
        bar.setValue(bar.value() + int(self._scroll_rate * DRAG_SCROLL_INTERVAL_MS / 1000.0))

    def _end_drag(self) -> None:
        """结束拖动/滚动：停定时器、清状态、按当前锁定态恢复空闲光标。"""
        self._drag_active = False
        self._drag_offset = None
        self._scroll_rate = 0.0
        self._scroll_drag_timer.stop()
        self._apply_idle_cursor()

    def _set_locked(self, locked: bool) -> None:
        """切换锁定态并恢复空闲光标（锁定 = 可滚动手形；解锁 = 移动样式）。"""
        self._locked = locked
        self._apply_idle_cursor()
        logger.info(
            "选项副作用日志%s",
            "已锁定（按住左键拖动滚动日志，窗口不动）" if locked else "已解锁（按住左键拖动移动窗口）",
        )

    def _line_height(self) -> float:
        return max(1.0, float(self._browser.fontMetrics().height()))

    def _apply_drag_cursor(self, cursor: Qt.CursorShape) -> None:
        self.setCursor(cursor)
        self._browser.viewport().setCursor(cursor)
        self._handle.setCursor(cursor)

    def _apply_idle_cursor(self) -> None:
        """空闲光标（按锁定态）：解锁 = 内容区箭头 + 把手移动样式；
        锁定 = 全窗可滚动手形。任何状态都不出现 I 形文本光标。"""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._browser.viewport().setCursor(
            Qt.CursorShape.OpenHandCursor if self._locked else Qt.CursorShape.ArrowCursor
        )
        self._handle.setCursor(
            Qt.CursorShape.OpenHandCursor if self._locked else Qt.CursorShape.SizeAllCursor
        )

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

    # ---- 渲染与事件兜底 ---------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r, g, b, a = ENTRY_BACKDROP_COLOR
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(r, g, b, a))
        painter.drawRoundedRect(
            QRectF(self.rect()), ENTRY_BACKDROP_RADIUS, ENTRY_BACKDROP_RADIUS
        )

    def hideEvent(self, event) -> None:  # noqa: N802
        """隐藏（含托盘隐藏与销毁路径）：停止滚动拖动，不残留定时器。"""
        self._scroll_drag_timer.stop()
        self._drag_active = False
        self._pending_press = None
        super().hideEvent(event)
