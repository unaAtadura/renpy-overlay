"""识曲历史浏览窗口：常规窗口阅读 ``game_song.db`` 的 ``music_master`` 表。

布局与交互（需求给定 .raw_plans/future_识曲历史窗口.txt）：上下两部分 ——
上部为「查找」标签 + 文本输入框 + 上一个/下一个/删除按钮；下部为
QTableView + QAbstractTableModel 的四列表格（曲名/艺术家/游戏场景/备注），
按数据库顺序滚动浏览，外观同四列的 Excel 表格。

行为约定：

- 查找：精确匹配（整格相等）、不区分大小写，上一个/下一个循环跳转到匹配
  行并整行高亮；所有列都参与匹配；无匹配与空库只记日志，不做界面提示；
- 删除：删除当前选中行并同步删除数据库记录，随后重建表格并原位回选；
- 编辑：仅第三、四列（游戏场景/备注）可编辑，编辑文本框内回车提交时写回
  数据库；第一、二列（曲名/艺术家）只读；
- 复制：Ctrl+C 把选中单元格文本复制到剪贴板；
- 本窗口只读浏览已入库记录，不触发录制或识别；窗口关闭 = 隐藏（单例复用，
  由宿主持有）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .store import SongStore

logger = logging.getLogger("renpy_overlay.song_recognition.history_window")

#: 四列表头（需求：曲名/艺术家/游戏场景/备注）
HEADERS = ("曲名", "艺术家", "游戏场景", "备注")
#: 可编辑列（0-based；需求：第三、四列可编辑，在文本框内回车写入数据库）
EDITABLE_COLUMNS = (2, 3)
#: 匹配行高亮色（暗色系配色参照截图历史窗口：深底浅字 + 蓝色强调）
MATCH_COLOR = QColor(45, 70, 110)
#: 模型接口的空父索引默认值（invalid QModelIndex 无状态，可共享；规避 B008）
_NO_PARENT = QModelIndex()


def normalize(text) -> str:
    """查找与比较用的归一化：None → 空串，去除首尾空白并转小写。"""
    return str(text or "").strip().casefold()


def next_match(row_texts: list[tuple[str, ...]], query: str, start: int, forward: bool) -> int:
    """查找下一个匹配行索引（纯函数，便于离线单测）。

    精确匹配：任一列归一化后与查询词相等（不区分大小写）即命中；从 ``start``
    行起沿 ``forward`` 方向循环扫描（next 从下一行、prev 从上一行开始）；
    ``start`` 为 -1（无当前行）时 next 从第 0 行、prev 从最后一行开始；
    找不到或查询词为空返回 -1。
    """
    count = len(row_texts)
    needle = normalize(query)
    if count == 0 or not needle:
        return -1
    base = start if start >= 0 else (-1 if forward else count)
    for offset in range(1, count + 1):
        index = (base + (offset if forward else -offset)) % count
        if needle in (normalize(cell) for cell in row_texts[index]):
            return index
    return -1


def _index_after_delete(deleted_index: int, new_count: int) -> int:
    """计算删除后应回位选中的新行索引（纯函数，便于离线单测）。

    优先保持原位置（被删行的下一行顺位前移到该处）；若原索引在新列表中
    越界（删的是末尾行）则回退到前一行；删除后已无任何记录返回 -1。
    """
    if new_count <= 0:
        return -1
    return min(deleted_index, new_count - 1)


class _SongTableModel(QAbstractTableModel):
    """识曲记录四列表格模型：曲名/艺术家/游戏场景/备注。

    仅第三、四列可编辑（需求）：编辑提交（文本框内回车）时经
    :meth:`SongStore.update_meta` 写回数据库，成功才更新缓存行；匹配当前
    查询词的行经 BackgroundRole 整行高亮。
    """

    def __init__(self, store: SongStore | None) -> None:
        super().__init__()
        self._store = store
        #: 行缓存与 SongStore.entries() 同构：(song_id, song_name, artist, game_scene, remark)
        self._rows: list[tuple[int, str, str, str | None, str | None]] = []
        self._query = ""  # 归一化后的查找词（"" = 无高亮）

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
        if role == Qt.ItemDataRole.BackgroundRole:
            if not self._query:
                return None
            row = self._rows[index.row()]
            if self._query in (normalize(cell) for cell in row[1:]):
                return MATCH_COLOR
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None
        cell = self._rows[index.row()][index.column() + 1]
        return str(cell or "")  # game_scene/remark 留空（NULL）显示空串

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() in EDITABLE_COLUMNS:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        """第三、四列编辑提交：写库成功才更新缓存行（需求：回车写入数据库）。"""
        if role != Qt.ItemDataRole.EditRole or index.column() not in EDITABLE_COLUMNS:
            return False
        row = index.row()
        if not 0 <= row < len(self._rows):
            return False
        song_id, song_name, artist, game_scene, remark = self._rows[row]
        text = str(value or "")
        if index.column() == 2:
            game_scene = text
        else:
            remark = text
        if self._store is None or not self._store.update_meta(
            song_id, game_scene=game_scene, remark=remark
        ):
            logger.warning("写入识曲备注失败（song_id=%s）", song_id)
            return False
        self._rows[row] = (song_id, song_name, artist, game_scene, remark)
        self.dataChanged.emit(index, index)
        return True

    # ---- 数据供给与查找辅助 -------------------------------------------------

    def set_records(self, records: list[tuple[int, str, str, str | None, str | None]]) -> None:
        """整体重建行缓存（打开窗口 refresh / 删除后调用）。"""
        self.beginResetModel()
        self._rows = list(records)
        self.endResetModel()

    def set_query(self, query: str) -> None:
        """更新查找词并刷新整表高亮（BackgroundRole）。"""
        normalized = normalize(query)
        if normalized == self._query:
            return
        self._query = normalized
        if self._rows:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self._rows) - 1, len(HEADERS) - 1)
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.BackgroundRole])

    def row_texts(self) -> list[tuple[str, str, str, str]]:
        """全部行的四列展示文本（供查找纯函数消费）。"""
        return [
            (str(row[1] or ""), str(row[2] or ""), str(row[3] or ""), str(row[4] or ""))
            for row in self._rows
        ]

    def song_id_at(self, row: int) -> int | None:
        """第 ``row`` 行的 song_id；越界返回 None。"""
        if 0 <= row < len(self._rows):
            return self._rows[row][0]
        return None


class _CopyTableView(QTableView):
    """支持 Ctrl+C 复制选中单元格文本的表格视图（选中格按行列拼接）。"""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _copy_selection(self) -> None:
        selection = self.selectionModel()
        if selection is None:
            return
        indexes = sorted(selection.selectedIndexes(), key=lambda i: (i.row(), i.column()))
        lines: list[str] = []
        line: list[str] = []
        current_row = -1
        for index in indexes:
            if current_row != -1 and index.row() != current_row:
                lines.append("\t".join(line))
                line = []
            current_row = index.row()
            line.append(str(self.model().data(index) or ""))
        if line:
            lines.append("\t".join(line))
        if lines:
            QApplication.clipboard().setText("\n".join(lines))
            logger.debug("已复制识曲记录文本（%d 行）", len(lines))


def _dark_palette() -> QPalette:
    """识曲历史窗口的暗色调色板（色彩方案参照截图历史窗口：深底浅字 + 蓝强调）。"""
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


class SongHistoryWindow(QWidget):
    """识曲历史浏览窗口（单例复用：close = hide，refresh 重建数据）。"""

    def __init__(self, store: SongStore | None) -> None:
        super().__init__()
        self._store = store
        self.setWindowTitle("识曲历史")
        self.resize(920, 560)
        self.setPalette(_dark_palette())
        self.setAutoFillBackground(True)

        # 上部：查找行（标签 + 输入框 + 上一个/下一个/删除）
        self._search_edit = QLineEdit()
        self._prev_btn = QPushButton("上一个")
        self._next_btn = QPushButton("下一个")
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setEnabled(False)  # 无选中行时不可用
        self._prev_btn.clicked.connect(lambda: self._find(forward=False))
        self._next_btn.clicked.connect(lambda: self._find(forward=True))
        self._delete_btn.clicked.connect(self._delete_selected)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("查找"))
        top_row.addWidget(self._search_edit, stretch=1)
        top_row.addWidget(self._prev_btn)
        top_row.addWidget(self._next_btn)
        top_row.addWidget(self._delete_btn)

        # 下部：四列表格（整行选择：查找跳转 / 删除 / 复制都以行为单位）
        self._model = _SongTableModel(store)
        self._table = _CopyTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addWidget(self._table, stretch=1)

    # ---- 对外接口 -----------------------------------------------------------

    def refresh(self) -> None:
        """从数据库重建表格内容（每次打开/删除后调用）。

        空库只记日志不做界面提示（需求：无需提示）；重建后保留当前查找词的
        高亮效果（窗口隐藏期间数据库可能已被新识曲结果更新）。
        """
        records = self._store.entries() if self._store is not None else []
        self._model.set_records(records)
        self._delete_btn.setEnabled(False)  # 重建后无选中行
        self._model.set_query(self._search_edit.text())
        if not records:
            logger.info(
                "识曲历史为空（数据库 %s）",
                self._store.path if self._store is not None else "<无>",
            )

    # ---- 查找 ---------------------------------------------------------------

    def _find(self, forward: bool) -> None:
        """上一个/下一个：循环跳转到匹配行并整行高亮（需求：精确匹配、不区分大小写）。"""
        query = self._search_edit.text()
        index = next_match(
            self._model.row_texts(), query, self._table.currentIndex().row(), forward
        )
        self._model.set_query(query)
        if index == -1:
            logger.info("识曲历史查找无匹配（query=%r）", query)
            return
        self._select_row(index)

    def _select_row(self, row: int) -> None:
        """选中整行并滚动到可视区中央（跳转/删除回位共用）。"""
        self._table.selectRow(row)
        self._table.scrollTo(
            self._model.index(row, 0),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    # ---- 删除 ---------------------------------------------------------------

    def _delete_selected(self) -> None:
        """删除当前选中行并同步删除数据库记录，随后重建表格并原位回选。"""
        row = self._table.currentIndex().row()
        if row < 0:
            logger.info("没有选中的识曲记录，忽略删除请求")
            return
        song_id = self._model.song_id_at(row)
        if song_id is None:
            return
        if self._store is None or not self._store.delete(song_id):
            logger.warning("删除识曲记录失败（song_id=%s）", song_id)
            return
        logger.info("已删除识曲记录 song_id=%s", song_id)
        self.refresh()
        new_index = _index_after_delete(row, self._model.rowCount())
        if new_index != -1:
            self._select_row(new_index)

    # ---- 生命周期 -----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()  # 单例复用：关闭 = 隐藏，宿主退出时统一销毁
