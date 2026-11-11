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

未锁定态另支持**八向拉伸调整窗口大小**（四边 + 四角，与截图窗口同口
径：角优先、对边/对角固定、最小 100×150；滚动条区域保持原生事件，把
手内最靠上的窄带优先作北向拉伸，其余把手区域仍为移动窗口）。拉伸仅在
未锁定时可用，锁定态不响应尺寸调整；尺寸不持久化，关闭再打开仍按配
置默认尺寸（150×300）打开。
"""

from __future__ import annotations

import html
import logging
import time

from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QFrame, QTextBrowser, QVBoxLayout, QWidget

from ..bound_window import ENTRY_BACKDROP_COLOR, ENTRY_BACKDROP_RADIUS
from ..screenshot.window_visual import draw_edge_highlight, draw_grip_handles
from .choice_log import ANCHOR_GAP, anchored_position, is_effect_parsed
from .scroll_logic import DRAG_SCROLL_INTERVAL_MS, body_drag_scroll_rate

logger = logging.getLogger("renpy_overlay.stream_window")

#: 顶部拖动把手高度（逻辑像素）：内容区（除滚动条）全域亦可拖动，把手仅作辅助热区
HANDLE_HEIGHT = 14

#: 拖动启动阈值（逻辑像素）：按下后位移超过该值才进入正式拖动（移动窗口
#: 或滚动内容）；双击的两次按击位移低于该值，与拖动互不误判
DRAG_START_THRESHOLD_PX = 12

#: 八向拉伸热区宽度（逻辑像素）：与拖动启动阈值同量级（参考实现为动态
#: min(14, 短边×15%)；过窄会导致按压几乎必然落空热区——实测教训）
RESIZE_EDGE_PX = 12
#: 八向拉伸的最小尺寸下限（与 config 校验下限同口径，保证滚动条与内容可用）
MIN_WINDOW_WIDTH = 100
MIN_WINDOW_HEIGHT = 150

#: 边缘/角 → 光标形态（与截图窗口同口径：nw/se → SizeFDiag，ne/sw →
#: SizeBDiag，n/s → SizeVer，e/w → SizeHor）
CURSOR_BY_EDGE = {
    "nw": Qt.CursorShape.SizeFDiagCursor,
    "ne": Qt.CursorShape.SizeBDiagCursor,
    "sw": Qt.CursorShape.SizeBDiagCursor,
    "se": Qt.CursorShape.SizeFDiagCursor,
    "n": Qt.CursorShape.SizeVerCursor,
    "s": Qt.CursorShape.SizeVerCursor,
    "w": Qt.CursorShape.SizeHorCursor,
    "e": Qt.CursorShape.SizeHorCursor,
}


def cursor_for_edge(edge: str | None) -> Qt.CursorShape:
    """边缘/角 → 对应拉伸光标（None → 箭头；纯函数，离线测试用）。"""
    return CURSOR_BY_EDGE.get(edge, Qt.CursorShape.ArrowCursor)


def should_begin_resize(locked: bool, edge: str | None) -> bool:
    """按下分流判定：仅未锁定且命中边缘/角热区才启动拉伸（纯函数）。"""
    return not locked and edge is not None


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


def edge_at(
    x: int,
    y: int,
    width: int,
    height: int,
    handle_h: int = HANDLE_HEIGHT,
    edge_px: int = RESIZE_EDGE_PX,
    right_start: int | None = None,
    right_end: int | None = None,
) -> str | None:
    """窗口本地坐标 → 八向拉伸边缘/角（None = 非热区；纯函数，离线测试用）。

    - 角优先于边：两方向热区叠加判角（nw/ne/sw/se），否则退回单边
      （n/s/w/e）；
    - 北向热区（y < edge_px）位于把手区内最靠上的若干像素，优先于「把手
      移动窗口」——其余把手区域（y ∈ [edge_px, handle_h)）返回 None，
      按下后仍为移动窗口语义；
    - 东向热区带由 ``right_start``（左界）/``right_end``（右开界）给出：
      滚动条可见时调用方传「宽度 − 滚动条宽 − edge_px」与「宽度 − 滚动
      条宽」，滚动条区域不参与拉伸（保持原生事件），滚动条隐藏时右带为
      最右 ``edge_px`` 窄带；
    - 锁定与否由调用方分流，本函数不做锁定判定。
    """
    rs = width - edge_px if right_start is None else right_start
    re = width if right_end is None else right_end
    near_n = y < edge_px
    near_s = y >= height - edge_px
    near_w = x < edge_px
    near_e = rs <= x < re
    if near_n and near_w:
        return "nw"
    if near_n and near_e:
        return "ne"
    if near_s and near_w:
        return "sw"
    if near_s and near_e:
        return "se"
    if near_n:
        return "n"
    if near_s:
        return "s"
    if near_w:
        return "w"
    if near_e:
        return "e"
    return None


def apply_resize_delta(
    x: int,
    y: int,
    w: int,
    h: int,
    edge: str,
    dx: int,
    dy: int,
    min_w: int = MIN_WINDOW_WIDTH,
    min_h: int = MIN_WINDOW_HEIGHT,
) -> tuple[int, int, int, int]:
    """按边/角与拖动增量计算新 ``(x, y, w, h)``（纯函数，离线测试用）。

    与截图窗口 ``_do_resize`` 同逻辑：拖动某条边只改变该方向尺寸、对边
    固定；拖动角同时改两个方向、对角固定；涉及左/上方向时原点同步平移
    （被拖边/角始终跟随鼠标）；新尺寸经 ``max(min, ...)`` 钳制，钳制时
    原点补偿保证固定侧不动。
    """
    new_x, new_y, new_w, new_h = x, y, w, h
    if "e" in edge:
        new_w = max(min_w, w + dx)
    if "s" in edge:
        new_h = max(min_h, h + dy)
    if "w" in edge:
        new_w = max(min_w, w - dx)
        new_x = x + w - new_w
    if "n" in edge:
        new_h = max(min_h, h - dy)
        new_y = y + h - new_h
    return new_x, new_y, new_w, new_h


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
    """选项副作用日志弹窗：初始尺寸取配置值、半透明黑、置顶、不抢焦点。

    生命周期由宿主（InjectionFlowMixin._toggle_choice_log）驱动：打开时
    创建并锚定，关闭时销毁并清空日志——本窗口自身无「最小化/关闭」语义，
    托盘隐藏/恢复（hide/show）不清空累积。

    交互（与流式正文窗同构）：双击（把手或内容区，滚动条除外）切换锁定
    ——未锁定按住左键拖动移动整窗、边缘/角热区八向拉伸调整尺寸；锁定后
    按住左键上下拖动改为滚动日志（窗口不动），再次双击解锁恢复。锁定状
    态存于实例，关闭销毁即消亡，重新打开回到未锁定。
    """

    def __init__(self, log, width: int, height: int) -> None:
        super().__init__()
        self.setWindowTitle("renpy-overlay · choice log")
        self._log = log
        self._locked = False
        self._pending_press: QPoint | None = None  # 按下点（未超启动阈值）
        self._gesture: str | None = None  # None | "move" | "scroll" | "resize"
        self._resize_edge: str | None = None  # 拉伸中的边/角（gesture == "resize"）
        self._drag_offset: QPoint | None = None
        self._drag_last_global: QPoint | None = None  # resize 增量基准（上一帧全局点）
        self._hover_cursor: Qt.CursorShape | None = None  # 悬停光标（变化才设置）
        self._hover_edge: str | None = None  # 悬停命中的拉伸边/角（变化才重绘高亮）
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
        # 初始尺寸 = 配置值（默认 150×300）；不再固定尺寸——未锁定态支持
        # 八向拉伸调整（最小 100×150，钳制在拉伸纯函数内），尺寸不持久化：
        # 关闭再打开仍按配置默认尺寸打开
        self.resize(width, height)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
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
        # 鼠标追踪：未按下（悬停）也接收 move，做边缘检测与光标切换
        # （QAbstractScrollArea.setMouseTracking 会透传至 viewport）
        self.setMouseTracking(True)
        self._handle.setMouseTracking(True)
        self._browser.setMouseTracking(True)
        # 滚动条区域保持原生事件与箭头光标（东向拉伸热区已避开该区域）
        self._browser.verticalScrollBar().setCursor(Qt.CursorShape.ArrowCursor)
        # 锁定态拖拽滚动的步进定时器（与流式正文窗同节拍）；parent=self，
        # 窗口销毁随级联销毁，hideEvent 再兜底停止，无定时器/惯性残留
        self._scroll_drag_timer = QTimer(self)
        self._scroll_drag_timer.setInterval(DRAG_SCROLL_INTERVAL_MS)
        self._scroll_drag_timer.timeout.connect(self._tick_scroll_drag)
        self.refresh()

    # ---- 鼠标状态机（把手与内容区共用；locked 决定拖动/拉伸语义） ---------

    def on_press(self, global_pos: QPoint) -> None:
        """左键按下：未锁定先判边缘/角热区（命中 → 拉伸）；否则记录按下点，
        位移超过启动阈值才进入移动/滚动——双击按击位移低，互不误判。"""
        local = self.mapFromGlobal(global_pos)
        rs, re = self._resize_right_band()
        edge = edge_at(
            local.x(),
            local.y(),
            self.width(),
            self.height(),
            HANDLE_HEIGHT,
            RESIZE_EDGE_PX,
            rs,
            re,
        )
        if should_begin_resize(self._locked, edge):
            self._begin_resize(edge, global_pos)
            return
        self._pending_press = QPoint(global_pos)

    def on_move(self, global_pos: QPoint) -> None:
        """移动：手势进行中按类型推进（拉伸/移动/滚动）；空闲时更新悬停光标。"""
        if self._gesture == "resize":
            if self._drag_last_global is not None:
                delta = global_pos - self._drag_last_global
                self._drag_last_global = QPoint(global_pos)
                geo = self.geometry()
                x, y, w, h = apply_resize_delta(
                    geo.x(),
                    geo.y(),
                    geo.width(),
                    geo.height(),
                    self._resize_edge,
                    delta.x(),
                    delta.y(),
                )
                self.setGeometry(x, y, w, h)
            return
        if self._gesture == "move":
            self.update_window_drag(global_pos)
            return
        if self._gesture == "scroll":
            self._update_scroll_drag(global_pos.y())
            return
        # 悬停（或按下未超阈值）：同步边缘检测光标与高亮重绘——必须在启动
        # 判定之前无条件执行，否则悬停（pending 为 None）时提前返回，边缘
        # 光标永不变化（实测回归根因）
        self._sync_hover_cursor(global_pos)
        # 未启动手势：按下点位移超过阈值才进入移动/滚动（双击按击位移低，
        # 不会误启动；真正拖动位移超阈值也不会被判成双击）
        if self._pending_press is None:
            return
        if exceeds_drag_threshold(
            global_pos.x() - self._pending_press.x(),
            global_pos.y() - self._pending_press.y(),
        ):
            if self._locked:
                self._gesture = "scroll"
                self._begin_scroll_drag(self._pending_press.y())
            else:
                self._gesture = "move"
                self.begin_window_drag(self._pending_press)
            return
        self._sync_hover_cursor(global_pos)

    def on_release(self) -> None:
        """释放：结束进行中的手势；未启动的按下（双击的一半）直接清掉。"""
        self._gesture = None
        self._resize_edge = None
        self._drag_offset = None
        self._scroll_drag_timer.stop()
        self._scroll_rate = 0.0
        self._pending_press = None
        self._apply_idle_cursor()

    def on_double_click(self) -> None:
        """双击：切换锁定态；同时取消未生效的按下与进行中的手势（防互判）。"""
        self._gesture = None
        self._resize_edge = None
        self._drag_offset = None
        self._scroll_drag_timer.stop()
        self._scroll_rate = 0.0
        self._pending_press = None
        self._set_locked(not self._locked)

    # ---- 手势实现（移动 / 滚动 / 拉伸） ------------------------------------

    def begin_window_drag(self, global_pos: QPoint) -> None:
        """移动窗口：记录抓取点并切移动光标（抓取点相对窗口位置保持不变）。"""
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self._drag_last_global = QPoint(global_pos)
        self._apply_drag_cursor(Qt.CursorShape.SizeAllCursor)

    def update_window_drag(self, global_pos: QPoint) -> None:
        """按当前全局坐标移动窗口（拖动中由 move 事件驱动）。"""
        if self._drag_offset is not None:
            self.move(global_pos - self._drag_offset)

    def _begin_resize(self, edge: str, global_pos: QPoint) -> None:
        """八向拉伸：记边/角与增量基准并切对应拉伸光标（窗口原点随边/角平移）。"""
        self._gesture = "resize"
        self._resize_edge = edge
        self._drag_last_global = QPoint(global_pos)
        self._apply_drag_cursor(cursor_for_edge(edge))

    def _resize_right_band(self) -> tuple[int, int]:
        """东向拉伸热区的 [左界, 右开界)：滚动条可见时避开滚动条区域。"""
        bar = self._browser.verticalScrollBar()
        sbw = bar.width() if bar.isVisibleTo(self) else 0
        right_end = max(0, self.width() - sbw)
        return max(0, right_end - RESIZE_EDGE_PX), right_end

    def _sync_hover_cursor(self, global_pos: QPoint) -> None:
        """空闲悬停：未锁定按边缘/角显示对应拉伸光标，把手非边缘区为移动
        样式、内部为箭头；锁定态全窗手形。光标与高亮变化才设置/重绘
        （幂等省开销），任何状态都不出现 I 形文本光标。"""
        local = self.mapFromGlobal(global_pos)
        rs, re = self._resize_right_band()
        edge = edge_at(
            local.x(),
            local.y(),
            self.width(),
            self.height(),
            HANDLE_HEIGHT,
            RESIZE_EDGE_PX,
            rs,
            re,
        )
        if self._locked:
            edge = None  # 锁定态不可拉伸：不显示热区光标与高亮
            cursor = Qt.CursorShape.OpenHandCursor
        elif edge is not None:
            cursor = cursor_for_edge(edge)
        elif local.y() < HANDLE_HEIGHT:
            cursor = Qt.CursorShape.SizeAllCursor  # 把手非边缘区：可移动窗口
        else:
            cursor = Qt.CursorShape.ArrowCursor
        if edge != self._hover_edge:
            self._hover_edge = edge
            self.update()  # 触发 paintEvent 重绘边缘高亮
        if cursor != self._hover_cursor:
            self._hover_cursor = cursor
            # 把手光标由 apply_lock/构造期管理（SizeAll/OpenHand），此处只
            # 同步窗口与内容区 viewport，避免覆盖把手的移动/滚动提示
            self.setCursor(cursor)
            self._browser.viewport().setCursor(cursor)

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
        # 可拉伸视觉提示（移植自 screenshot.window_visual）：未锁定时四角
        # 常驻半透明白短线 + 悬停边缘亮白反馈线；锁定态不可拉伸，不绘制
        if not self._locked:
            draw_grip_handles(painter, self.rect())
            if self._hover_edge is not None and self._gesture != "resize":
                draw_edge_highlight(painter, self.rect(), self._hover_edge)

    def hideEvent(self, event) -> None:  # noqa: N802
        """隐藏（含托盘隐藏与销毁路径）：停止滚动拖动，不残留定时器、
        手势标记与悬停高亮状态。"""
        self._scroll_drag_timer.stop()
        self._gesture = None
        self._resize_edge = None
        self._drag_offset = None
        self._pending_press = None
        self._hover_edge = None
        super().hideEvent(event)
