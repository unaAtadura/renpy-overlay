"""翻译记忆缓存：原文哈希 → 分桶存储，纯内存、FIFO 淘汰、容量可配置。

键为**原文的稳定哈希**（sha256 前 128 位，跨进程 / 跨运行一致），每个哈希键
对应一个**桶（列表）**，桶内存放该哈希下的若干条 ``(原文, 译文)`` 记录 ——
哈希相同但原文不同的记录可以在同一桶内共存，查询时用存储原文与查询原文逐一
精确校验，绝不返回其他原文的译文。哈希键与 SQLite 持久化（``translation_store``）
使用同一个哈希函数，保证两级缓存互相对应。

只在 Tk 主线程内读写（调用方保证）：后台翻译线程不直接接触本结构，一律通过
队列把结果交回主线程后再写入，避免并发访问同一数据结构。

容量口径：全部记录的哈希键、原文、译文按 UTF-8 编码后的字节数之和，上限由
``config.json`` 的 ``translation_cache_size_kb`` 决定（默认 256KB）。写入超限时
按**记录**粒度的写入顺序淘汰最早加入的记录（FIFO，覆盖视为一次新写入并刷新其
淘汰顺序）；单条记录本身超限则拒绝写入（不影响译文正常上屏，由调用方负责）。
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable

logger = logging.getLogger("renpy_overlay.translation_cache")

#: 缓存总容量上限的默认值（字节，哈希 + 原文 + 译文的 UTF-8 编码长度之和）
MAX_CACHE_BYTES = 256 * 1024

HashFunc = Callable[[str], str]


def hash_original(text: str) -> str:
    """原文的稳定哈希键：sha256 前 128 位（32 个十六进制字符）。

    跨进程、跨运行结果一致，保证内存缓存与 SQLite 记录能相互对应；
    不用内置 ``hash()``（带随机化盐，重启即变）。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def choice_translation_text(items: list[str]) -> str:
    """分支选项的翻译输入：编号逐行拼接（与正文里的编号选项列表一致）。

    以它为翻译键 / 缓存键：同一菜单的选项组合稳定，回退重放与存档载入可
    命中两级缓存；保留序号让模型输出的译文与选项一一对应。空列表返回空串。

    原居 ``stream_window``，为离线预构建（``pretranslate`` 包）可无第三方
    依赖地复用同一口径而迁入本模块；``stream_window`` 保留 re-export。
    """
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, 1))


class TranslationCache:
    """分桶存储的 FIFO 翻译缓存。

    - ``_buckets``：哈希 → ``[(原文, 译文), ...]``（桶内按写入顺序排列）；
    - ``_order``：``(哈希, 原文) → 记录字节数``，按写入顺序排列，
      作为记录级 FIFO 淘汰与覆盖刷新位置的全局索引。
    """

    def __init__(self, max_bytes: int = MAX_CACHE_BYTES, hash_func: HashFunc = hash_original):
        self._max_bytes = int(max_bytes)
        self._hash_func = hash_func
        self._buckets: dict[str, list[tuple[str, str]]] = {}
        self._order: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._bytes = 0  # 全表 哈希 + 原文 + 译文 的 UTF-8 字节数之和

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @staticmethod
    def _cost(key: str, original: str, translated: str) -> int:
        return (
            len(key.encode("utf-8"))
            + len(original.encode("utf-8"))
            + len(translated.encode("utf-8"))
        )

    @staticmethod
    def _remove_from_bucket(bucket: list[tuple[str, str]], original: str) -> None:
        for index, (stored_original, _translated) in enumerate(bucket):
            if stored_original == original:
                del bucket[index]
                return

    def get(self, original: str) -> str | None:
        """按原文查询译文：定位哈希桶后逐一精确校验，桶内无匹配返回 None。"""
        if not original:
            return None
        bucket = self._buckets.get(self._hash_func(original))
        if not bucket:
            return None
        for stored_original, translated in bucket:
            if stored_original == original:
                return translated
        return None

    def put(self, original: str, translated: str) -> bool:
        """写入 / 覆盖一条记录，返回是否写入成功。

        同一哈希下已存在相同原文 → 覆盖译文并刷新淘汰顺序；哈希相同但原文不同
        → 在同一个桶内追加新记录（共存）。
        """
        if not original or not translated:
            return False
        key = self._hash_func(original)
        cost = self._cost(key, original, translated)
        if cost > self._max_bytes:
            logger.warning("翻译缓存单条超限（%d 字节 > %d），不写入", cost, self._max_bytes)
            return False

        record_key = (key, original)
        previous_cost = self._order.pop(record_key, None)
        if previous_cost is not None:
            # 覆盖视为新写入：移除旧记录（位置刷新为队尾），桶内同步删除
            self._bytes -= previous_cost
            self._remove_from_bucket(self._buckets.get(key, []), original)
            logger.debug("翻译缓存覆盖：%s…", original[:24])
        elif self._buckets.get(key):
            logger.debug("翻译缓存桶内追加（同哈希不同原文）：%s…", original[:24])

        # 记录级 FIFO 淘汰：从写入顺序的队首逐条淘汰，直到容下新记录
        evicted = 0
        while self._order and self._bytes + cost > self._max_bytes:
            (old_key, old_original), old_cost = self._order.popitem(last=False)
            old_bucket = self._buckets.get(old_key, [])
            self._remove_from_bucket(old_bucket, old_original)
            if not old_bucket:
                self._buckets.pop(old_key, None)
            self._bytes -= old_cost
            evicted += 1

        # 淘汰可能删空过当前桶，这里重新定位后再追加
        bucket = self._buckets.setdefault(key, [])
        bucket.append((original, translated))
        self._order[record_key] = cost
        self._bytes += cost
        if evicted:
            logger.info(
                "翻译缓存淘汰 %d 条最旧记录，当前占用 %d/%d 字节（共 %d 条 / %d 个哈希桶）",
                evicted,
                self._bytes,
                self._max_bytes,
                len(self._order),
                len(self._buckets),
            )
        else:
            logger.debug(
                "翻译缓存写入：%s…（占用 %d/%d 字节，共 %d 条 / %d 个哈希桶）",
                original[:24],
                self._bytes,
                self._max_bytes,
                len(self._order),
                len(self._buckets),
            )
        return True

    def size(self) -> int:
        """全表占用的 UTF-8 字节数（哈希 + 原文 + 译文之和）。"""
        return self._bytes

    def count(self) -> int:
        """全表记录数（含同一哈希桶内的多条记录）。"""
        return len(self._order)

    def clear(self) -> None:
        self._buckets.clear()
        self._order.clear()
        self._bytes = 0
