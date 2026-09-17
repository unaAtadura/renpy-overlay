"""截图记录的 SQLite 持久化：游戏根目录 ``renpy_overlay_cache/screenshot.db``。

与 :mod:`renpy_overlay.translation_store` 同一套模式：目录创建失败或数据库
不可写时记录日志并返回 ``None``（调用方降级为"仅显示不入库"，翻译流程不受
影响）；运行时写入失败后 ``_degrade`` 短路，避免反复报错刷屏。

线程约束：与翻译缓存一致 —— 只在 Qt 主线程读写；后台线程只做图像处理与
网络请求，编码好的 bytes 经队列回传主线程后统一落库。

表结构（需求给定，``ts`` 为 13 位毫秒时间戳）::

    CREATE TABLE game_screenshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts BIGINT NOT NULL,
        ocr_text TEXT,
        img_original BLOB,
        img_thumbnail BLOB
    );
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.screenshot.store")

DB_FILENAME = "screenshot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_screenshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts BIGINT NOT NULL,
    ocr_text TEXT,
    img_original BLOB,
    img_thumbnail BLOB
)
"""


class ScreenshotStore:
    """一个已打开的截图数据库（连接与实例同生命周期，仅主线程使用）。"""

    def __init__(self, connection: sqlite3.Connection, db_path: Path):
        self._conn = connection
        self._db_path = db_path
        self._broken = False  # 运行时读写失败后降级短路，避免反复报错刷屏

    @property
    def path(self) -> Path:
        return self._db_path

    def insert(
        self, ts: int, ocr_text: str, img_original: bytes, img_thumbnail: bytes
    ) -> int | None:
        """写入一条截图记录，返回自增 id；失败返回 None（已降级则直接 None）。"""
        if self._broken:
            return None
        try:
            cursor = self._conn.execute(
                "INSERT INTO game_screenshot (ts, ocr_text, img_original, img_thumbnail) "
                "VALUES (?, ?, ?, ?)",
                (int(ts), ocr_text, img_original, img_thumbnail),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("写入", exc)
            return None
        logger.debug("截图记录已写入（id=%s，译文 %d 字）：%s", cursor.lastrowid, len(ocr_text), self._db_path)
        return int(cursor.lastrowid)

    def first_last_ts(self) -> tuple[int, int] | None:
        """首条与末条记录的毫秒时间戳（升序）；空库或失败返回 None。"""
        if self._broken:
            return None
        try:
            row = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM game_screenshot"
            ).fetchone()
        except sqlite3.Error as exc:
            self._degrade("查询首末时间", exc)
            return None
        if row is None or row[0] is None:
            return None
        return (int(row[0]), int(row[1]))

    def entries(self) -> list[tuple[int, int]]:
        """全部记录的 ``(id, ts)``，按时间升序（缩略图条带的浏览顺序）。"""
        if self._broken:
            return []
        try:
            rows = self._conn.execute(
                "SELECT id, ts FROM game_screenshot ORDER BY ts ASC, id ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            self._degrade("查询记录列表", exc)
            return []
        return [(int(row[0]), int(row[1])) for row in rows]

    def thumbnail(self, record_id: int) -> bytes | None:
        """按 id 读取缩略图 BLOB；不存在或失败返回 None。"""
        return self._read_blob("img_thumbnail", record_id)

    def record(self, record_id: int) -> tuple[str, bytes] | None:
        """按 id 读取 ``(译文, 原图 JPEG bytes)``；不存在或失败返回 None。"""
        if self._broken:
            return None
        try:
            row = self._conn.execute(
                "SELECT ocr_text, img_original FROM game_screenshot WHERE id = ?",
                (int(record_id),),
            ).fetchone()
        except sqlite3.Error as exc:
            self._degrade("读取记录", exc)
            return None
        if row is None:
            return None
        return (str(row[0] or ""), bytes(row[1] or b""))

    def count(self) -> int:
        """表内记录数（诊断用）；查询失败返回 -1。"""
        if self._broken:
            return -1
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM game_screenshot").fetchone()
        except sqlite3.Error as exc:
            self._degrade("统计", exc)
            return -1
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - 关闭失败不影响退出
            pass

    def _read_blob(self, column: str, record_id: int) -> bytes | None:
        if self._broken:
            return None
        try:
            row = self._conn.execute(
                f"SELECT {column} FROM game_screenshot WHERE id = ?",  # noqa: S608 - 列名内部常量
                (int(record_id),),
            ).fetchone()
        except sqlite3.Error as exc:
            self._degrade(f"读取 {column}", exc)
            return None
        if row is None or row[0] is None:
            return None
        return bytes(row[0])

    def _degrade(self, action: str, exc: Exception) -> None:
        if not self._broken:
            self._broken = True
            logger.error(
                "截图数据库%s失败（%s），后续降级为不入库：%s", action, self._db_path, exc
            )
        else:  # pragma: no cover - 已降级后不应再进入（短路保护）
            logger.debug("截图数据库%s继续失败（已降级）：%s", action, exc)


def open_store(game_dir) -> ScreenshotStore | None:
    """在 ``game_dir/renpy_overlay_cache/screenshot.db`` 打开（必要时创建）数据库。

    任何失败（未提供游戏目录、目录不可建、数据库不可写）都记日志并返回 None，
    调用方降级为"译文只显示不入库"。
    """
    if not game_dir:
        logger.info("未提供游戏目录，截图记录不入库")
        return None
    cache_dir = Path(game_dir) / CACHE_DIR_NAME
    db_path = cache_dir / DB_FILENAME
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.execute(_SCHEMA)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("初始化截图数据库失败（%s），降级为不入库：%s", db_path, exc)
        return None
    logger.info("截图数据库已就绪：%s", db_path)
    return ScreenshotStore(connection, db_path)
