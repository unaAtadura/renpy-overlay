"""识曲数据库（game_song.db）的离线验证：建表、成功写入、可选字段留空与降级。"""

from __future__ import annotations

import sqlite3

from renpy_overlay.song_recognition.store import DB_FILENAME, open_store
from renpy_overlay.translation_store import CACHE_DIR_NAME


def test_open_creates_dir_and_db_with_schema(tmp_path):
    store = open_store(tmp_path)
    try:
        assert store is not None
        db_path = tmp_path / CACHE_DIR_NAME / DB_FILENAME
        assert db_path.is_file(), "应在游戏目录下创建 renpy_overlay_cache/game_song.db"
        assert store.path == db_path
    finally:
        store.close()


def test_schema_matches_specification(tmp_path):
    """表结构与需求文档建表语句一致（列名/类型/NOT NULL 约束）。"""
    store = open_store(tmp_path)
    store.close()
    connection = sqlite3.connect(tmp_path / CACHE_DIR_NAME / DB_FILENAME)
    info = connection.execute("PRAGMA table_info(music_master)").fetchall()
    connection.close()
    columns = {row[1]: row[2] for row in info}
    notnull = {row[1]: bool(row[3]) for row in info}
    pk = {row[1]: row[5] for row in info if row[5]}
    assert columns == {
        "song_id": "INTEGER",
        "artist": "TEXT",
        "song_name": "TEXT",
        "game_scene": "TEXT",
        "remark": "TEXT",
    }
    assert notnull == {
        "song_id": False,
        "artist": True,
        "song_name": True,
        "game_scene": False,
        "remark": False,
    }
    assert pk == {"song_id": 1}, "song_id 应为自增主键"


def test_insert_success_roundtrip(tmp_path):
    store = open_store(tmp_path)
    try:
        first = store.insert(artist="Artist A", song_name="Song One")
        second = store.insert(artist="Artist B", song_name="Song Two")
        assert first == 1 and second == 2  # 自增 song_id
        assert store.count() == 2
        connection = sqlite3.connect(tmp_path / CACHE_DIR_NAME / DB_FILENAME)
        rows = connection.execute(
            "SELECT song_id, artist, song_name FROM music_master ORDER BY song_id"
        ).fetchall()
        connection.close()
        assert rows == [(1, "Artist A", "Song One"), (2, "Artist B", "Song Two")]
    finally:
        store.close()


def test_insert_leaves_optional_fields_null(tmp_path):
    """game_scene / remark 不填写：落库为 NULL（需求：留空即可）。"""
    store = open_store(tmp_path)
    try:
        assert store.insert(artist="Artist A", song_name="Song One") == 1
        connection = sqlite3.connect(tmp_path / CACHE_DIR_NAME / DB_FILENAME)
        row = connection.execute(
            "SELECT game_scene, remark FROM music_master WHERE song_id = 1"
        ).fetchone()
        connection.close()
        assert row == (None, None)
    finally:
        store.close()


def test_insert_rejects_empty_artist_or_song_name(tmp_path):
    """artist / song_name 为空时不写入（表约束 NOT NULL，空串同样拒绝）。"""
    store = open_store(tmp_path)
    try:
        assert store.insert(artist="", song_name="Song One") is None
        assert store.insert(artist="Artist A", song_name="") is None
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
    store.insert(artist="Artist A", song_name="Song One")
    store.close()
    reopened = open_store(tmp_path)
    try:
        assert reopened.count() == 1, "重开后历史识曲记录应保留"
    finally:
        reopened.close()


def test_insert_degrades_after_write_failure(tmp_path):
    """写入失败后短路降级：返回 None 且后续写入不再尝试（不刷屏）。"""
    store = open_store(tmp_path)
    try:
        store._conn.close()  # 模拟数据库不可写
        assert store.insert(artist="Artist A", song_name="Song One") is None
        assert store._broken is True
        assert store.insert(artist="Artist B", song_name="Song Two") is None
        assert store.count() == -1
    finally:
        store.close()


def test_close_is_idempotent(tmp_path):
    store = open_store(tmp_path)
    store.close()
    store.close()  # 重复关闭不应抛异常
