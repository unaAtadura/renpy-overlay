"""预构建翻译缓存主窗口：三页签脚本勾选 → 后台批量处理 → 进度展示。

移植自独立工具 renpy-overlay-batch-translator 的三页签主窗口（无自带设置
按钮：配置统一收归宿主设置窗口）。三页签（各页独立扫描按钮与文件列表；
进度条 / 开始处理 / 停止等公共控件在标签页之外）：

- 页 0「.rpy 翻译缓存」：unrpa 判定——``renpy_overlay_cache/unrpa`` 存在
  且递归含 .rpy → 扫描根为 unrpa，否则 game（与页 1 独立判定）；
  game 根沿用既有扫描（tl 过滤 + .rpa 归档内条目）；开始处理执行完整
  翻译流程（解析 → 清洗去重 → 判存 → 动态并发翻译 → 写入 translations.db，
  写库经进程级写锁与前台翻译链路串行化）；
- 页 1「.rpyc 反编译」：unrpa 判定同形（只看 .rpyc）；逐个调用 unrpyc.exe
  反编译：game 内源 → unrpa 同构相对路径，unrpa 内源 → 同级同名 .rpy
  （目标已存在恒跳过）；
- 页 2「.rpa 解包」：恒扫 game 下 .rpa；逐个调用 rpaextract.exe 解包到
  unrpa 下归档专属目录（输出目录非空整档跳过＝同归档幂等）。

线程模型：批量任务在后台守护线程执行（翻译 ``runner.BatchTask`` / 工具
``tools_runner.ToolFileTask``），进度与阶段回调经 :class:`_TaskSignals`
（Qt Signal，跨线程自动排队）回主线程更新控件；取消经 ``threading.Event``
传向任务，窗口关闭时优雅收束在途请求。

状态机：

- ``select``：可扫描 / 勾选 / 开始处理；
- ``running``：各页列表与扫描禁用，「开始处理」变「停止」；
- 任务终态后回到 ``select``（已入库条目保留，可增量补译）。

目录记忆：目录行可改（预填宿主当前游戏目录），变更成功后合并写
``last_game_dir`` 键到 config.json（经 ``config.update_config_keys``，
不覆盖其它键）。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import config, external_tools
from .pretranslate.formatting import (
    finished_text,
    format_checked,
    format_counting,
    format_db_count,
    format_dir_invalid,
    format_dir_ok,
    format_progress,
    format_progress_time,
    format_scan_result,
    format_tool_progress,
    format_tool_scan_result,
    normalize_game_dir,
    start_button_enabled,
)
from .pretranslate.overlay_paths import list_relative, resolve_scan_root, unrpa_root
from .pretranslate.parser import scan_rpy_files
from .pretranslate.runner import BatchOptions, BatchTask
from .tools_runner import DecompileTask, ExtractTask
from .translation_store import db_path_for, open_store

logger = logging.getLogger("renpy_overlay.prebuild_window")

#: 窗口初始尺寸（逻辑像素）
WINDOW_SIZE = (680, 640)
#: 关闭窗口时等待后台任务收束的最长秒数
_CLOSE_JOIN_TIMEOUT = 10.0

STATE_SELECT = "select"
STATE_RUNNING = "running"

#: 页签索引（同时决定进度文案口径与开始处理分派）
_PAGE_TRANSLATE = 0
_PAGE_DECOMPILE = 1
_PAGE_EXTRACT = 2

_PAGE_TITLES = (".rpy 翻译缓存", ".rpyc 反编译", ".rpa 解包")


class _FilePage(QWidget):
    """单个页签：页内扫描按钮 + 本类型文件列表 + 勾选工具行。

    只负责列表交互与勾选状态；扫描根判定、文件列举与任务构造由
    :class:`PrebuildWindow` 编排。
    """

    def __init__(self) -> None:
        super().__init__()
        self.scan_button = QPushButton("扫描", self)
        self.file_list = QListWidget(self)
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.select_all_button = QPushButton("全选", self)
        self.select_none_button = QPushButton("全不选", self)
        self.checked_label = QLabel(format_checked(0), self)

        scan_row = QHBoxLayout()
        scan_row.addStretch(1)
        scan_row.addWidget(self.scan_button)

        select_row = QHBoxLayout()
        select_row.addWidget(self.select_all_button)
        select_row.addWidget(self.select_none_button)
        select_row.addStretch(1)
        select_row.addWidget(self.checked_label)

        layout = QVBoxLayout(self)
        layout.addLayout(scan_row)
        layout.addWidget(self.file_list, 1)
        layout.addLayout(select_row)

    # ---- 列表操作 -----------------------------------------------------------

    def clear(self) -> None:
        self.file_list.clear()
        self.update_checked_label()

    def set_files(self, display_paths: list[str]) -> None:
        """填充列表：默认不勾选（支持分批）。"""
        self.file_list.clear()
        for text in display_paths:
            item = QListWidgetItem(text, self.file_list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
        self.update_checked_label()

    def toggle_at(self, pos) -> bool:
        """释放位置处的条目翻转勾选；命中返回 True（吃掉事件避免双重翻转）。"""
        item = self.file_list.itemAt(pos)
        if item is None:
            return False
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        self.update_checked_label()
        return True

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.file_list.count()):
            self.file_list.item(row).setCheckState(state)
        self.update_checked_label()

    def checked_files(self) -> list[str]:
        files: list[str] = []
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                files.append(item.text())
        return files

    def update_checked_label(self) -> None:
        self.checked_label.setText(format_checked(len(self.checked_files())))

    def set_enabled_state(self, enabled: bool) -> None:
        """列表与勾选工具行的可用态（扫描按钮由 PrebuildWindow 统一管理）。"""
        self.select_all_button.setEnabled(enabled)
        self.select_none_button.setEnabled(enabled)
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if enabled:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)


class PrebuildWindow(QMainWindow):
    """预构建翻译缓存主窗口（三页签：翻译缓存 / 反编译 / 解包）。"""

    def __init__(
        self,
        cfg: config.AppConfig,
        initial_game_dir: str = "",
        on_closed=None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._game_dir: Path | None = None
        self._task: BatchTask | DecompileTask | ExtractTask | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state = STATE_SELECT
        self._task_start_mono: float | None = None  # 已运行时间基准（任务开始时刻）
        self._total = 0  # 进度分母：页 0=待翻译条目数，页 1/2=勾选文件数
        self._current_page = _PAGE_TRANSLATE  # running 中正在执行的页（进度文案口径）
        self._page_scan_roots: dict[int, Path] = {}  # 各页最近一次扫描的根目录
        self._on_closed = on_closed  # 窗口关闭回调（宿主清引用，主线程调用）

        self.setWindowTitle("预构建翻译缓存")
        self.resize(*WINDOW_SIZE)

        # ---- 信号桥（跨线程 emit 自动排队回主线程） ------------------------
        self._signals = _make_task_signals()
        self._signals.counting.connect(self._on_counting)
        self._signals.ready.connect(self._on_ready)
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)

        # ---- 目录行（扫描按钮在各页签内） -----------------------------------
        self._dir_edit = QLineEdit(self)
        self._dir_edit.setReadOnly(True)
        self._dir_edit.setPlaceholderText("选择 Ren'Py 游戏根目录（含 game/ 子目录）…")
        self._browse_button = QPushButton("选择目录…", self)
        self._browse_button.clicked.connect(self._on_browse)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self._dir_edit, 1)
        dir_row.addWidget(self._browse_button)

        self._dir_info = QLabel("尚未选择游戏目录。", self)
        self._dir_info.setWordWrap(True)

        # ---- 三页签 ----------------------------------------------------------
        self._tabs = QTabWidget(self)
        self._pages = [_FilePage() for _ in range(3)]
        for index, (page, title) in enumerate(zip(self._pages, _PAGE_TITLES, strict=True)):
            self._tabs.addTab(page, title)
            page.scan_button.clicked.connect(lambda _checked=False, idx=index: self._on_scan(idx))
            page.select_all_button.clicked.connect(
                lambda _checked=False, p=page: p.set_all_checked(True)
            )
            page.select_none_button.clicked.connect(
                lambda _checked=False, p=page: p.set_all_checked(False)
            )
            page.file_list.viewport().installEventFilter(self)
        # 切页签后「开始处理」按当前页扫描结果刷新可用性
        self._tabs.currentChanged.connect(lambda _index: self._apply_state())

        # ---- 进度区 ---------------------------------------------------------
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._progress_label = QLabel("", self)
        self._time_label = QLabel("--", self)

        # ---- 按钮行（无设置按钮：配置统一收归宿主设置窗口） ------------------
        button_row = QHBoxLayout()
        self._start_button = QPushButton("开始处理", self)
        self._start_button.clicked.connect(self._on_start)
        self._stop_button = QPushButton("停止", self)
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._on_stop)
        button_row.addWidget(self._start_button)
        button_row.addWidget(self._stop_button)
        button_row.addStretch(1)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.addLayout(dir_row)
        layout.addWidget(self._dir_info)
        layout.addWidget(self._tabs, 1)
        layout.addWidget(self._progress_bar)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self._progress_label, 1)
        progress_row.addWidget(self._time_label)
        layout.addLayout(progress_row)
        layout.addLayout(button_row)
        self.setCentralWidget(central)

        # 启动预填入口链路给定的目录（注入模式为向上定位的游戏根；跳过
        # 注入模式为空——目录行留空占位，由用户在窗口内选择；不回退
        # last_game_dir 预填，可处理性完全由扫描结果决定）
        if initial_game_dir:
            self._apply_game_dir(initial_game_dir)
        else:
            self._apply_state()

    # ---- 事件过滤：整行点击切换勾选 -----------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """列表视口的释放事件：选择态下条目行内任意位置翻转该行复选框。

        拦截（返回 True）同时吃掉原生指示器的切换，避免双重翻转。
        """
        from PyQt6.QtCore import QEvent

        if (
            self._state == STATE_SELECT
            and event.type() == QEvent.Type.MouseButtonRelease
        ):
            for page in self._pages:
                if obj is page.file_list.viewport():
                    if page.toggle_at(event.position().toPoint()):
                        return True
                    break
        return False

    def closeEvent(self, event) -> None:  # noqa: N802
        """关闭窗口时优雅收束后台任务（置位停止、限时等待在途请求）。"""
        if self._thread is not None and self._thread.is_alive():
            logger.info("窗口关闭：等待后台任务收束…")
            self._stop_event.set()
            self._thread.join(timeout=_CLOSE_JOIN_TIMEOUT)
        if callable(self._on_closed):
            try:
                self._on_closed()
            except Exception:  # pragma: no cover - 回调失败不影响关闭
                logger.exception("预构建窗口关闭回调失败")
        super().closeEvent(event)

    # ---- 目录 ----------------------------------------------------------------

    def _on_browse(self) -> None:
        start = str(self._game_dir) if self._game_dir else (self._cfg.last_game_dir or "")
        selected = QFileDialog.getExistingDirectory(self, "选择游戏根目录", start)
        if not selected:
            return
        self._apply_game_dir(selected)

    def _apply_game_dir(self, selected: str) -> None:
        game_dir = normalize_game_dir(selected)
        if game_dir is None:
            self._dir_info.setText(format_dir_invalid(str(selected)))
            self._game_dir = None
            self._apply_state()
            return
        changed = self._game_dir != game_dir
        self._game_dir = game_dir
        self._dir_edit.setText(str(game_dir))
        self._dir_info.setText(format_dir_ok(game_dir, db_path_for(game_dir)))
        self._apply_state()
        self._refresh_db_count()
        # 目录变化后清空各页上一轮文件列表
        self._page_scan_roots.clear()
        for page in self._pages:
            page.clear()
        if changed:
            # 目录记忆：合并写 last_game_dir，不覆盖 config.json 其它键
            config.update_config_keys({"last_game_dir": str(game_dir)})

    def _apply_scan_buttons(self) -> None:
        enabled = self._state == STATE_SELECT and self._game_dir is not None
        for page in self._pages:
            page.scan_button.setEnabled(enabled)

    # ---- 扫描 ----------------------------------------------------------------

    def _on_scan(self, index: int) -> None:
        if self._game_dir is None or self._state != STATE_SELECT:
            return
        if index == _PAGE_TRANSLATE:
            rels, scan_root = self._scan_translate_page()
            message = format_scan_result(len(rels))
        else:
            suffix = ".rpyc" if index == _PAGE_DECOMPILE else ".rpa"
            # 页 1：unrpa 判定同形（与页 0 独立）；页 2：恒扫 game
            scan_root = (
                resolve_scan_root(self._game_dir, suffix)
                if index == _PAGE_DECOMPILE
                else self._game_dir / "game"
            )
            rels = list_relative(scan_root, suffix)
            message = format_tool_scan_result(len(rels), suffix)
        self._page_scan_roots[index] = scan_root
        prefix = "unrpa/" if scan_root == unrpa_root(self._game_dir) else "game/"
        self._pages[index].set_files([f"{prefix}{rel}" for rel in rels])
        self._progress_label.setText(message)
        # 扫描结果决定「开始处理」可用性（含扫不到文件时的禁用）
        self._apply_state()
        logger.info("页 %d 扫描完成：%d 个文件（扫描根=%s）", index, len(rels), scan_root)

    def _scan_translate_page(self) -> tuple[list[str], Path]:
        """页 0 扫描：返回（相对扫描根路径列表, 扫描根）。

        unrpa 根 → 通用递归列举；game 根 → 沿用既有 :func:`scan_rpy_files`
        （tl 过滤 + .rpa 归档内条目），剥去 "game/" 前缀归一为相对扫描根。
        """
        assert self._game_dir is not None
        scan_root = resolve_scan_root(self._game_dir, ".rpy")
        if scan_root == unrpa_root(self._game_dir):
            return list_relative(scan_root, ".rpy"), scan_root
        # tl 过滤：include_tl 关闭 → 剔除全部 tl 文件（tl_langs=[]）；
        # 开启 → 配置的语言列表（空 = None = 全部语言）
        if self._cfg.include_tl_strings:
            tl_param: list[str] | None = list(self._cfg.tl_langs) or None
        else:
            tl_param = []
        files = scan_rpy_files(self._game_dir, tl_langs=tl_param)
        return [name.removeprefix("game/") for name in files], scan_root

    # ---- 勾选 ----------------------------------------------------------------

    @staticmethod
    def _rel_of(display_text: str) -> str:
        """列表展示文案（``game/…`` / ``unrpa/…``）→ 相对扫描根路径。"""
        _, _, rel = display_text.partition("/")
        return rel

    # ---- 启动 / 停止 -----------------------------------------------------------

    def _on_start(self) -> None:
        if self._state != STATE_SELECT or self._game_dir is None:
            return
        index = self._tabs.currentIndex()
        checked = self._pages[index].checked_files()
        if not checked:
            self._progress_label.setText("尚未勾选任何文件，请先勾选后开始处理。")
            return
        if index == _PAGE_TRANSLATE:
            self._start_translate_task(checked)
        else:
            self._start_tool_task(checked, index)

    def _start_translate_task(self, checked: list[str]) -> None:
        """页 0：完整翻译流程（源根可为 unrpa，数据库仍在游戏根）。"""
        assert self._game_dir is not None
        store = open_store(self._game_dir)
        if store is None:
            self._progress_label.setText(
                f"无法创建翻译数据库：{db_path_for(self._game_dir)}（目录不可写？）"
            )
            return
        self._stop_event.clear()
        self._task = BatchTask(
            files=[self._rel_of(text) for text in checked],
            game_dir=self._game_dir,
            options=BatchOptions.from_config(self._cfg),
            store=store,
            stop_event=self._stop_event,
            on_counting=self._signals.counting.emit,
            on_ready=self._signals.ready.emit,
            on_progress=lambda done, failed, conc: self._signals.progress.emit(
                done, failed, conc
            ),
            on_finished=self._signals.finished.emit,
            source_dir=self._page_scan_roots.get(_PAGE_TRANSLATE, self._game_dir / "game"),
        )
        self._begin_running(_PAGE_TRANSLATE)
        self._total = 0
        self._progress_bar.setRange(0, 0)  # 统计阶段：忙碌指示
        self._progress_bar.setValue(0)
        self._progress_label.setText("统计中…")
        self._time_label.setText(format_progress_time(0, 0, 0.0))
        logger.info("批量处理已启动：%d 个文件", len(checked))

    def _start_tool_task(self, checked: list[str], index: int) -> None:
        """页 1/2：反编译 / 解包任务（外部工具缺失 → 提示不启动）。"""
        assert self._game_dir is not None
        if index == _PAGE_DECOMPILE:
            try:
                exe = external_tools.unrpyc_exe()
            except FileNotFoundError:
                self._progress_label.setText(
                    f"未找到反编译工具：{external_tools.plugin_dir() / 'unrpyc.exe'}"
                )
                return
        else:
            try:
                exe = external_tools.rpaextract_exe()
            except FileNotFoundError:
                self._progress_label.setText(
                    f"未找到解包工具：{external_tools.plugin_dir() / 'rpaextract.exe'}"
                )
                return
        rels = [self._rel_of(text) for text in checked]
        callbacks = dict(
            stop_event=self._stop_event,
            on_ready=self._signals.ready.emit,
            on_progress=lambda done, failed, skipped: self._signals.progress.emit(
                done, failed, skipped
            ),
            on_finished=self._signals.finished.emit,
        )
        if index == _PAGE_DECOMPILE:
            scan_root = self._page_scan_roots.get(_PAGE_DECOMPILE, self._game_dir / "game")
            self._task = DecompileTask(
                rels, self._game_dir, scan_root, exe, **callbacks
            )
        else:
            self._task = ExtractTask(rels, self._game_dir, exe, **callbacks)
        self._begin_running(index)
        self._total = len(rels)
        self._progress_bar.setRange(0, max(1, self._total))
        self._progress_bar.setValue(0)
        self._progress_label.setText(format_tool_progress(0, self._total, 0, 0))
        self._time_label.setText(format_progress_time(0, self._total, 0.0))
        logger.info("工具任务已启动（页 %d）：%d 个文件", index, len(rels))

    def _begin_running(self, page_index: int) -> None:
        assert self._task is not None
        self._current_page = page_index
        self._state = STATE_RUNNING
        self._apply_state()
        self._task_start_mono = time.monotonic()  # 已运行时间从进入 running 起算
        self._thread = threading.Thread(
            target=self._task.run, daemon=True, name="batch-task"
        )
        self._thread.start()

    def _on_stop(self) -> None:
        if self._state == STATE_RUNNING:
            self._stop_event.set()
            self._progress_label.setText("正在停止…（等待在途请求收束）")

    # ---- 任务回调（主线程） ------------------------------------------------------

    def _on_counting(self, discovered: int) -> None:
        self._progress_label.setText(format_counting(0, 0, discovered))
        self._update_time_label(0)  # 统计阶段：已用时间照常走，剩余无依据显示 "--"

    def _on_ready(self, total: int) -> None:
        self._total = int(total)
        self._progress_bar.setRange(0, max(1, self._total))
        self._progress_bar.setValue(0)
        if self._current_page == _PAGE_TRANSLATE:
            self._progress_label.setText(format_progress(0, self._total, 0, 0))
        else:
            self._progress_label.setText(format_tool_progress(0, self._total, 0, 0))

    def _on_progress(self, done: int, failed: int, third: int) -> None:
        self._progress_bar.setValue(min(done, self._progress_bar.maximum()))
        if self._current_page == _PAGE_TRANSLATE:
            # third = 实时并发档位
            self._progress_label.setText(format_progress(done, self._total, failed, third))
        else:
            # third = 已跳过文件数
            self._progress_label.setText(
                format_tool_progress(done, self._total, failed, third)
            )
        self._update_time_label(done)

    def _on_finished(self, status: str) -> None:
        prefix = self._progress_label.text()
        self._progress_label.setText(f"{prefix} {finished_text(status)}".strip())
        # 终态复位：时间行回落占位符，不残留过期估算
        self._task_start_mono = None
        self._total = 0
        self._time_label.setText("--")
        self._state = STATE_SELECT
        self._thread = None
        self._task = None
        self._apply_state()
        self._refresh_db_count()
        logger.info("批量处理收束：status=%s（页 %d）", status, self._current_page)

    def _update_time_label(self, done: int) -> None:
        """时间信息行刷新（统计 / 进度回调时同步调用；终态由 _on_finished 复位）。"""
        if self._task_start_mono is None:
            return
        elapsed = time.monotonic() - self._task_start_mono
        self._time_label.setText(
            format_progress_time(done, self._total, elapsed, now_epoch=time.time())
        )

    # ---- 状态与工具 -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """后台任务在途（宿主 is_busy 互斥判定用）。"""
        return self._state == STATE_RUNNING

    def _apply_state(self) -> None:
        running = self._state == STATE_RUNNING
        # 开始处理：选择态 + 目录有效 + 当前页扫描到合法文件（可处理性
        # 完全由扫描结果决定；扫不到文件保持不可用并已由进度行提示）
        self._start_button.setEnabled(
            start_button_enabled(
                running,
                self._game_dir is not None,
                self._pages[self._tabs.currentIndex()].file_list.count(),
            )
        )
        self._start_button.setText("停止" if running else "开始处理")
        self._stop_button.setEnabled(running)
        self._browse_button.setEnabled(not running)
        self._apply_scan_buttons()
        enabled = not running
        for page in self._pages:
            page.set_enabled_state(enabled)

    def _refresh_db_count(self) -> None:
        if self._game_dir is None:
            return
        store = open_store(self._game_dir)
        if store is None:
            self.statusBar().showMessage(format_db_count(-1))
            return
        count = store.count()
        store.close()
        self.statusBar().showMessage(format_db_count(count))


def _make_task_signals():
    """构造任务信号对象：信号须声明在 QObject 子类体内（Qt 元对象约束）。"""
    from PyQt6.QtCore import QObject

    class TaskSignals(QObject):
        counting = pyqtSignal(int)
        ready = pyqtSignal(int)
        progress = pyqtSignal(int, int, int)
        finished = pyqtSignal(str)

    return TaskSignals()
