"""语音识别历史窗口：三列浏览 ``asr.db``（播放/时长倒计时/语音文本）。

布局与交互（需求给定 future_视频录制.txt，参考翻译/识曲历史窗口）：顶部
「删除」按钮；下部 QTableView 三列表格 —— 第一列为播放/停止按钮（逐行
setIndexWidget），第二列显示音频时长（播放中每秒倒计时剩余时长，停止或
播放结束后恢复时长），第三列为语音文本。

行为约定（需求）：

- 删除：删除当前选中行并同步删除数据库记录，随后重建表格并原位回选；
  删除正在播放的行先停止播放；
- 复制：选中条目 Ctrl+C 直接复制该行语音文本（不复制时长或音频本身）；
- 播放互斥：同时只播放一条音频，点击其他播放按钮先停止前一条再播放新的；
- 本窗口只读浏览已入库记录；窗口关闭 = 隐藏（单例复用，由宿主持有）。

离线测试约定：不构造真实 QWidget —— 换算/显示纯函数与表格模型直接单测
（模型仅依赖 QCoreApplication），播放适配器经鸭子接口注入 stub。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import (
    QAbstractTableModel,
    QBuffer,
    QByteArray,
    QModelIndex,
    Qt,
    QTimer,
)
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .asr_store import AsrStore

logger = logging.getLogger("renpy_overlay.video.asr_history_window")

#: 三列表头（需求：播放/停止、时长倒计时、语音文本）
HEADERS = ("播放", "时长", "语音文本")
#: 语音文本列（0-based；Ctrl+C 只复制该列，需求）
TEXT_COLUMN = 2
#: 播放按钮的两种文案（需求：第一列显示播放/停止按钮）
PLAY_TEXT = "播放"
STOP_TEXT = "停止"
#: 匹配行高亮色（暗色系配色参照识曲历史窗口：深底浅字 + 蓝色强调）
MATCH_COLOR = QColor(45, 70, 110)
#: 模型接口的空父索引默认值（invalid QModelIndex 无状态，可共享；规避 B008）
_NO_PARENT = QModelIndex()


def format_duration(seconds: int) -> str:
    """时长显示 ``mm:ss``（纯函数）：负值钳零。"""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def duration_cell_text(duration: int, playing_row: int | None, row: int, remaining: int) -> str:
    """第二列显示文本（纯函数）：播放行显示剩余倒计时，其余显示音频时长。"""
    if playing_row is not None and row == playing_row:
        return format_duration(remaining)
    return format_duration(duration)


def _index_after_delete(deleted_index: int, new_count: int) -> int:
    """计算删除后应回位选中的新行索引（纯函数，与识曲历史同一公式）。

    优先保持原位置（被删行的下一行顺位前移到该处）；若原索引在新列表中
    越界（删的是末尾行）则回退到前一行；删除后已无任何记录返回 -1。
    """
    if new_count <= 0:
        return -1
    return min(deleted_index, new_count - 1)


class _AsrTableModel(QAbstractTableModel):
    """语音识别记录三列表格模型：行 = ``(asr_id, duration, text)``。

    第一列不放数据（播放按钮经 setIndexWidget 由窗口管理）；第二列播放行
    显示倒计时（窗口每秒经 :meth:`set_remaining` 刷新）；第三列语音文本。
    """

    def __init__(self, store: AsrStore | None) -> None:
        super().__init__()
        self._store = store
        #: 行缓存与 AsrStore.entries() 同构：(asr_id, duration, text)
        self._rows: list[tuple[int, int, str]] = []
        self._playing_row: int | None = None
        self._remaining = 0

    # ---- Qt 标准模型接口 ---------------------------------------------------

    def rowCount(self, parent: QModelIndex = _NO_PARENT) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = _NO_PARENT) -> int:  # noqa: N802
        return len(HEADERS)

    def headerData(self, section: int, orientation, role=int):  # noqa: N802
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(HEADERS)
        ):
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        asr_id, duration, text = self._rows[index.row()]
        if index.column() == 1:
            return duration_cell_text(duration, self._playing_row, index.row(), self._remaining)
        if index.column() == TEXT_COLUMN:
            return text
        return None  # 第一列由按钮 widget 呈现

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ---- 数据供给与播放状态 -------------------------------------------------

    def set_records(self, records: list[tuple[int, int, str]]) -> None:
        """整体重建行缓存（打开窗口 refresh / 删除后调用）。"""
        self.beginResetModel()
        self._rows = list(records)
        self.endResetModel()

    def set_playing_row(self, row: int | None) -> None:
        """更新播放状态行（None = 全部停止）：刷新按钮文案与倒计时显示。"""
        old = self._playing_row
        self._playing_row = row
        rows = {r for r in (old, row) if r is not None and 0 <= r < len(self._rows)}
        for r in rows:
            self.dataChanged.emit(self.index(r, 0), self.index(r, len(HEADERS) - 1))

    def set_remaining(self, remaining: int) -> None:
        """更新播放行剩余秒数并仅刷新该行（倒计时每秒 tick）。"""
        self._remaining = max(0, int(remaining))
        row = self._playing_row
        if row is not None and 0 <= row < len(self._rows):
            self.dataChanged.emit(self.index(row, 1), self.index(row, 1))

    @property
    def playing_row(self) -> int | None:
        return self._playing_row

    def row_id_at(self, row: int) -> int | None:
        """第 ``row`` 行的 asr_id；越界返回 None。"""
        if 0 <= row < len(self._rows):
            return self._rows[row][0]
        return None

    def duration_at(self, row: int) -> int:
        """第 ``row`` 行的音频时长（秒）；越界返回 0。"""
        if 0 <= row < len(self._rows):
            return self._rows[row][1]
        return 0

    def text_at(self, row: int) -> str:
        """第 ``row`` 行的语音文本（Ctrl+C 复制用）；越界返回空串。"""
        if 0 <= row < len(self._rows):
            return self._rows[row][2]
        return ""


class _TextCopyTableView(QTableView):
    """Ctrl+C 只复制选中行语音文本的表格视图（需求：不复制时长或音频）。"""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._copy_text_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _copy_text_selection(self) -> None:
        model = self.model()
        if model is None:
            return
        texts: list[str] = []
        for index in sorted(
            self.selectionModel().selectedIndexes(), key=lambda i: i.row()
        ):
            if index.column() == TEXT_COLUMN:
                text = str(model.data(index) or "")
                if text:
                    texts.append(text)
        if texts:
            QApplication.clipboard().setText("\n".join(texts))
            logger.debug("已复制语音识别文本（%d 条）", len(texts))


class _QtPlayerAdapter:
    """QMediaPlayer 封装：播放内存 wav 字节（真实实现；离线测试注入 stub）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

        self._player = QMediaPlayer(parent)
        self._audio = QAudioOutput(parent)
        self._player.setAudioOutput(self._audio)
        self._buffer = QBuffer(parent)
        #: 播放自然结束（EndOfMedia）回调（由窗口注入，倒计时复位用）
        self.on_finished = None
        self._player.mediaStatusChanged.connect(self._on_status)

    def _on_status(self, status) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.on_finished:
            self.on_finished()

    def play_bytes(self, blob: bytes) -> None:
        self._buffer.close()
        self._buffer.setData(QByteArray(blob))
        self._buffer.open(QBuffer.OpenModeFlag.ReadOnly)
        self._player.setSourceDevice(self._buffer)
        self._player.play()

    def stop(self) -> None:
        self._player.stop()


