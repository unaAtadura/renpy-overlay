"""日志区域：剪辑功能内部的独立进度信息流（显式上报，不经 logging 链路）。

数据来源：功能代码（导出/字幕/载入等）在主线程显式调用 :meth:`LogPanel.append`
系列接口把进度与状态消息推送到面板——显示什么只取决于调用点本身，不依赖
root logger 的传播链、handler 挂载时机、logger 级别或心跳标记（旧设计由
logging handler 桥接上屏，实测长耗时任务运行期间面板静止不可靠，已废弃）。

线程约定：append 系列仅限主线程调用；worker 线程一律经 pyqtSignal 投递
（如 ``progress_logged``/``entry_progress``/``message``）由主线程槽转发，
不在子线程直接操作控件。

镜像写 logging：append 在上屏的同时经本模块 logger 以原始文本写入标准
logging 体系（传播到 root 的文件/控制台 handler）——面板来源与日志文件
互不依赖：向 root 写日志不会再进面板，面板消息仍保留文件日志存档。

显示分级（「调试」按钮，仅界面阈值不落盘）：通道消息中 info/success/
warning/error（INFO+）在默认简洁模式恒可见（不因任何标记隐藏或降级）；
append_debug 为独立诊断通道（DEBUG 级）——仅完整调试模式可见，简洁模式
隐藏但入环形缓存，切换即时复现。缓存与刷新共用同一谓词：切换等级不清空、
不丢行，先到的缓存行与后到的新行保持一致。
"""

from __future__ import annotations

import logging
import time
from collections import deque

from PyQt6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

#: 简洁模式显示阈值（默认）：任务进度/状态提示/警告/错误恒可见。
COMPACT_LEVEL = logging.INFO
#: 完整调试模式显示阈值：含 append_debug 诊断通道（DEBUG 级）。
DEBUG_LEVEL = logging.DEBUG

#: 历史/面板环形上限（一致：刷新重建不超过面板块上限）。
PANEL_MAX_ENTRIES = 2000

#: 通道消息级别 → logging 级别（决定谓词放行与镜像 logging 的记录级别）
_LEVELNOS = {
    "info": logging.INFO,
    "success": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "debug": logging.DEBUG,
}
#: 通道消息级别 → 行首显示标签（success 单列，便于一眼区分完成类消息）
_LEVEL_TAGS = {
    "info": "INFO",
    "success": "SUCCESS",
    "warning": "WARNING",
    "error": "ERROR",
    "debug": "DEBUG",
}

logger = logging.getLogger("renpy_overlay.video.clip.log_panel")


