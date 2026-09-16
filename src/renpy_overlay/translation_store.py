"""翻译记录的 SQLite 持久化：在游戏根目录下增量保存「原文 → 译文」。

位置：被注入游戏可执行文件所在目录下的 ``renpy_overlay_cache/`` 子目录，
数据库文件 ``translations.db``。以原文哈希为键、**（哈希, 原文）为复合主键**
（与内存缓存 ``translation_cache`` 的分桶语义一致：同一哈希下多条不同原文的
记录共存），只做增量追加 / 覆盖，不重建、不清空全表 —— 即使内存缓存因 FIFO
淘汰或进程重启而丢失记录，数据库中的历史翻译仍然保留。

旧版数据库（``hash`` 单主键结构）在打开时自动迁移为复合主键并保留原有数据。

线程约束：与内存缓存一致 —— 只在 Tk 主线程内读写；后台翻译线程只负责网络
请求，结果经队列回传主线程后再统一落库，避免并发访问。

降级策略：目录创建失败或数据库不可写（无权限、只读磁盘等）时记录日志并返回
``None``，调用方退化为“仅内存缓存”，翻译流程不受影响。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .translation_cache import HashFunc, hash_original

logger = logging.getLogger("renpy_overlay.translation_store")

CACHE_DIR_NAME = "renpy_overlay_cache"
DB_FILENAME = "translations.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    hash       TEXT NOT NULL,
    original   TEXT NOT NULL,
    translated TEXT NOT NULL,
    PRIMARY KEY (hash, original)
)
"""


def _ensure_schema(connection: sqlite3.Connection, db_path: Path) -> None:
    """建表；若检测到旧版 ``hash`` 单主键结构，则迁移为 ``(hash, original)``

    复合主键（用临时表重建，保留全部历史数据）。
    """
    connection.execute(_SCHEMA)
    info = connection.execute("PRAGMA table_info(translations)").fetchall()
    pk_columns = [row[1] for row in sorted((row for row in info if row[5]), key=lambda r: r[5])]
    if pk_columns == ["hash", "original"]:
        return
    if not pk_columns:  # pragma: no cover - IF NOT EXISTS 后不应出现
        return
    logger.info("检测到旧版翻译表结构（主键 %s），迁移为 (hash, original) 复合主键", pk_columns)
    with connection:
        connection.execute("ALTER TABLE translations RENAME TO translations_legacy")
        connection.execute(_SCHEMA)
        connection.execute(
            "INSERT OR REPLACE INTO translations (hash, original, translated) "
            "SELECT hash, original, translated FROM translations_legacy"
        )
        connection.execute("DROP TABLE translations_legacy")
    logger.info("翻译表迁移完成，历史记录已保留（%s）", db_path)


class TranslationStore:
    """一个已打开的翻译数据库（连接与实例同生命周期，仅主线程使用）。"""

    def __init__(self, connection: sqlite3.Connection, db_path: Path, hash_func: HashFunc):
        self._conn = connection
        self._db_path = db_path
        self._hash_func = hash_func
        self._broken = False  # 运行时写入失败后降级短路，避免反复报错刷屏

    @property
    def path(self) -> Path:
        return self._db_path

    def get(self, original: str) -> str | None:
        """按原文查询译文（哈希 + 原文精确匹配）；未命中返回 None。"""
        if not original or self._broken:
            return None
        key = self._hash_func(original)
        try:
            row = self._conn.execute(
                "SELECT translated FROM translations WHERE hash = ? AND original = ?",
                (key, original),
            ).fetchone()
        except sqlite3.Error as exc:
            self._degrade("查询", exc)
            return None
        if row is None:
            logger.debug("翻译数据库未命中：%s…", original[:24])
            return None
        logger.info("翻译数据库命中（原文 %d 字）", len(original))
        return str(row[0])

    def put(self, original: str, translated: str) -> bool:
        """增量写入 / 覆盖同一原文的译文，返回是否写入成功。

        同一哈希下不同原文的记录各不相同（不可覆盖彼此）；同一（哈希, 原文）
        则更新译文。
        """
        if not original or not translated or self._broken:
            return False
        key = self._hash_func(original)
        try:
            self._conn.execute(
                "INSERT INTO translations (hash, original, translated) VALUES (?, ?, ?) "
                "ON CONFLICT(hash, original) DO UPDATE SET translated = excluded.translated",
                (key, original, translated),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("写入", exc)
            return False
        logger.debug("翻译数据库已写入（原文 %d 字）：%s", len(original), self._db_path)
        return True

    def count(self) -> int:
        """表内记录数（诊断用）；查询失败返回 -1。"""
        if self._broken:
            return -1
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()
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
                "翻译数据库%s失败（%s），后续降级为仅内存缓存：%s", action, self._db_path, exc
            )
        else:  # pragma: no cover - 已降级后不应再进入（短路保护）
            logger.debug("翻译数据库%s继续失败（已降级）：%s", action, exc)


def open_store(game_dir, hash_func: HashFunc = hash_original) -> TranslationStore | None:
    """在 ``game_dir/renpy_overlay_cache/translations.db`` 打开（必要时创建）数据库。

    任何失败（未提供游戏目录、目录不可建、数据库不可写）都记日志并返回 None，
    调用方降级为仅内存缓存。
    """
    if not game_dir:
        logger.info("未提供游戏目录，翻译记录仅保存在内存缓存中")
        return None
    cache_dir = Path(game_dir) / CACHE_DIR_NAME
    db_path = cache_dir / DB_FILENAME
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        _ensure_schema(connection, db_path)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("初始化翻译数据库失败（%s），降级为仅内存缓存：%s", db_path, exc)
        return None
    logger.info("翻译数据库已就绪：%s", db_path)
    return TranslationStore(connection, db_path, hash_func=hash_func)
