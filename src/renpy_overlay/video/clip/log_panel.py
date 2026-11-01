"""日志区域：一切日志同步输出在窗口的日志区域（future_*.txt 提示章节）。

logging.Handler 桥接到「顶部工具条 + 只读文本区」：worker 线程的日志
经 Qt 信号（自动队列化）投递主线程追加，主线程冻结判别与
logs/videocut.log 文件日志并存（app.setup_logging）。

等级切换（仅界面，不落盘）：顶部工具条「调试」按钮（独占一行，不与
日志文字重叠）切换简洁模式（INFO+，默认）与完整调试模式（DEBUG+）。
handler 恒收全部记录 → 主线程环形缓存 → 统一谓词过滤显示（刷新与
追加共用同一谓词，无绕过路径），切换即时全量刷新（简洁期收到的
DEBUG 切调试后仍可追溯）；重启恢复默认简洁模式。文件/控制台两路的
级别由 app.setup_logging 在各自 handler 上锁定，不受本切换影响。

心跳可见性：带 ``heartbeat`` 标记（logging extra，不进格式化文本）
的高频周期日志（引擎指标/渲染循环心跳等）仅完整调试模式可见——简洁
模式完全隐藏（含历史缓存行），切回即时重新隐藏；日志记录本身（级别/
文件内容）不变。
"""

from __future__ import annotations

import logging
from collections import deque

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

#: 简洁模式显示阈值（默认）：任务进度/状态提示/警告/错误。
COMPACT_LEVEL = logging.INFO
#: 完整调试模式显示阈值：全部日志（含 DEBUG 细节）。
DEBUG_LEVEL = logging.DEBUG

#: 历史/面板环形上限（一致：刷新重建不超过面板块上限）。
PANEL_MAX_ENTRIES = 2000


class _PanelHandler(logging.Handler):
    """logging → Qt 信号桥（线程安全：emit 只发信号，追加在主线程）。

    级别恒为 DEBUG：全部记录先投递面板缓存，显示过滤由主线程侧统一
    谓词完成——切换即时生效且双向可回溯（不丢已产生记录）；心跳标记
    （logging extra ``heartbeat``）随记录一并透传。
    """

    def __init__(self, panel: LogPanel) -> None:
        super().__init__(level=DEBUG_LEVEL)
        self._panel = panel
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # pragma: no cover - 格式化异常兜底
            return
        self._panel.record_received.emit(
            record.levelno, bool(getattr(record, "heartbeat", False)), message
        )


class LogPanel(QWidget):
    """日志面板：顶部工具条（等级切换）+ 只读文本区，挂载即接管 root logger。

    「调试」按钮切换显示阈值：状态仅存内存（不写 config.json 或任何
    持久化配置），重启恢复默认简洁模式；不影响 root 级别与文件/控制台
    日志。心跳标记日志仅完整调试模式显示。
    """

    record_received = pyqtSignal(int, bool, str)  # (levelno, 心跳标记, 格式化文本)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: deque[tuple[int, bool, str]] = deque(maxlen=PANEL_MAX_ENTRIES)
        self._threshold = COMPACT_LEVEL
        self._handler = _PanelHandler(self)

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

        self.record_received.connect(self._on_record)
        self._toggle.toggled.connect(self.set_debug_visible)
        self._update_toggle_tip()

    # ---- root logger 挂载（不动 root 级别与文件/控制台两路） ----------

    def attach_logger(self) -> None:
        """挂载到 root logger（幂等；重复调用不叠加 handler）。"""
        root = logging.getLogger()
        if self._handler not in root.handlers:
            root.addHandler(self._handler)

    def detach_logger(self) -> None:
        logging.getLogger().removeHandler(self._handler)

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

        心跳标记日志仅完整调试模式可见（简洁模式阈值之下再拦一层）；
        其余按阈值放行。
        """
        if heartbeat:
            return self.is_debug_visible()
        return levelno >= self._threshold

    def _refresh(self) -> None:
        """按当前谓词用环形历史全量重建面板（心跳/DEBUG 行随之隐藏/复现）。"""
        self._view.clear()
        for levelno, heartbeat, message in self._history:
            if self._is_visible(levelno, heartbeat):
                self._view.appendPlainText(message)

    def _update_toggle_tip(self) -> None:
        if self.is_debug_visible():
            self._toggle.setToolTip(
                "当前：完整调试模式（DEBUG 及以上，含心跳日志）\n"
                "点击切回简洁模式（INFO 及以上）"
            )
        else:
            self._toggle.setToolTip(
                "当前：简洁模式（INFO 及以上，隐藏心跳日志）\n"
                "点击切换完整调试模式（DEBUG 及以上）"
            )

    # ---- 记录接收 -------------------------------------------------------

    def _on_record(self, levelno: int, heartbeat: bool, message: str) -> None:
        self._history.append((levelno, heartbeat, message))
        if self._is_visible(levelno, heartbeat):
            self._view.appendPlainText(message)
