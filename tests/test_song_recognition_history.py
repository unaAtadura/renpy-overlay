"""识曲历史窗口的离线验证：查找/回位纯函数、表格模型与 store 读写接口。

遵循项目离线测试惯例：不实例化 QWidget（窗口几何与交互不测），查找与删除
回位抽成模块级纯函数直接验证；模型仅依赖 QCoreApplication（无显示环境），
配合 stub/真库 SongStore 验证列表加载、留空展示、编辑写库与刷新。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication, Qt

from renpy_overlay.song_recognition.history_window import (
    EDITABLE_COLUMNS,
    HEADERS,
    MATCH_COLOR,
    _index_after_delete,
    _SongTableModel,
    next_match,
    normalize,
)
from renpy_overlay.song_recognition.store import open_store


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """QAbstractTableModel 是 QObject：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


# ---------------------------------------------------------------- 查找纯函数


def _rows(*texts: str) -> list[tuple[str, ...]]:
    return [(text,) for text in texts]


def test_next_match_forward_and_backward():
    rows = _rows("A", "B", "C", "B")
    assert next_match(rows, "B", 0, forward=True) == 1  # 从下一行起向后
    assert next_match(rows, "B", 1, forward=True) == 3  # 命中行之后继续找
    assert next_match(rows, "B", 0, forward=False) == 3  # 向上：循环回绕到末尾


def test_next_match_wraps_around():
    rows = _rows("A", "B", "C")
    assert next_match(rows, "A", 1, forward=True) == 0  # 向后循环回绕
    assert next_match(rows, "C", 0, forward=False) == 2


def test_next_match_without_current_row():
    rows = _rows("A", "B")
    assert next_match(rows, "B", -1, forward=True) == 1  # 无当前行：从第 0 行起
    assert next_match(rows, "B", -1, forward=False) == 1  # 无当前行：从最后一行起
    assert next_match(rows, "A", -1, forward=True) == 0
    assert next_match(rows, "A", -1, forward=False) == 0


def test_next_match_exact_and_case_insensitive():
    rows = _rows("My Song", "Other", "my song")
    assert next_match(rows, "MY SONG", -1, forward=True) == 0  # 不区分大小写
    assert next_match(rows, "My", -1, forward=True) == -1  # 子串不算（精确匹配）


def test_next_match_searches_every_column():
    rows = [("Song", "Artist", "战斗", ""), ("X", "Y", "Z", "城镇")]
    assert next_match(rows, "Artist", -1, forward=True) == 0  # 第二列命中
    assert next_match(rows, "战斗", -1, forward=True) == 0  # 第三列命中
    assert next_match(rows, "城镇", -1, forward=True) == 1  # 第四列命中


def test_next_match_no_hit_or_no_query():
    rows = _rows("A", "B")
    assert next_match(rows, "C", -1, forward=True) == -1
    assert next_match(rows, "", -1, forward=True) == -1  # 空查询词不查找
    assert next_match([], "A", -1, forward=True) == -1  # 空表


def test_normalize_casefold_and_none():
    assert normalize("  AbC ") == "abc"
    assert normalize(None) == ""


# ---------------------------------------------------------------- 删除回位


def test_index_after_delete_matches_screenshot_history_semantics():
    assert _index_after_delete(1, 4) == 1  # 删中间：原位置不变
    assert _index_after_delete(2, 2) == 1  # 删末尾：回退前一行
    assert _index_after_delete(0, 0) == -1  # 删到空库
    assert _index_after_delete(0, 3) == 0


# ---------------------------------------------------------------- 表格模型


def _record(
    song_id: int,
    song_name: str = "Song",
    artist: str = "Artist",
    game_scene: str | None = None,
    remark: str | None = None,
):
    return (song_id, song_name, artist, game_scene, remark)


