"""手动选择目标进程：默认弹出 PyQt6 暗色选择窗，不可用时回退命令行菜单。

选择窗刻意显示"分数 + 判定依据"，而不是只给一个进程名列表 —— 识别策略总会有
误判（例如某些游戏的 exe 叫 python.exe），把依据摊开给用户看，比让用户猜更可靠。

另提供「跳过注入」入口（需求 .raw_plans/future_跳过注入.txt）：向非 Ren'Py
游戏提供部分功能 —— 点击后经 IFileOpenDialog 选择数据目录，选择期间选择窗
保持打开，取消则留在选择窗，选定后以 :class:`SkipSelection` 作为选择结果。

视觉与窗口行为（PyQt6 版）：普通层级窗口（不置顶，可被其他窗口遮挡）；配色
沿用项目统一暗色调色板（与识曲 / 翻译 / 语音识别历史窗口同一色彩方案），字体
Microsoft YaHei UI；交互与旧 Tk 版等价——选中行刷新详情、双击行或回车注入、
Esc 取消、「重新扫描」在无 refresh 回调时保持禁用。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .discovery import Candidate, print_candidates
from .translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.picker")

#: 中文字体（与 choice_log_window 等窗口同一字体约定；9pt 同旧 Tk 版字号）
_CJK_FONT_FAMILY = "Microsoft YaHei UI"
_CJK_FONT_SIZE = 9

#: 表格行高（逻辑像素）：显式放大，保证高 DPI 缩放下 9pt 中文字形不被裁切
_ROW_HEIGHT = 28

#: 详情区最小高度（逻辑像素）：缩到最小窗口时详情仍完整可读
_DETAIL_MIN_HEIGHT = 64

#: 表格列定义（表头, 初始列宽, 是否左对齐）：列与列宽同旧 Tk 版，末列自适应拉伸
_COLUMNS: tuple[tuple[str, int, bool], ...] = (
    ("PID", 80, False),
    ("进程名", 220, True),
    ("架构", 70, False),
    ("分数", 60, False),
    ("窗口标题", 420, True),
)


class SkipSelection:
    """选择窗「跳过注入」的结果：``path`` 为用户选择的数据目录（原始路径）。"""

    def __init__(self, path: str) -> None:
        self.path = path


def resolve_cache_dir(selected: Path) -> Path:
    """跳过注入模式：把用户选择的目录解析为数据缓存目录（纯函数，便于离线单测）。

    需求三规则：
    1. 目录名即为 ``renpy_overlay_cache`` → 使用当前目录；
    2. 目录名不同但其下已包含 ``renpy_overlay_cache`` → 使用该子目录；
    3. 其下不包含 → 使用（随后由存储管线自动创建的）``renpy_overlay_cache``。

    情况 1/2 目录与数据库已存在，各存储以 ``CREATE TABLE IF NOT EXISTS`` 打开，
    不覆盖既有数据库；情况 3 由存储管线自动建目录并新建数据库。既有存储管线
    约定 ``game_dir`` 下固定挂 ``renpy_overlay_cache/``，故调用方以
    ``resolve_cache_dir(selected).parent`` 作为 game_dir 传入即可命中全部三种
    情况的正确目录。
    """
    if selected.name == CACHE_DIR_NAME:
        return selected
    return selected / CACHE_DIR_NAME


def choose_target(
    candidates: list[Candidate],
    refresh: Callable[[], list[Candidate]] | None = None,
    use_gui: bool = True,
) -> Candidate | SkipSelection | None:
    """返回用户选中的候选或「跳过注入」结果，取消或没有候选时返回 None。"""
    if not candidates:
        print("未发现候选进程。请先启动 Ren'Py 游戏，或使用 --all 列出全部进程。")
        return None
    if use_gui:
        try:
            return _choose_gui(candidates, refresh)
        except Exception as exc:  # 无显示环境 / Qt 界面异常 等
            logger.warning("图形选择界面不可用（%s），回退到命令行菜单", exc)
    return _choose_console(candidates, refresh)


# ---------------------------------------------------------------- 图形界面


def _dark_palette() -> QPalette:
    """项目统一暗色调色板（与识曲 / 翻译 / 语音识别历史窗口同一色彩方案）。

    在同一方案上补 Disabled 组文字色：无 refresh 回调时禁用的「重新扫描」
    按钮若用默认 Disabled 色会过暗难辨，补中灰保证禁用态对比度。
    """
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(24, 24, 24))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(200, 200, 200))
    palette.setColor(QPalette.ColorRole.Base, QColor(18, 18, 18))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.Text, QColor(200, 200, 200))
    palette.setColor(QPalette.ColorRole.Button, QColor(40, 40, 40))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(200, 200, 200))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(45, 70, 110))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(230, 230, 230))
    disabled_text = QColor(110, 110, 110)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, disabled_text)
    return palette


class _CandidateTable(QTableWidget):
    """候选进程表：整行选择；表格内回车等同「注入选中项」（与双击行一致）。

    回车不交给 QTableView 默认处理（默认是移动/编辑语义），经信号交宿主
    对话框走注入动作；数字小键盘回车（Key_Enter）同样注入。
    """

    inject_requested = pyqtSignal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.inject_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _ChooseDialog(QDialog):
    """进程选择对话框（纯界面 + 本窗交互；按测试约定不做真实窗口单测）。

    三块区域与旧 Tk 版同布局：提示行 + 表格 + 详情 + 按钮行；表格与详情
    1:1 弹性分配拉伸空间（stretch=1 各占一半），按钮行固定高度贴底。

    结果约定（``choose_target`` 的调用方无感知）：「注入」accept 且
    :meth:`result_candidate` 携带选中候选；「跳过注入」accept 且
    :meth:`result_skip` 携带所选数据目录；取消（Esc / 关闭 / 取消按钮）
    两者皆为 None。
    """

    def __init__(
        self,
        candidates: list[Candidate],
        refresh: Callable[[], list[Candidate]] | None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("renpy-overlay - 选择要注入的 Ren'Py 进程")
        # 初始 1000x480 / 最小 760x380 同旧 Tk 版；不设 WindowStaysOnTopHint，
        # 即普通层级窗口（可被其他窗口遮挡）
        self.resize(1000, 480)
        self.setMinimumSize(760, 380)
        self.setPalette(_dark_palette())
        self.setAutoFillBackground(True)
        self.setFont(QFont(_CJK_FONT_FAMILY, _CJK_FONT_SIZE))

        self._rows: list[Candidate] = list(candidates)
        self._refresh = refresh
        self._candidate: Candidate | None = None
        self._skip: SkipSelection | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(
            QLabel(
                "选择目标进程后点击「注入」。分数越高越可能是 Ren'Py 游戏，双击表格行可快速注入。"
            )
        )

        self._table = _CandidateTable(len(self._rows), len(_COLUMNS))
        self._table.setHorizontalHeaderLabels([head for head, _w, _a in _COLUMNS])
        for column, (_head, width, _left) in enumerate(_COLUMNS[:-1]):
            self._table.setColumnWidth(column, width)
        self._table.horizontalHeader().setSectionResizeMode(
            len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.cellDoubleClicked.connect(lambda _row, _column: self._on_inject())
        self._table.inject_requested.connect(self._on_inject)
        layout.addWidget(self._table, stretch=1)

        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMinimumHeight(_DETAIL_MIN_HEIGHT)
        layout.addWidget(self._detail, stretch=1)

        buttons = QHBoxLayout()
        self._refresh_button = QPushButton("重新扫描")
        self._skip_button = QPushButton("跳过注入")
        self._cancel_button = QPushButton("取消")
        self._inject_button = QPushButton("注入选中项")
        # 注入为主操作（default 按钮）：焦点不在表格时的窗口级回车落在它上，
        # 语义仍等同「注入选中项」
        self._inject_button.setDefault(True)
        self._refresh_button.clicked.connect(self._on_refresh)
        self._skip_button.clicked.connect(self._on_skip)
        self._cancel_button.clicked.connect(self.reject)
        self._inject_button.clicked.connect(self._on_inject)
        if refresh is None:
            self._refresh_button.setEnabled(False)  # 无刷新回调：保持禁用（需求）
        buttons.addWidget(self._refresh_button)
        buttons.addWidget(self._skip_button)
        buttons.addStretch(1)
        buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._inject_button)
        layout.addLayout(buttons)

        self._fill()

    # ---- 表格内容与详情 -----------------------------------------------------

    def _fill(self) -> None:
        """按候选列表重建表格内容；有行时选中首行（选中信号联动详情刷新）。"""
        self._table.setRowCount(len(self._rows))
        for row, item in enumerate(self._rows):
            for column, text in enumerate(item.row()):
                cell = QTableWidgetItem(text)
                _head, _width, left = _COLUMNS[column]
                cell.setTextAlignment(
                    (Qt.AlignmentFlag.AlignLeft if left else Qt.AlignmentFlag.AlignHCenter)
                    | Qt.AlignmentFlag.AlignVCenter
                )
                # 只可选不可编辑（双保险：编辑触发器已在视图层整体关闭）
                cell.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(row, column, cell)
        if self._rows:
            self._table.selectRow(0)

    def _selected(self) -> Candidate | None:
        """当前选中行对应的候选；无选中返回 None。"""
        row = self._table.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _on_selection_changed(self) -> None:
        item = self._selected()
        if item is not None:
            self._detail.setPlainText(item.detail())

    # ---- 按钮动作 -----------------------------------------------------------

    def _on_inject(self) -> None:
        item = self._selected()
        if item is None:
            return
        self._candidate = item
        self.accept()

    def _on_refresh(self) -> None:
        if self._refresh is None:
            return
        try:
            self._rows = list(self._refresh())
        except Exception as exc:  # pragma: no cover - 枚举失败时保持原表
            logger.error("重新扫描失败：%s", exc)
            return
        self._fill()

    def _on_skip(self) -> None:
        """跳过注入：选数据目录后关闭选择窗；取消则留在选择窗（需求）。"""
        from .folder_dialog import pick_folder

        # 对话框以本选择窗为属主（winId 即顶层原生 HWND）：期间本窗保持
        # 打开、不可交互，取消/失败（返回 None）留在本窗
        path = pick_folder(int(self.winId()))
        if not path:
            return
        self._skip = SkipSelection(path)
        self.accept()

    # ---- 结果读取（exec 返回后由 _choose_gui 消费） --------------------------

    def result_candidate(self) -> Candidate | None:
        """「注入」路径的选中候选（未注入时 None）。"""
        return self._candidate

    def result_skip(self) -> SkipSelection | None:
        """「跳过注入」路径的结果（未跳过时 None）。"""
        return self._skip


def _choose_gui(
    candidates: list[Candidate], refresh: Callable[[], list[Candidate]] | None
) -> Candidate | SkipSelection | None:
    """弹出 PyQt6 选择窗并模态执行；异常由 choose_target 捕获后回退命令行。"""
    # 选择窗通常是本工具第一个 Qt 窗口（先于悬浮窗创建）：无实例时兜底自建，
    # 局部引用保活到选择流程结束；QApplication 是进程级单例，悬浮窗创建时
    # 经 instance() 复用（见 stream/host_core 的同一惯例）
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    dialog = _ChooseDialog(candidates, refresh)
    dialog.exec()
    skip = dialog.result_skip()
    if skip is not None:
        return skip
    return dialog.result_candidate()


# ---------------------------------------------------------------- 命令行菜单


def _choose_console(
    candidates: list[Candidate], refresh: Callable[[], list[Candidate]] | None
) -> Candidate | None:
    rows = list(candidates)
    while True:
        print()
        print_candidates(rows)
        prompt = "输入 PID 回车确认（直接回车 = 最高分候选"
        prompt += "，r = 重新扫描" if refresh else ""
        prompt += "，q = 取消）: "
        try:
            answer = input(prompt).strip()
        except EOFError:  # pragma: no cover - 管道输入
            return None
        if not answer:
            return rows[0] if rows else None
        if answer.lower() == "q":
            return None
        if answer.lower() == "r":
            if refresh is not None:
                rows = refresh()
            continue
        if answer.isdigit():
            pid = int(answer)
            for item in rows:
                if item.pid == pid:
                    return item
            print(f"PID {pid} 不在候选列表中。")
            continue
        print("无效输入。")


def confirm(prompt: str) -> bool:
    """简单的 y/N 确认，默认取 No。"""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:  # pragma: no cover
        return False
    return answer in ("y", "yes")


def is_interactive() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:  # pragma: no cover
        return False
