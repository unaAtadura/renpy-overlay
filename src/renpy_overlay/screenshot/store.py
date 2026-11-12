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

第二张表 ``screenshot_signature_cache``（feat/remote-cache-persist）承载遥控
器自动化截图翻译的持久化缓存（512 字节感知签名 ↔ 译文）：命中判定为「零
超阈格」（``signatures_match_for_cache``），SQL 层用 8 个区均值列无损预筛；
命中只刷 ``updated_ts``（LRU 活性），不入 ``game_screenshot`` 历史；超限按
「冷端随机」水位淘汰（仅最旧一半候选内等距随机采样，新条目结构性免疫）。
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from pathlib import Path

from ..translation_store import CACHE_DIR_NAME
from .remote_automation import (
    CACHE_PRUNE_CANDIDATE_FRACTION,
    CACHE_PRUNE_WATERMARK_RATIO,
    SIGNATURE_CACHE_PRESCREEN_TOLERANCE,
    SIGNATURE_CACHE_QUERY_LIMIT,
    bytes_to_signature,
    signature_is_informative,
    signatures_match_for_cache,
)

logger = logging.getLogger("renpy_overlay.screenshot.store")

DB_FILENAME = "screenshot.db"

_SCHEMA_SCRIPT = """
CREATE TABLE IF NOT EXISTS game_screenshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts BIGINT NOT NULL,
    ocr_text TEXT,
    img_original BLOB,
    img_thumbnail BLOB
);
CREATE TABLE IF NOT EXISTS screenshot_signature_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature BLOB NOT NULL,
    translated TEXT NOT NULL,
    region_width INTEGER,
    region_height INTEGER,
    region_mean_0 INTEGER NOT NULL,
    region_mean_1 INTEGER NOT NULL,
    region_mean_2 INTEGER NOT NULL,
    region_mean_3 INTEGER NOT NULL,
    region_mean_4 INTEGER NOT NULL,
    region_mean_5 INTEGER NOT NULL,
    region_mean_6 INTEGER NOT NULL,
    region_mean_7 INTEGER NOT NULL,
    screenshot_id INTEGER,
    created_ts BIGINT NOT NULL,
    updated_ts BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ssc_updated ON screenshot_signature_cache(updated_ts);
"""

#: 区均值无损预筛查询（C 级过滤，排除不可能命中的候选）：全部列名为模块
#: 内部常量，无注入面；参数为 8 组 (区均值, 阈值) + LIMIT
_SIG_CACHE_PRESCREEN_SQL = (
    "SELECT id, signature, translated FROM screenshot_signature_cache WHERE "
    + " AND ".join(f"abs(region_mean_{index} - ?) <= ?" for index in range(8))
    + " ORDER BY updated_ts DESC LIMIT ?"
)


def _now_ms() -> int:
    """13 位毫秒时间戳（与 game_screenshot.ts 同一口径）。"""
    return int(time.time() * 1000)