class _StubStore:
    """记录 update_meta 调用的 stub（可注入成功/失败）。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[int, str | None, str | None]] = []

    def update_meta(self, song_id, game_scene, remark):
        self.calls.append((song_id, game_scene, remark))
        return self.ok


def _model_with(*records, store=None):
    model = _SongTableModel(store)
    model.set_records(list(records))
    return model


def test_model_shape_and_headers():
    model = _model_with(_record(1), _record(2))
    assert model.rowCount() == 2
    assert model.columnCount() == 4
    for column, title in enumerate(HEADERS):
        assert (
            model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            == title
        )


def test_model_list_loading_order_and_values():
    model = _model_with(
        _record(1, "Song One", "Artist A"),
        _record(2, "Song Two", "Artist B"),
    )
    first = [model.data(model.index(0, c)) for c in range(4)]
    second = [model.data(model.index(1, c)) for c in range(4)]
    assert first == ["Song One", "Artist A", "", ""]
    assert second == ["Song Two", "Artist B", "", ""]
    assert model.song_id_at(0) == 1 and model.song_id_at(1) == 2
    assert model.song_id_at(99) is None


def test_model_blank_display_for_optional_fields():
    """game_scene/remark 为 NULL 时显示空串（需求：留空）。"""
    model = _model_with(_record(1, game_scene="战斗", remark=None))
    assert model.data(model.index(0, 2)) == "战斗"
    assert model.data(model.index(0, 3)) == ""


def test_model_flags_only_last_two_columns_editable():
    model = _model_with(_record(1))
    for column in range(4):
        flags = model.flags(model.index(0, column))
        editable = bool(flags & Qt.ItemFlag.ItemIsEditable)
        assert editable == (column in EDITABLE_COLUMNS)


def test_model_set_data_writes_via_store_and_updates_cache():
    store = _StubStore()
    model = _model_with(_record(7, game_scene=None, remark="旧备注"), store=store)
    assert model.setData(model.index(0, 2), "城镇") is True
    assert store.calls == [(7, "城镇", "旧备注")]  # 未编辑列保持原值
    assert model.data(model.index(0, 2)) == "城镇"
    assert model.setData(model.index(0, 3), "新备注") is True
    assert store.calls[-1] == (7, "城镇", "新备注")


def test_model_set_data_readonly_columns_rejected():
    store = _StubStore()
    model = _model_with(_record(1), store=store)
    assert model.setData(model.index(0, 0), "改曲名") is False
    assert model.setData(model.index(0, 1), "改艺术家") is False
    assert store.calls == []


def test_model_set_data_store_failure_keeps_cache():
    store = _StubStore(ok=False)
    model = _model_with(_record(1, remark=None), store=store)
    assert model.setData(model.index(0, 3), "备注") is False
    assert model.data(model.index(0, 3)) == ""  # 写库失败不更新缓存


def test_model_query_highlights_matching_rows_only():
    model = _model_with(_record(1, "Alpha"), _record(2, "Beta"), _record(3, "alpha beta"))
    assert all(
        model.data(model.index(r, 0), Qt.ItemDataRole.BackgroundRole) is None for r in range(3)
    )
    model.set_query("ALPHA")
    assert model.data(model.index(0, 0), Qt.ItemDataRole.BackgroundRole) == MATCH_COLOR
    assert model.data(model.index(1, 0), Qt.ItemDataRole.BackgroundRole) is None
    # "alpha beta" 整格不等于查询词：精确匹配下不高亮（与 next_match 语义一致）
    assert model.data(model.index(2, 0), Qt.ItemDataRole.BackgroundRole) is None
    model.set_query("")  # 清空查询词清除高亮
    assert model.data(model.index(0, 0), Qt.ItemDataRole.BackgroundRole) is None


def test_model_refresh_via_set_records():
    """刷新语义：set_records 整体重建行缓存（窗口每次打开/删除后调用）。"""
    model = _SongTableModel(None)
    assert model.rowCount() == 0  # 空库降级：无记录
    model.set_records([_record(1), _record(2), _record(3)])
    assert model.rowCount() == 3
    model.set_records([_record(9)])
    assert model.rowCount() == 1
    assert model.song_id_at(0) == 9


def test_model_empty_store_defaults():
    model = _SongTableModel(None)
    assert model.rowCount() == 0
    assert model.row_texts() == []
    assert next_match(model.row_texts(), "x", -1, forward=True) == -1


# ---------------------------------------------------------------- store 读写


def test_store_entries_delete_update_roundtrip(tmp_path):
    store = open_store(tmp_path)
    try:
        store.insert(artist="Artist A", song_name="Song One")
        store.insert(artist="Artist B", song_name="Song Two")
        assert store.entries() == [
            (1, "Song One", "Artist A", None, None),
            (2, "Song Two", "Artist B", None, None),
        ]
        # 编辑第三、四列（需求：回车写入数据库），曲名/艺术家不受影响
        assert store.update_meta(1, game_scene="战斗", remark="Boss 战 BGM") is True
        assert store.entries()[0] == (1, "Song One", "Artist A", "战斗", "Boss 战 BGM")
        # 删除选中行（需求：同时删除数据库记录）
        assert store.delete(2) is True
        assert [row[0] for row in store.entries()] == [1]
        assert store.delete(999) is False  # 未命中
        assert store.update_meta(999, game_scene="x", remark="y") is False
    finally:
        store.close()


def test_store_entries_empty_database(tmp_path):
    store = open_store(tmp_path)
    try:
        assert store.entries() == []
    finally:
        store.close()
