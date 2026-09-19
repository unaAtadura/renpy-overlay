"""听歌识曲成功结果的 SQLite 持久化：游戏根目录 ``renpy_overlay_cache/game_song.db``。

与 :mod:`renpy_overlay.translation_store` / :mod:`renpy_overlay.screenshot.store`
同一套模式：目录创建失败或数据库不可写时记录日志并返回 ``None``（调用方降级为
"只显示不入库"，识曲流程不受影响）；运行时写入失败后 ``_degrade`` 短路，避免
反复报错刷屏。

线程约束：与翻译缓存一致 —— 后台线程只做录制与识别，成功结果经队列回传主线程
后统一落库（``StreamOverlayWindow._handle_song_done``）；识曲历史窗口的
读取 / 删除 / 备注编辑也全部在 Qt 主线程进行。

表结构（需求给定 .raw_plans/future_听歌识曲存储.txt，识曲成功时 ``game_scene``/
``remark`` 不填写）::

    CREATE TABLE music_master (
        song_id INTEGER PRIMARY KEY AUTOINCREMENT,
        artist TEXT NOT NULL,   -- 艺术家名
        song_name TEXT NOT NULL,-- 歌曲名
        game_scene TEXT,        -- 游戏场景：战斗/城镇/过场/菜单等
        remark TEXT             -- 备注：版本、变奏、来源说明
    );
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.song_recognition.store")

DB_FILENAME = "game_song.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS music_master (
    song_id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    song_name TEXT NOT NULL,
    game_scene TEXT,
    remark TEXT
)
"""


class SongStore:
    """一个已打开的识曲数据库（连接与实例同生命周期，仅主线程使用）。"""

    def __init__(self, connection: sqlite3.Connection, db_path: Path):
        self._conn = connection
        self._db_path = db_path
        self._broken = False  # 运行时写入失败后降级短路，避免反复报错刷屏

    @property
    def path(self) -> Path:
        return self._db_path

    def insert(
        self,
        artist: str,
        song_name: str,
        game_scene: str | None = None,
        remark: str | None = None,
    ) -> int | None:
        """写入一条成功识别结果，返回自增 song_id；失败返回 None（已降级则直接 None）。

        ``game_scene`` / ``remark`` 默认不填写（NULL）。
        """
        if not artist or not song_name or self._broken:
            return None
        try:
            cursor = self._conn.execute(
                "INSERT INTO music_master (artist, song_name, game_scene, remark) "
                "VALUES (?, ?, ?, ?)",
                (artist, song_name, game_scene, remark),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("写入", exc)
            return None
        logger.debug(
            "识曲记录已写入（song_id=%s，%s - %s）：%s",
            cursor.lastrowid,
            artist,
            song_name,
            self._db_path,
        )
        return int(cursor.lastrowid)

    def entries(self) -> list[tuple[int, str, str, str | None, str | None]]:
        """全部识曲记录 ``(song_id, song_name, artist, game_scene, remark)``，

        按写入顺序（song_id 升序）；失败返回空列表。
        """
        if self._broken:
            return []
        try:
            rows = self._conn.execute(
                "SELECT song_id, song_name, artist, game_scene, remark "
                "FROM music_master ORDER BY song_id ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            self._degrade("查询记录列表", exc)
            return []
        return [(int(row[0]), str(row[1]), str(row[2]), row[3], row[4]) for row in rows]

    def delete(self, song_id: int) -> bool:
        """按 song_id 删除一条识曲记录，返回是否删除成功（id 不存在返回 False）。"""
        if self._broken:
            return False
        try:
            cursor = self._conn.execute(
                "DELETE FROM music_master WHERE song_id = ?", (int(song_id),)
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("删除", exc)
            return False
        if cursor.rowcount == 0:
            logger.debug("删除识曲记录未命中（song_id=%s）：%s", song_id, self._db_path)
            return False
        logger.debug("识曲记录已删除（song_id=%s）：%s", song_id, self._db_path)
        return True

    def update_meta(self, song_id: int, game_scene: str | None, remark: str | None) -> bool:
        """更新一条记录的游戏场景与备注（识曲历史窗口的第三、四列编辑），

        返回是否更新成功（id 不存在或失败返回 False）。
        """
        if self._broken:
            return False
        try:
            cursor = self._conn.execute(
                "UPDATE music_master SET game_scene = ?, remark = ? WHERE song_id = ?",
                (game_scene, remark, int(song_id)),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("更新备注", exc)
            return False
        if cursor.rowcount == 0:
            logger.debug("更新识曲记录未命中（song_id=%s）：%s", song_id, self._db_path)
            return False
        logger.debug("识曲记录备注已更新（song_id=%s）：%s", song_id, self._db_path)
        return True

    def count(self) -> int:
        """表内记录数（诊断用）；查询失败返回 -1。"""
        if self._broken:
            return -1
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM music_master").fetchone()
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
            logger.error("识曲数据库%s失败（%s），后续降级为不入库：%s", action, self._db_path, exc)
        else:  # pragma: no cover - 已降级后不应再进入（短路保护）
            logger.debug("识曲数据库%s继续失败（已降级）：%s", action, exc)


def open_store(game_dir) -> SongStore | None:
    """在 ``game_dir/renpy_overlay_cache/game_song.db`` 打开（必要时创建）数据库。

    任何失败（未提供游戏目录、目录不可建、数据库不可写）都记日志并返回 None，
    调用方降级为"识曲结果只显示不入库"。
    """
    if not game_dir:
        logger.info("未提供游戏目录，识曲结果不入库")
        return None
    cache_dir = Path(game_dir) / CACHE_DIR_NAME
    db_path = cache_dir / DB_FILENAME
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.execute(_SCHEMA)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("初始化识曲数据库失败（%s），降级为不入库：%s", db_path, exc)
        return None
    logger.info("识曲数据库已就绪：%s", db_path)
    return SongStore(connection, db_path)
