"""AI 对话记录的 SQLite 持久化：游戏根目录 ``renpy_overlay_cache/chat.db``。

与 :mod:`renpy_overlay.screenshot.store`、:mod:`renpy_overlay.translation_store`
同一套模式：目录创建失败或数据库不可写时记录日志并返回 ``None``（调用方降级
为"仅显示不入库"，对话流程不受影响）；运行时写入失败后 ``_degrade`` 短路，
避免反复报错刷屏。

线程约束：与截图记录一致 —— 只在 Qt 主线程读写；后台线程只做 API 请求，
完整回复经队列回传主线程后统一落库。

表结构（需求给定，对话不保存上下文，仅成对存用户消息与 AI 回复）::

    CREATE TABLE IF NOT EXISTS chat (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_msg TEXT NOT NULL,
        ai_msg TEXT NOT NULL
    )
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.screenshot.chat_store")

DB_FILENAME = "chat.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_msg TEXT NOT NULL,
    ai_msg TEXT NOT NULL
)
"""


class ChatStore:
    """一个已打开的对话数据库（连接与实例同生命周期，仅主线程使用）。"""

    def __init__(self, connection: sqlite3.Connection, db_path: Path):
        self._conn = connection
        self._db_path = db_path
        self._broken = False  # 运行时读写失败后降级短路，避免反复报错刷屏

    @property
    def path(self) -> Path:
        return self._db_path

    def insert(self, user_msg: str, ai_msg: str) -> int | None:
        """成对写入一条对话记录，返回自增 id；失败返回 None（已降级则直接 None）。"""
        if self._broken:
            return None
        try:
            cursor = self._conn.execute(
                "INSERT INTO chat (user_msg, ai_msg) VALUES (?, ?)",
                (user_msg, ai_msg),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("写入", exc)
            return None
        logger.debug(
            "对话记录已写入（id=%s，用户消息 %d 字，回复 %d 字）：%s",
            cursor.lastrowid,
            len(user_msg),
            len(ai_msg),
            self._db_path,
        )
        return int(cursor.lastrowid)

    def entries(self) -> list[tuple[int, str, str]]:
        """全部记录的 ``(id, user_msg, ai_msg)``，按自增 id 升序（写入顺序，即表格浏览顺序）。"""
        if self._broken:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, user_msg, ai_msg FROM chat ORDER BY id ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            self._degrade("查询记录列表", exc)
            return []
        return [(int(row[0]), str(row[1] or ""), str(row[2] or "")) for row in rows]

    def delete(self, record_id: int) -> bool:
        """按 id 删除一条对话记录，返回是否删除成功（id 不存在返回 False）。"""
        if self._broken:
            return False
        try:
            cursor = self._conn.execute(
                "DELETE FROM chat WHERE id = ?", (int(record_id),)
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("删除", exc)
            return False
        if cursor.rowcount == 0:
            logger.debug("删除对话记录未命中（id=%s）：%s", record_id, self._db_path)
            return False
        logger.debug("对话记录已删除（id=%s）：%s", record_id, self._db_path)
        return True

    def count(self) -> int:
        """表内记录数（诊断用）；查询失败返回 -1。"""
        if self._broken:
            return -1
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM chat").fetchone()
        except sqlite3.Error as exc:
            self._degrade("统计", exc)
            return -1
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭失败不影响退出
            pass

    def _degrade(self, action: str, exc: Exception) -> None:
        if not self._broken:
            self._broken = True
            logger.error(
                "对话数据库%s失败（%s），后续降级为不入库：%s", action, self._db_path, exc
            )
        else:  # pragma: no cover - 已降级后不应再进入（短路保护）
            logger.debug("对话数据库%s继续失败（已降级）：%s", action, exc)


def open_store(game_dir) -> ChatStore | None:
    """在 ``game_dir/renpy_overlay_cache/chat.db`` 打开（必要时创建）数据库。

    与 screenshot.db、translations.db 同目录；任何失败（未提供游戏目录、
    目录不可建、数据库不可写）都记日志并返回 None，调用方降级为
    "回复只显示不入库"。
    """
    if not game_dir:
        logger.info("未提供游戏目录，对话记录不入库")
        return None
    cache_dir = Path(game_dir) / CACHE_DIR_NAME
    db_path = cache_dir / DB_FILENAME
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.execute(_SCHEMA)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("初始化对话数据库失败（%s），降级为不入库：%s", db_path, exc)
        return None
    logger.info("对话数据库已就绪：%s", db_path)
    return ChatStore(connection, db_path)
