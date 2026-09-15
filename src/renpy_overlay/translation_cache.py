"""翻译记忆缓存：原文 → 译文，纯内存、FIFO 淘汰、容量上限 256KB。

只在 Tk 主线程内读写（调用方保证）：后台翻译线程不直接接触本结构，一律通过
队列把结果交回主线程后再写入，避免并发访问同一数据结构。

计量口径：全部记录的 key 与 value 按 UTF-8 编码后的字节数之和，上限固定为
256KB（262144 字节），全表合计不得超过。写入超限时按写入顺序淘汰**最早加入**
的记录（FIFO）；覆盖已有 key 视为一次新写入，刷新其淘汰顺序；单条记录本身
超限则拒绝写入（不影响译文正常上屏，由调用方负责）。
"""

from __future__ import annotations

import logging
from collections import OrderedDict

logger = logging.getLogger("renpy_overlay.translation_cache")

#: 缓存总容量上限（字节，key + value 的 UTF-8 编码长度之和）
MAX_CACHE_BYTES = 256 * 1024


class TranslationCache:
    """有序字典实现的 FIFO 翻译缓存（插入顺序即淘汰顺序）。"""

    def __init__(self, max_bytes: int = MAX_CACHE_BYTES):
        self._max_bytes = int(max_bytes)
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._bytes = 0  # 全表 key+value 的 UTF-8 字节数之和

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @staticmethod
    def _cost(key: str, value: str) -> int:
        return len(key.encode("utf-8")) + len(value.encode("utf-8"))

    def get(self, key: str) -> str | None:
        """按原文查询译文；未命中返回 None（不改变淘汰顺序）。"""
        if not key:
            return None
        return self._entries.get(key)

    def put(self, key: str, value: str) -> bool:
        """写入 / 覆盖一条记录，返回是否写入成功。"""
        if not key or not value:
            return False
        cost = self._cost(key, value)
        if cost > self._max_bytes:
            logger.warning("翻译缓存单条超限（%d 字节 > %d），不写入", cost, self._max_bytes)
            return False

        previous = self._entries.pop(key, None)
        if previous is not None:
            # 覆盖视为新写入：旧记录先移除，新值按最新顺序参与淘汰
            self._bytes -= self._cost(key, previous)
            logger.debug("翻译缓存覆盖：%s…", key[:24])

        evicted = 0
        while self._entries and self._bytes + cost > self._max_bytes:
            old_key, old_value = self._entries.popitem(last=False)
            self._bytes -= self._cost(old_key, old_value)
            evicted += 1

        self._entries[key] = value
        self._bytes += cost
        if evicted:
            logger.info(
                "翻译缓存淘汰 %d 条最旧记录，当前占用 %d/%d 字节（共 %d 条）",
                evicted,
                self._bytes,
                self._max_bytes,
                len(self._entries),
            )
        else:
            logger.debug(
                "翻译缓存写入：%s…（占用 %d/%d 字节，共 %d 条）",
                key[:24],
                self._bytes,
                self._max_bytes,
                len(self._entries),
            )
        return True

    def size(self) -> int:
        """全表占用的 UTF-8 字节数（key + value 之和）。"""
        return self._bytes

    def count(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0
