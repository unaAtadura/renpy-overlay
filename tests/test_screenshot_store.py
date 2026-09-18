"""截图数据库的离线验证：建库、插入、时间戳范围、升序列表与降级。"""

from __future__ import annotations

from renpy_overlay.screenshot.store import open_store


def _insert(store, ts: int, text: str = "译文") -> int:
    record_id = store.insert(
        ts=ts,
        ocr_text=text,
        img_original=b"original-" + str(ts).encode(),
        img_thumbnail=b"thumb-" + str(ts).encode(),
    )
    assert record_id is not None
    return record_id


def test_insert_and_query_roundtrip(tmp_path):
    store = open_store(str(tmp_path))
    first = _insert(store, 1000, "第一条")
    second = _insert(store, 2000, "第二条")
    assert first == 1 and second == 2  # 自增 id
    assert store.count() == 2
    assert store.first_last_ts() == (1000, 2000)
    assert [ts for _id, ts in store.entries()] == [1000, 2000]  # 按 ts 升序
    text, original = store.record(first)
    assert text == "第一条"
    assert original == b"original-1000"
    assert store.thumbnail(first) == b"thumb-1000"
    store.close()


def test_missing_record_returns_none(tmp_path):
    store = open_store(str(tmp_path))
    assert store.record(999) is None
    assert store.thumbnail(999) is None
    store.close()


def test_empty_database_returns_none_and_empty(tmp_path):
    store = open_store(str(tmp_path))
    assert store.first_last_ts() is None
    assert store.entries() == []
    assert store.count() == 0
    store.close()


def test_empty_game_dir_degrades_to_none():
    assert open_store("") is None  # 未提供游戏目录：不入库（与 translation_store 一致）


def test_database_persists_across_connections(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1234567890123, "持久化")
    store.close()
    reopened = open_store(str(tmp_path))
    assert reopened.count() == 1
    text, _original = reopened.record(1)
    assert text == "持久化"
    reopened.close()


def test_delete_removes_only_target_record(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1000, "第一条")
    second = _insert(store, 2000, "第二条")
    third = _insert(store, 3000, "第三条")
    assert store.delete(second) is True
    assert store.count() == 2
    assert store.record(second) is None  # 被删记录不可再读
    assert [ts for _id, ts in store.entries()] == [1000, 3000]  # 其余记录保留
    assert store.record(third)[0] == "第三条"
    store.close()


def test_delete_missing_id_returns_false(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1000, "唯一一条")
    assert store.delete(999) is False  # id 不存在：未命中
    assert store.count() == 1
    store.close()


def test_delete_persists_after_reopen(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1000, "保留")
    victim = _insert(store, 2000, "将被删除")
    store.close()
    reopened = open_store(str(tmp_path))
    assert reopened.delete(victim) is True
    reopened.close()
    final = open_store(str(tmp_path))
    assert final.count() == 1
    assert final.record(victim) is None
    final.close()


def test_schema_matches_specification(tmp_path):
    import sqlite3

    store = open_store(str(tmp_path))
    store.close()
    connection = sqlite3.connect(tmp_path / "renpy_overlay_cache" / "screenshot.db")
    columns = {
        row[1]: row[2] for row in connection.execute("PRAGMA table_info(game_screenshot)")
    }
    connection.close()
    assert columns == {
        "id": "INTEGER",
        "ts": "BIGINT",
        "ocr_text": "TEXT",
        "img_original": "BLOB",
        "img_thumbnail": "BLOB",
    }
