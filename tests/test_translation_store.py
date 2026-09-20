"""翻译数据库（SQLite 持久化）的离线验证：增量写入、覆盖、命中与降级。"""

from __future__ import annotations

import sqlite3

from renpy_overlay.translation_cache import hash_original
from renpy_overlay.translation_store import CACHE_DIR_NAME, DB_FILENAME, open_store


def test_open_creates_dir_and_db(tmp_path):
    store = open_store(tmp_path)
    try:
        assert store is not None
        db_path = tmp_path / CACHE_DIR_NAME / DB_FILENAME
        assert db_path.is_file(), "应在游戏目录下创建 renpy_overlay_cache/translations.db"
        assert store.path == db_path
    finally:
        store.close()


def test_put_get_roundtrip_and_persistence(tmp_path):
    store = open_store(tmp_path)
    assert store.put("Hello", "你好") is True
    assert store.get("Hello") == "你好"
    assert store.get("未翻译过") is None
    store.close()
    # 重新打开（模拟进程重启）：历史记录仍在
    reopened = open_store(tmp_path)
    try:
        assert reopened.get("Hello") == "你好", "重启后数据库记录应保留"
    finally:
        reopened.close()


def test_put_overwrites_same_original(tmp_path):
    store = open_store(tmp_path)
    try:
        store.put("Hello", "你好")
        store.put("Hello", "您好")  # 覆盖同一原文
        assert store.get("Hello") == "您好"
        assert store.count() == 1, "覆盖不应产生新行"
    finally:
        store.close()


def test_incremental_rows_are_appended(tmp_path):
    store = open_store(tmp_path)
    try:
        store.put("A", "译A")
        store.put("B", "译B")
        assert store.get("A") == "译A"
        assert store.get("B") == "译B"
        assert store.count() == 2
    finally:
        store.close()


def test_same_hash_multiple_originals_coexist(tmp_path):
    # 注入固定哈希模拟碰撞：不同原文共存（复合主键 (hash, original)）
    store = open_store(tmp_path, hash_func=lambda _text: "same-key")
    try:
        assert store.put("原文A", "译文A") is True
        assert store.put("原文B", "译文B") is True
        assert store.get("原文A") == "译文A"
        assert store.get("原文B") == "译文B"
        assert store.count() == 2, "同哈希不同原文应各占一行"
        assert store.get("原文C") is None
    finally:
        store.close()


def test_legacy_single_pk_database_is_migrated(tmp_path):
    cache_dir = tmp_path / CACHE_DIR_NAME
    cache_dir.mkdir()
    db_path = cache_dir / DB_FILENAME
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE translations ("
        "hash TEXT PRIMARY KEY, original TEXT NOT NULL, translated TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO translations (hash, original, translated) VALUES (?, ?, ?)",
        (hash_original("Hello"), "Hello", "你好"),
    )
    conn.commit()
    conn.close()

    store = open_store(tmp_path)
    try:
        assert store is not None
        assert store.get("Hello") == "你好", "迁移后旧记录应保留"
        assert store.put("Hello", "您好") is True  # 新结构可正常写入
        assert store.get("Hello") == "您好"
        assert store.count() == 1
    finally:
        store.close()


def test_missing_game_dir_returns_none():
    assert open_store(None) is None
    assert open_store("") is None


def test_open_degrades_when_dir_uncreatable(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")  # 文件占位 → mkdir 失败
    assert open_store(blocker) is None, "目录创建失败应降级为仅内存缓存"


def test_close_is_idempotent(tmp_path):
    store = open_store(tmp_path)
    store.close()
    store.close()  # 重复关闭不应抛异常


# ---------------------------------------------------------------- 历史浏览（entries/delete）


def test_entries_lists_all_records_in_insertion_order(tmp_path):
    store = open_store(tmp_path)
    try:
        store.put("B 原文", "译B")
        store.put("A 原文", "译A")
        entries = store.entries()
        assert [row[1] for row in entries] == ["B 原文", "A 原文"]  # 写入顺序（rowid）
        assert [row[2] for row in entries] == ["译B", "译A"]
        assert all(row[0] for row in entries)  # 分桶哈希键非空
    finally:
        store.close()


def test_delete_removes_only_target_pair_and_keeps_bucket_siblings(tmp_path):
    # 分桶语义：仅删除选中的一组 (hash, original)，同桶内其它原文的记录保留
    store = open_store(tmp_path, hash_func=lambda _text: "same-key")
    try:
        store.put("原文A", "译文A")
        store.put("原文B", "译文B")
        assert store.delete("原文A") is True
        assert store.get("原文A") is None
        assert store.get("原文B") == "译文B", "同桶内其它原文的记录不受影响"
        assert [row[1] for row in store.entries()] == ["原文B"]
        assert store.delete("原文A") is False  # 未命中
        assert store.delete("") is False  # 空原文直接拒绝
    finally:
        store.close()


def test_delete_last_record_empties_the_bucket(tmp_path):
    # 桶空才删整个条目：删除桶内唯一记录后，不再有任何该桶的行
    store = open_store(tmp_path, hash_func=lambda _text: "solo-key")
    try:
        store.put("唯一原文", "唯一译文")
        assert store.delete("唯一原文") is True
        assert store.entries() == []
        assert store.count() == 0
    finally:
        store.close()