class AsrHistoryWindow(QWidget):
    """语音识别历史窗口（单例复用：close = hide，refresh 重建数据）。"""

    def __init__(self, store: AsrStore | None, player=None) -> None:
        super().__init__()
        self._store = store
        self.setWindowTitle("语音识别历史")
        self.resize(920, 560)
        self.setPalette(self._dark_palette())
        self.setAutoFillBackground(True)

        #: 播放适配器（鸭子接口 play_bytes/stop；离线测试注入 stub）
        self._player = player if player is not None else _QtPlayerAdapter(self)
        self._player.on_finished = self._finish_playback
        self._playing_row = -1  # 当前播放行（-1 = 无）
        self._remaining = 0

        # 顶部：删除按钮（需求支持逐条删除；不做查找——需求未要求）
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setEnabled(False)  # 无选中行时不可用
        self._delete_btn.clicked.connect(self._delete_selected)
        top_row = QHBoxLayout()
        top_row.addStretch(1)
        top_row.addWidget(self._delete_btn)

        # 下部：三列表格（整行选择：删除/复制都以行为单位）
        self._model = _AsrTableModel(store)
        self._table = _TextCopyTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self._table, stretch=1)

        #: 倒计时定时器：播放中每秒刷新剩余时长
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)
        #: 行播放按钮（row → QPushButton；refresh 时重建）
        self._buttons: dict[int, QPushButton] = {}

    @staticmethod
    def _dark_palette() -> QPalette:
        """暗色调色板（与识曲历史窗口同一色彩方案）。"""
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
        return palette

    # ---- 对外接口 -----------------------------------------------------------

    def refresh(self) -> None:
        """从数据库重建表格内容（每次打开/删除/新识别入库后调用）。"""
        playing_id = (
            self._model.row_id_at(self._playing_row) if self._playing_row >= 0 else None
        )
        self._stop_playback()
        records = self._store.entries() if self._store is not None else []
        self._model.set_records(records)
        self._delete_btn.setEnabled(False)  # 重建后无选中行
        self._rebuild_play_buttons()
        if playing_id is not None and self._playing_row == -1:
            logger.debug("刷新丢弃播放状态（asr_id=%s 已不在列表）", playing_id)
        if not records:
            logger.info(
                "语音识别历史为空（数据库 %s）",
                self._store.path if self._store is not None else "<无>",
            )

    # ---- 播放（单条互斥：切播放先停前一条） ---------------------------------------

    def _rebuild_play_buttons(self) -> None:
        """为每一行第一列放置播放按钮（setIndexWidget；文案随播放状态切换）。"""
        for button in self._buttons.values():
            button.deleteLater()
        self._buttons.clear()
        for row in range(self._model.rowCount()):
            button = QPushButton(PLAY_TEXT)
            button.clicked.connect(lambda _=False, r=row: self._toggle_play(r))
            self._table.setIndexWidget(self._model.index(row, 0), button)
            self._buttons[row] = button

    def _toggle_play(self, row: int) -> None:
        if not 0 <= row < self._model.rowCount():
            return
        if row == self._playing_row:
            self._stop_playback()  # 再点当前行：停止并恢复时长显示
            return
        asr_id = self._model.row_id_at(row)
        blob = self._store.audio(asr_id) if (self._store is not None and asr_id is not None) else None
        if not blob:
            logger.warning("读取音频失败（asr_id=%s），忽略播放请求", asr_id)
            return
        if self._playing_row >= 0:
            self._stop_playback()  # 单播放互斥：先停止前一条（需求）
        self._player.play_bytes(blob)
        self._playing_row = row
        self._remaining = self._model.duration_at(row)
        self._model.set_playing_row(row)
        self._model.set_remaining(self._remaining)
        self._update_button_texts()
        self._countdown_timer.start()
        logger.info("开始播放语音识别音频（asr_id=%s，%ds）", asr_id, self._remaining)

    def _stop_playback(self) -> None:
        """停止播放并恢复该行时长显示（按钮文案复位）。"""
        if self._playing_row < 0:
            return
        self._player.stop()
        self._finish_playback()

    def _finish_playback(self) -> None:
        """播放结束（手动停止或自然播完）：复位倒计时与按钮文案。"""
        self._countdown_timer.stop()
        row, self._playing_row = self._playing_row, -1
        self._model.set_playing_row(None)
        self._update_button_texts()
        if row >= 0:
            logger.debug("播放结束（row=%d），已恢复时长显示", row)

    def _on_countdown_tick(self) -> None:
        """倒计时（需求：开始播放后倒计时显示剩余时长，结束恢复时长）。"""
        self._remaining -= 1
        if self._remaining <= 0:
            self._finish_playback()
            return
        self._model.set_remaining(self._remaining)

    def _update_button_texts(self) -> None:
        for row, button in self._buttons.items():
            button.setText(STOP_TEXT if row == self._playing_row else PLAY_TEXT)

    # ---- 选择与删除 -----------------------------------------------------------

    def _on_selection_changed(self, *_args) -> None:
        self._delete_btn.setEnabled(self._table.currentIndex().row() >= 0)

    def _select_row(self, row: int) -> None:
        """选中整行并滚动到可视区中央（删除回位共用）。"""
        self._table.selectRow(row)
        self._table.scrollTo(
            self._model.index(row, 0),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    def _delete_selected(self) -> None:
        """删除当前选中行并同步删除数据库记录，随后重建表格并原位回选。"""
        row = self._table.currentIndex().row()
        if row < 0:
            logger.info("没有选中的语音识别记录，忽略删除请求")
            return
        asr_id = self._model.row_id_at(row)
        if asr_id is None:
            return
        if row == self._playing_row:
            self._stop_playback()  # 删除正在播放的行先停止
        if self._store is None or not self._store.delete(asr_id):
            logger.warning("删除语音识别记录失败（asr_id=%s）", asr_id)
            return
        logger.info("已删除语音识别记录 asr_id=%s", asr_id)
        self.refresh()
        new_index = _index_after_delete(row, self._model.rowCount())
        if new_index != -1:
            self._select_row(new_index)

    # ---- 生命周期 -----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()  # 单例复用：关闭 = 隐藏，宿主退出时统一销毁