class LogPanel(QWidget):
    """日志面板：顶部工具条（等级切换）+ 只读文本区 + 内部进度信息流。

    内容只来自 :meth:`append` 系列显式上报（主线程）；「调试」按钮切换
    显示阈值：状态仅存内存（不写 config.json 或任何持久化配置），重启
    恢复默认简洁模式；不影响 root 级别与文件/控制台日志。append_debug
    诊断通道仅完整调试模式显示。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: deque[tuple[int, bool, str]] = deque(maxlen=PANEL_MAX_ENTRIES)
        self._threshold = COMPACT_LEVEL

        # 顶部工具条（独占一行，按钮右对齐）+ 文本区：按钮不与日志文字重叠
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QHBoxLayout()
        bar.addStretch(1)
        self._toggle = QToolButton(self)
        self._toggle.setText("调试")
        self._toggle.setCheckable(True)
        bar.addWidget(self._toggle)
        layout.addLayout(bar)
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(PANEL_MAX_ENTRIES)  # 环形上限：防长会话内存增长
        layout.addWidget(self._view, stretch=1)

        self._toggle.toggled.connect(self.set_debug_visible)
        self._update_toggle_tip()

    # ---- 进度信息流（显式上报；仅主线程调用） --------------------------

    def append(self, text: str, *, level: str = "info", category: str | None = None) -> None:
        """把一条进度/状态消息推送到日志区（主线程；worker 经信号转发）。

        ``level``：info / success / warning / error / debug（debug 为诊断
        通道，仅完整调试模式可见）；``category``：可选分类（导出/字幕/
        播放/系统），显示为行首 ``[分类]``。消息入环形缓存并按当前阈值
        显示；同时以原始文本镜像写入 logging（文件/控制台保留存档，与
        面板来源互不依赖）。
        """
        levelno = _LEVELNOS.get(level, logging.INFO)
        tag = _LEVEL_TAGS.get(level, "INFO")
        prefix = f"[{category}] " if category else ""
        message = f"{time.strftime('%H:%M:%S')} {tag} {prefix}{text}"
        self._history.append((levelno, False, message))
        if self._is_visible(levelno, False):
            self._view.appendPlainText(message)
        logger.log(levelno, "%s", text)  # 镜像存档：文件/控制台保留进度记录

    def append_progress(self, text: str, *, category: str | None = None) -> None:
        """任务周期进度（百分比/帧数/阶段等；INFO 级恒可见）。"""
        self.append(text, level="info", category=category)

    def append_info(self, text: str, *, category: str | None = None) -> None:
        """一般状态消息（开始/完成/中断等）。"""
        self.append(text, level="info", category=category)

    def append_success(self, text: str, *, category: str | None = None) -> None:
        """成功完成类消息（SUCCESS 标签）。"""
        self.append(text, level="success", category=category)

    def append_warning(self, text: str, *, category: str | None = None) -> None:
        """警告类消息（回退/降级/配置缺失等）。"""
        self.append(text, level="warning", category=category)

    def append_error(self, text: str, *, category: str | None = None) -> None:
        """失败类消息（含原因）。"""
        self.append(text, level="error", category=category)

    def append_debug(self, text: str, *, category: str | None = None) -> None:
        """诊断通道（DEBUG 级）：仅完整调试模式可见，简洁模式隐藏但入缓存。"""
        self.append(text, level="debug", category=category)

    # ---- 门面（文本内容/焦点策略落于文本区） ----------------------------

    def toPlainText(self) -> str:
        """当前显示的日志文本（与 QPlainTextEdit 同名门面）。"""
        return self._view.toPlainText()

    def set_text_focus_policy(self, policy) -> None:
        """焦点策略作用于文本区（窗口路由：点击可 Ctrl+C 复制日志）。"""
        self._view.setFocusPolicy(policy)

    # ---- 等级切换（仅界面阈值） ----------------------------------------

    def is_debug_visible(self) -> bool:
        """当前是否完整调试模式（显示阈值 DEBUG+）。"""
        return self._threshold == DEBUG_LEVEL

    def set_debug_visible(self, visible: bool) -> None:
        """切换显示阈值并即时按新级别全量刷新（幂等；同步按钮选中态）。"""
        if visible == self.is_debug_visible():
            return
        self._threshold = DEBUG_LEVEL if visible else COMPACT_LEVEL
        if self._toggle.isChecked() != visible:
            self._toggle.setChecked(visible)  # toggled → 本方法幂等返回，无回环
        self._update_toggle_tip()
        self._refresh()

    def _is_visible(self, levelno: int, heartbeat: bool) -> bool:
        """统一显示谓词（追加与刷新共用，无绕过路径）。

        通道消息无心跳标记（heartbeat 恒 False）——info+ 简洁模式恒可见；
        debug 级（诊断通道）仅完整调试模式可见。heartbeat 形参保留以兼容
        缓存结构，恒为 False。
        """
        if heartbeat:
            return self.is_debug_visible()
        return levelno >= self._threshold

    def _refresh(self) -> None:
        """按当前谓词用环形历史全量重建面板（DEBUG 诊断行随之隐藏/复现）。"""
        self._view.clear()
        for levelno, heartbeat, message in self._history:
            if self._is_visible(levelno, heartbeat):
                self._view.appendPlainText(message)

    def _update_toggle_tip(self) -> None:
        if self.is_debug_visible():
            self._toggle.setToolTip(
                "当前：完整调试模式（DEBUG 及以上，含诊断通道）\n"
                "点击切回简洁模式（任务进度恒可见）"
            )
        else:
            self._toggle.setToolTip(
                "当前：简洁模式（任务进度恒可见，隐藏诊断通道）\n"
                "点击切换完整调试模式（含 DEBUG 诊断）"
            )
