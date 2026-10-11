"""语音识别数据库（asr.db）：识别成功后音频片段与文本的持久化。

与 :mod:`renpy_overlay.song_recognition.store` 同一套模式：目录创建失败或
数据库不可写时记录日志并返回 ``None``（调用方降级为"只上屏不入库"）；
运行时写入失败后 ``_degrade`` 短路，避免反复报错刷屏。

线程约束：与识曲存储一致 —— 后台线程只做识别，成功结果经队列回传主线程
后统一落库（``StreamOverlayWindow`` 的 asr_done 事件处理）；语音识别历史
窗口的读取 / 删除 / 音频读取也全部在 Qt 主线程进行。

表结构（需求给定 .raw_plans/future_视频录制.txt：自增主键 / blob 音频 /
text 文本 / INTEGER 时长）::

    CREATE TABLE asr (
        asr_id INTEGER PRIMARY KEY AUTOINCREMENT,
        audio BLOB NOT NULL,    -- 截取的 wav 音频片段（16k 单声道）
        text TEXT NOT NULL,     -- 语音识别文本
        duration INTEGER NOT NULL  -- 音频时长（秒）
    );

``entries()`` 不拉取 blob（列表只需时长与文本，音频按需经 :meth:`AsrStore.audio`
单条读取，避免全表搬运大字段）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.video.asr_store")

DB_FILENAME = "asr.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS asr (
    asr_id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio BLOB NOT NULL,
    text TEXT NOT NULL,
    duration INTEGER NOT NULL
)
"""


class AsrStore:
    """一个已打开的语音识别数据库（连接与实例同生命周期，仅主线程使用）。"""

    def __init__(self, connection: sqlite3.Connection, db_path: Path):
        self._conn = connection
        self._db_path = db_path
        self._broken = False  # 运行时写入失败后降级短路，避免反复报错刷屏

    @property
    def path(self) -> Path:
        return self._db_path

    def insert(self, audio: bytes, text: str, duration: int) -> int | None:
        """写入一条成功识别结果，返回自增 asr_id；失败返回 None（已降级则直接 None）。"""
        if not audio or not text or self._broken:
            return None
        try:
            cursor = self._conn.execute(
                "INSERT INTO asr (audio, text, duration) VALUES (?, ?, ?)",
                (sqlite3.Binary(audio), text, int(duration)),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("写入", exc)
            return None
        logger.debug(
            "语音识别记录已写入（asr_id=%s，%d 字节，%ds）：%s",
            cursor.lastrowid,
            len(audio),
            duration,
            self._db_path,
        )
        return int(cursor.lastrowid)

    def entries(self) -> list[tuple[int, int, str]]:
        """全部记录 ``(asr_id, duration, text)``，按写入顺序（asr_id 升序）。

        不拉取 audio blob（大字段按需经 :meth:`audio` 读取）；失败返回空列表。
        """
        if self._broken:
            return []
        try:
            rows = self._conn.execute(
                "SELECT asr_id, duration, text FROM asr ORDER BY asr_id ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            self._degrade("查询记录列表", exc)
            return []
        return [(int(row[0]), int(row[1]), str(row[2])) for row in rows]

    def audio(self, asr_id: int) -> bytes | None:
        """按 id 读取音频 blob（历史窗口播放用）；不存在或失败返回 ``None``。"""
        if self._broken:
            return None
        try:
            row = self._conn.execute(
                "SELECT audio FROM asr WHERE asr_id = ?", (int(asr_id),)
            ).fetchone()
        except sqlite3.Error as exc:
            self._degrade("读取音频", exc)
            return None
        if row is None:
            return None
        return bytes(row[0])

    def delete(self, asr_id: int) -> bool:
        """按 asr_id 删除一条记录，返回是否删除成功（id 不存在返回 False）。"""
        if self._broken:
            return False
        try:
            cursor = self._conn.execute(
                "DELETE FROM asr WHERE asr_id = ?", (int(asr_id),)
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("删除", exc)
            return False
        if cursor.rowcount == 0:
            logger.debug("删除语音识别记录未命中（asr_id=%s）：%s", asr_id, self._db_path)
            return False
        logger.debug("语音识别记录已删除（asr_id=%s）：%s", asr_id, self._db_path)
        return True

    def count(self) -> int:
        """表内记录数（诊断用）；查询失败返回 -1。"""
        if self._broken:
            return -1
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM asr").fetchone()
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
            logger.error("语音识别数据库%s失败（%s），后续降级为不入库：%s", action, self._db_path, exc)
        else:  # pragma: no cover - 已降级后不应再进入（短路保护）
            logger.debug("语音识别数据库%s继续失败（已降级）：%s", action, exc)


def open_store(game_dir) -> AsrStore | None:
    """在 ``game_dir/renpy_overlay_cache/asr.db`` 打开（必要时创建）数据库。

    任何失败（未提供游戏目录、目录不可建、数据库不可写）都记日志并返回 None，
    调用方降级为"识别结果只上屏不入库"。
    """
    if not game_dir:
        logger.info("未提供游戏目录，语音识别结果不入库")
        return None
    cache_dir = Path(game_dir) / CACHE_DIR_NAME
    db_path = cache_dir / DB_FILENAME
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.execute(_SCHEMA)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("初始化语音识别数据库失败（%s），降级为不入库：%s", db_path, exc)
        return None
    logger.info("语音识别数据库已就绪：%s", db_path)
    return AsrStore(connection, db_path)
