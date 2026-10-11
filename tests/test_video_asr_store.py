"""语音识别数据库（asr.db）的离线验证：建表、写入、读取、删除与降级。"""

from __future__ import annotations

import sqlite3

from renpy_overlay.translation_store import CACHE_DIR_NAME
from renpy_overlay.video.asr_store import DB_FILENAME, open_store


def test_open_creates_dir_and_db_with_schema(tmp_path):
    store = open_store(tmp_path)
    try:
        assert store is not None
        db_path = tmp_path / CACHE_DIR_NAME / DB_FILENAME
        assert db_path.is_file(), "应在游戏目录下创建 renpy_overlay_cache/asr.db"
        assert store.path == db_path
    finally:
        store.close()


def test_schema_matches_specification(tmp_path):
    """表结构与需求文档一致（自增主键 / blob 音频 / text 文本 / INTEGER 时长）。"""
    store = open_store(tmp_path)
    store.close()
    connection = sqlite3.connect(tmp_path / CACHE_DIR_NAME / DB_FILENAME)
    info = connection.execute("PRAGMA table_info(asr)").fetchall()
    connection.close()
    columns = {row[1]: row[2] for row in info}
    notnull = {row[1]: bool(row[3]) for row in info}
    pk = {row[1]: row[5] for row in info if row[5]}
    assert columns == {
        "asr_id": "INTEGER",
        "audio": "BLOB",
        "text": "TEXT",
        "duration": "INTEGER",
    }
    assert notnull == {
        "asr_id": False,
        "audio": True,
        "text": True,
        "duration": True,
    }
    assert pk == {"asr_id": 1}, "asr_id 应为自增主键"


def test_insert_and_entries_roundtrip(tmp_path):
    store = open_store(tmp_path)
    try:
        first = store.insert(b"wav-bytes-1", "你好世界", 4)
        second = store.insert(b"wav-bytes-2", "再会", 2)
        assert first == 1 and second == 2  # 自增 asr_id
        assert store.count() == 2
        # 列表不拉取 blob：只含 (asr_id, duration, text)
        assert store.entries() == [(1, 4, "你好世界"), (2, 2, "再会")]
        # 音频按需单条读取
        assert store.audio(1) == b"wav-bytes-1"
        assert store.audio(2) == b"wav-bytes-2"
        assert store.audio(999) is None
    finally:
        store.close()


def test_insert_rejects_empty_audio_or_text(tmp_path):
    store = open_store(tmp_path)
    try:
        assert store.insert(b"", "文本", 1) is None
        assert store.insert(b"wav", "", 1) is None
        assert store.count() == 0
    finally:
        store.close()


def test_empty_game_dir_degrades_to_none():
    assert open_store(None) is None
    assert open_store("") is None


def test_open_degrades_when_dir_uncreatable(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")  # 文件占位 → mkdir 失败
    assert open_store(blocker) is None, "目录创建失败应降级为不入库"


def test_database_persists_across_connections(tmp_path):
    store = open_store(tmp_path)
    store.insert(b"wav", "你好", 3)
    store.close()
    reopened = open_store(tmp_path)
    try:
        assert reopened.count() == 1, "重开后历史识别记录应保留"
    finally:
        reopened.close()


def test_delete_removes_row_and_reports_miss(tmp_path):
    store = open_store(tmp_path)
    try:
        asr_id = store.insert(b"wav", "你好", 3)
        assert store.delete(asr_id) is True
        assert store.delete(asr_id) is False  # 已不存在
        assert store.entries() == []
    finally:
        store.close()


def test_operations_degrade_after_write_failure(tmp_path):
    """失败后短路降级：后续操作不再尝试（不刷屏）。"""
    store = open_store(tmp_path)
    try:
        store._conn.close()  # 模拟数据库不可写
        assert store.insert(b"wav", "你好", 3) is None
        assert store._broken is True
        assert store.entries() == []
        assert store.audio(1) is None
        assert store.delete(1) is False
        assert store.count() == -1
    finally:
        store.close()


def test_close_is_idempotent(tmp_path):
    store = open_store(tmp_path)
    store.close()
    store.close()  # 重复关闭不应抛异常
