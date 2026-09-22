"""预构建翻译缓存的选择与进度弹窗：文件勾选 → 解析 → 后台翻译进度。

交互流（设计文档第六节）：点击绑定窗口入口 → 宿主扫描得到相对路径列表 →
本弹窗列出条目（复选框**默认不勾选**，支持分批构建）→ 用户「解析」进入
运行态（列表禁用、进度实时：先"统计中…已发现 N 条"，再 ``0/total``）→
「隐藏到后台」只藏窗口任务继续（入口按钮唤回）、「取消」终止本轮并销毁
弹窗（下次入口重新扫描）。

线程约定：弹窗只在 Qt 主线程创建与更新，状态由宿主经 ``set_*`` 方法在
主线程回调（进度事件从后台线程经宿主队列回主线程后到达）。控件行为本身
不做业务逻辑 —— 勾选收集、文案格式化与终态文案抽为模块级纯函数，离线
单测（见 ``tests/test_pretranslate_dialog.py``）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("renpy_overlay.pretranslate_dialog")

#: 弹窗初始尺寸（逻辑像素；文件多时列表内部滚动）
DIALOG_SIZE = (420, 560)

STATE_SELECT = "select"  # 勾选文件，解析/取消可用
STATE_RUNNING = "running"  # 任务在途：列表与解析禁用，取消/隐藏可用
STATE_DONE = "done"  # 终态（completed / cancelled / circuit_break / error）

#: 终态 → 进度区展示文案
_FINISHED_TEXT = {
    "completed": "本轮预构建完成。",
    "cancelled": "已取消（已入库条目保留，可重新进入继续）。",
    "circuit_break": "服务不可达已熔断，请检查 config.json 后重试。",
    "error": "预构建异常终止，详见日志。",
}


def collect_checked(items: list[tuple[str, bool]]) -> list[str]:
    """从 ``(路径, 勾选)`` 列表收集勾选路径，保持展示顺序（纯函数，测试用）。"""
    return [path for path, checked in items if checked]


def toggle_item_check(item: QListWidgetItem) -> None:
    """整行点击的复选框状态翻转（勾选 ↔ 取消勾选；纯状态切换，无副作用）。"""
    item.setCheckState(
        Qt.CheckState.Unchecked
        if item.checkState() == Qt.CheckState.Checked
        else Qt.CheckState.Checked
    )


def format_counting(discovered: int) -> str:
    """统计阶段进度文案（纯函数，测试用）。"""
    return f"统计中…已发现 {discovered} 条原文"


def format_progress(done: int, total: int, failed: int, skipped: int) -> str:
    """翻译阶段进度文案：``done/total`` 为主，失败/跳过非零时附注（纯函数）。"""
    text = f"{done}/{total}"
    extra = []
    if failed:
        extra.append(f"失败 {failed}")
    if skipped:
        extra.append(f"跳过 {skipped}")
    if extra:
        text += "（" + "，".join(extra) + "）"
    return text


def finished_text(status: str) -> str:
    """终态文案（未知状态回退 error 文案；纯函数，测试用）。"""
    return _FINISHED_TEXT.get(status, _FINISHED_TEXT["error"])


class PretranslateDialog(QDialog):
    """文件选择 + 进度显示弹窗（业务分发经构造回调注入，弹窗不含业务逻辑）。"""

    def __init__(
        self,
        files: list[str],
        on_parse,  # Callable[[list[str]], None]  勾选文件 → 宿主启动任务
        on_cancel,  # Callable[[], None]           → 宿主中止任务并销毁弹窗
        on_hide,  # Callable[[], None]             → 宿主隐藏弹窗（任务继续）
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("预构建翻译缓存")
        self.resize(*DIALOG_SIZE)
        self._on_parse = on_parse
        self._on_cancel = on_cancel
        self._on_hide = on_hide
        self._state = STATE_SELECT
        self._total = 0  # 阶段 2 就绪后的进度分母（本轮待翻译条数）

        self._status_label = QLabel("勾选本轮需要构建的脚本文件（默认不勾选）：", self)
        self._progress_label = QLabel("", self)
        self._file_list = QListWidget(self)
        self._file_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for path in files:
            item = QListWidgetItem(path, self._file_list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)  # 需求：默认不勾选
        self._parse_button = QPushButton("解析", self)
        self._cancel_button = QPushButton("取消", self)
        self._hide_button = QPushButton("隐藏到后台", self)
        self._parse_button.clicked.connect(self._emit_parse)
        self._cancel_button.clicked.connect(self._emit_cancel)
        self._hide_button.clicked.connect(self._emit_hide)

        buttons = QHBoxLayout()
        buttons.addWidget(self._cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self._hide_button)
        buttons.addWidget(self._parse_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._status_label)
        layout.addWidget(self._file_list, 1)
        layout.addWidget(self._progress_label)
        layout.addLayout(buttons)
        # 整行点击切换勾选：接管视口释放事件（拦截原生指示器切换，避免双重翻转）
        self._file_list.viewport().installEventFilter(self)
        self._apply_state()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """右上角关闭（X）与「取消」按钮完全等价：走同一 on_cancel 回调。

        接管默认关闭流程（ignore 后由宿主 deleteLater 负责销毁）：
        - 选择态：退出解析流程并销毁弹窗；
        - 运行态：中止本轮后台任务（已入库条目保留）并销毁弹窗；
        - 终态：仅销毁弹窗。
        两种入口均不得触发程序退出（无父 QDialog 是唯一计入
        lastWindowClosed 的常规窗口，默认关闭流程曾连带退出整个工具）。
        """
        event.ignore()  # 销毁交由宿主 _cancel_prebuild 的 deleteLater
        self._emit_cancel()

    # ---- 整行点击切换勾选 ---------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """列表视口的释放事件：选择态下条目行内任意位置翻转该行复选框。

        拦截（返回 True）同时吃掉原生指示器的切换，避免双重翻转；点击落在
        条目行之外（列表空白区）或非选择态（运行/终态整表禁用）时不处理，
        交回默认行为。不产生解析/取消/隐藏等任何其它副作用。
        """
        if (
            obj is self._file_list.viewport()
            and self._state == STATE_SELECT
            and event.type() == QEvent.Type.MouseButtonRelease
        ):
            item = self._file_list.itemAt(event.position().toPoint())
            if item is not None:
                toggle_item_check(item)
                return True
        return False

    # ---- 状态 ------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def checked_files(self) -> list[str]:
        """当前勾选的相对路径（展示顺序）。"""
        items = []
        for row in range(self._file_list.count()):
            item = self._file_list.item(row)
            items.append((item.text(), item.checkState() == Qt.CheckState.Checked))
        return collect_checked(items)

    # ---- 宿主主线程回调：阶段 / 进度 / 终态 -------------------------------

    def set_counting(self, discovered: int) -> None:
        """阶段 1（统计）：随解析进度显示已发现条数。"""
        self._progress_label.setText(format_counting(discovered))

    def set_ready(self, total: int) -> None:
        """阶段 2 就绪：进度分母确定，从 0/total 起。"""
        self._total = int(total)
        self._progress_label.setText(format_progress(0, self._total, 0, 0))

    def set_progress(self, done: int, failed: int, skipped: int) -> None:
        """翻译进度实时更新（done 含成功，failed/skipped 单列）。"""
        self._progress_label.setText(format_progress(done, self._total, failed, skipped))

    def set_finished(self, status: str) -> None:
        """终态：进度区给结论，解析保持禁用，取消变为可用的收束按钮。"""
        self._state = STATE_DONE
        prefix = self._progress_label.text()
        self._progress_label.setText(f"{prefix} {finished_text(status)}".strip())
        self._parse_button.setEnabled(False)
        self._hide_button.setEnabled(False)
        self._cancel_button.setText("关闭")
        self._apply_state_list()

    # ---- 按钮分发 ---------------------------------------------------------

    def _emit_parse(self) -> None:
        if self._state != STATE_SELECT:
            return
        files = self.checked_files()
        if not files:
            self._status_label.setText("尚未勾选任何文件，请先勾选后解析。")
            return
        self._state = STATE_RUNNING
        self._apply_state()
        self._on_parse(files)

    def _emit_cancel(self) -> None:
        """取消/关闭统一出口：选择态退出解析流程，运行态中止任务（宿主销毁弹窗）。"""
        self._on_cancel()

    def _emit_hide(self) -> None:
        if self._state == STATE_RUNNING:
            self._on_hide()

    # ---- 状态 → 控件可用性 -------------------------------------------------

    def _apply_state(self) -> None:
        running = self._state == STATE_RUNNING
        self._parse_button.setEnabled(self._state == STATE_SELECT)
        self._hide_button.setEnabled(running)
        self._cancel_button.setEnabled(True)
        self._apply_state_list()

    def _apply_state_list(self) -> None:
        """仅选择态允许勾选（运行/终态整表禁用，防中途改勾选造成歧义）。"""
        enabled = self._state == STATE_SELECT
        for row in range(self._file_list.count()):
            item = self._file_list.item(row)
            if enabled:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
