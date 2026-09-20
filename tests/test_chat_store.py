"""对话数据库的离线验证：建库、成对写入、自增 id、计数与降级。"""

from __future__ import annotations

from renpy_overlay.screenshot.chat_store import open_store


def test_insert_pairs_and_autoincrement_id(tmp_path):
    store = open_store(str(tmp_path))
    first = store.insert("这句话什么意思？", "它的意思是……")
    second = store.insert("第二句怎么理解？", "可以这样理解……")
    assert first == 1 and second == 2  # 自增 id
    assert store.count() == 2
    store.close()


def test_empty_game_dir_degrades_to_none():
    assert open_store("") is None  # 未提供游戏目录：不入库（与其他 store 一致）


def test_database_persists_across_connections(tmp_path):
    store = open_store(str(tmp_path))
    assert store.insert("用户消息", "AI 回复") == 1
    store.close()
    reopened = open_store(str(tmp_path))
    assert reopened.count() == 1  # 重开连接后记录仍在
    reopened.close()


def test_schema_matches_specification(tmp_path):
    import sqlite3

    store = open_store(str(tmp_path))
    store.close()
    connection = sqlite3.connect(tmp_path / "renpy_overlay_cache" / "chat.db")
    columns = {
        row[1]: (row[2], row[3]) for row in connection.execute("PRAGMA table_info(chat)")
    }
    connection.close()
    # 需求给定表结构：两个消息列均 NOT NULL，主键自增
    assert columns == {
        "id": ("INTEGER", 0),
        "user_msg": ("TEXT", 1),
        "ai_msg": ("TEXT", 1),
    }


def test_open_degrades_when_dir_uncreatable(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")  # 文件占位 → mkdir 失败
    assert open_store(blocker) is None, "目录创建失败应降级为不入库"


def test_insert_degrades_after_write_failure(tmp_path):
    """写入失败后短路降级：返回 None 且后续写入不再尝试（不刷屏）。"""
    store = open_store(str(tmp_path))
    try:
        store._conn.close()  # 模拟数据库不可写
        assert store.insert("用户消息", "AI 回复") is None
        assert store._broken is True
        assert store.insert("第二条", "回复二") is None
        assert store.count() == -1
    finally:
        store.close()