class ScreenshotStore:
    """一个已打开的截图数据库（连接与实例同生命周期，仅主线程使用）。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        db_path: Path,
        rng: random.Random | None = None,
    ):
        self._conn = connection
        self._db_path = db_path
        self._broken = False  # 运行时读写失败后降级短路，避免反复报错刷屏
        self._rng = rng or random.Random()  # 冷端随机淘汰的随机源（测试可注入固定 seed）

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

    def delete(self, record_id: int) -> bool:
        """按 id 删除一条截图记录，返回是否删除成功（id 不存在返回 False）。"""
        if self._broken:
            return False
        try:
            cursor = self._conn.execute(
                "DELETE FROM game_screenshot WHERE id = ?", (int(record_id),)
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("删除", exc)
            return False
        if cursor.rowcount == 0:
            logger.debug("删除截图记录未命中（id=%s）：%s", record_id, self._db_path)
            return False
        logger.debug("截图记录已删除（id=%s）：%s", record_id, self._db_path)
        return True

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

    # ---- 签名缓存（遥控器自动化持久化两级命中；仅主线程，_degrade 同模式） ----

    def _prescreen_rows(self, sig_bytes: bytes, means: tuple[int, ...]):
        """区均值预筛候选（最新优先）；查询失败返回 None（语义＝未命中）。"""
        params: list[object] = []
        for mean in means:
            params.extend((int(mean), SIGNATURE_CACHE_PRESCREEN_TOLERANCE))
        params.append(SIGNATURE_CACHE_QUERY_LIMIT)
        return self._conn.execute(_SIG_CACHE_PRESCREEN_SQL, params).fetchall()

    def lookup_signature(self, sig_bytes: bytes, means: tuple[int, ...]) -> str | None:
        """按感知签名查找缓存译文；未命中/降级/异常返回 None（语义＝未命中）。

        预筛后的候选（最新优先）逐条零超阈细比较，首个命中即返回并刷新其
        ``updated_ts``（LRU 活性，把被命中条目移出冷端淘汰候选区）；刷新失败
        不影响本次命中返回。
        """
        if self._broken:
            return None
        try:
            rows = self._prescreen_rows(sig_bytes, means)
        except sqlite3.Error as exc:
            self._degrade("查询签名缓存", exc)
            return None
        query = bytes_to_signature(sig_bytes)
        for row_id, blob, translated in rows:
            candidate = bytes_to_signature(bytes(blob))
            if not signatures_match_for_cache(candidate, query):
                continue
            try:
                self._conn.execute(
                    "UPDATE screenshot_signature_cache SET updated_ts = ? WHERE id = ?",
                    (_now_ms(), int(row_id)),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                self._degrade("刷新缓存活性", exc)  # 刷新失败不影响本次命中
            logger.debug("签名缓存命中（id=%s，译文 %d 字）", row_id, len(translated))
            return str(translated)
        return None

    def put_signature(
        self,
        sig_bytes: bytes,
        means: tuple[int, ...],
        translated: str,
        region_width: int | None,
        region_height: int | None,
        screenshot_id: int | None,
        capacity: int | None = None,
    ) -> bool:
        """写入/去重一条签名缓存；失败返回 False（不影响调用方主流程）。

        相似条目（零超阈命中）覆盖译文并刷新 ``updated_ts``，不新增行（防止
        同画面多次截图产生签名噪点变体膨胀）；空白签名拒写（存储准入兜底，
        控制器侧已先行拦截）。``capacity`` 非空且插入后超限时触发冷端随机
        水位淘汰。
        """
        if self._broken:
            return False
        text = str(translated or "").strip()
        if not text:
            logger.debug("空译文不写入签名缓存")
            return False
        if not signature_is_informative(bytes_to_signature(sig_bytes)):
            logger.debug("空白签名不写入缓存（信息量不足）")
            return False
        now = _now_ms()
        try:
            rows = self._prescreen_rows(sig_bytes, means)
            hit_id: int | None = None
            for row_id, blob, _existing in rows:
                if signatures_match_for_cache(
                    bytes_to_signature(bytes(blob)), bytes_to_signature(sig_bytes)
                ):
                    hit_id = int(row_id)
                    break
            if hit_id is not None:
                self._conn.execute(
                    "UPDATE screenshot_signature_cache SET translated = ?, updated_ts = ? "
                    "WHERE id = ?",
                    (text, now, hit_id),
                )
                self._conn.commit()
                logger.debug("签名缓存相似条目已覆盖（id=%d）", hit_id)
                return True
            cursor = self._conn.execute(
                "INSERT INTO screenshot_signature_cache ("
                "signature, translated, region_width, region_height, "
                "region_mean_0, region_mean_1, region_mean_2, region_mean_3, "
                "region_mean_4, region_mean_5, region_mean_6, region_mean_7, "
                "screenshot_id, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sig_bytes,
                    text,
                    None if region_width is None else int(region_width),
                    None if region_height is None else int(region_height),
                    *(int(mean) for mean in means),
                    None if screenshot_id is None else int(screenshot_id),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._degrade("写入签名缓存", exc)
            return False
        logger.debug("签名缓存已写入（id=%s）", cursor.lastrowid)
        if capacity is not None:
            self.prune_cold_random(int(capacity))
        return True

    def signature_cache_count(self) -> int:
        """签名缓存条数（诊断用）；查询失败返回 -1。"""
        if self._broken:
            return -1
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM screenshot_signature_cache"
            ).fetchone()
        except sqlite3.Error as exc:
            self._degrade("统计签名缓存", exc)
            return -1
        return int(row[0]) if row else 0

    def prune_cold_random(self, high: int) -> int:
        """超限水位淘汰（冷端随机）：返回实际删除行数；异常降级返回 0。

        仅在按 ``updated_ts`` 最旧 ``CACHE_PRUNE_CANDIDATE_FRACTION`` 比例的
        候选内等距随机采样（随机偏移 + 取模步长），删除到水位的
        ``CACHE_PRUNE_WATERMARK_RATIO`` 比例；残余超限（采样空洞导致少删）
        由下一次写入再触发自愈，不循环强删。
        """
        if self._broken:
            return 0
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM screenshot_signature_cache"
            ).fetchone()
            total = int(row[0]) if row else 0
            if total <= int(high):
                return 0
            low = int(high * CACHE_PRUNE_WATERMARK_RATIO)
            k = total - low
            if k <= 0:
                return 0
            half = max(1, int(total * CACHE_PRUNE_CANDIDATE_FRACTION))
            step = max(1, half // k)
            offset = self._rng.randrange(step)
            cursor = self._conn.execute(
                "DELETE FROM screenshot_signature_cache WHERE id IN ("
                "SELECT id FROM ("
                "SELECT id, ROW_NUMBER() OVER (ORDER BY updated_ts ASC) AS rn "
                "FROM screenshot_signature_cache"
                ") WHERE rn <= ? AND (rn - ?) % ? = 0 LIMIT ?"
                ")",
                (half, offset, step, k),
            )
            self._conn.commit()
            removed = max(0, int(cursor.rowcount))
            logger.info(
                "签名缓存超限淘汰：%d → %d 条（水位 %.0f%%，冷端 %d%% 内等距随机采样）",
                total,
                total - removed,
                CACHE_PRUNE_WATERMARK_RATIO * 100,
                CACHE_PRUNE_CANDIDATE_FRACTION * 100,
            )
            return removed
        except sqlite3.Error as exc:
            self._degrade("签名缓存淘汰", exc)
            return 0

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
        connection.executescript(_SCHEMA_SCRIPT)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("初始化截图数据库失败（%s），降级为不入库：%s", db_path, exc)
        return None
    logger.info("截图数据库已就绪：%s", db_path)
    return ScreenshotStore(connection, db_path)
