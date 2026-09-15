"""翻译缓存的离线验证：命中 / 覆盖 / 256KB 上限下的 FIFO 淘汰。"""

from __future__ import annotations

from renpy_overlay.translation_cache import MAX_CACHE_BYTES, TranslationCache


def test_miss_returns_none():
    cache = TranslationCache()
    assert cache.get("不存在") is None
    assert cache.get("") is None
    assert cache.size() == 0
    assert cache.count() == 0


def test_put_get_roundtrip_and_utf8_size():
    cache = TranslationCache()
    assert cache.put("Hello", "你好") is True
    assert cache.get("Hello") == "你好"
    # 计量口径：key + value 的 UTF-8 字节数之和（"Hello"=5，"你好"=6）
    assert cache.size() == 5 + 6
    assert cache.count() == 1


def test_default_capacity_is_256kb():
    assert MAX_CACHE_BYTES == 256 * 1024
    assert TranslationCache().max_bytes == 256 * 1024


def test_overwrite_replaces_value_and_refreshes_order():
    # 每条 10 字节（key=4 + value=6）；上限 30 恰好容 3 条
    cache = TranslationCache(max_bytes=30)
    cache.put("key1", "x" * 6)
    cache.put("key2", "x" * 6)
    cache.put("key3", "x" * 6)
    assert cache.put("key1", "y" * 6) is True  # 覆盖：key1 刷新为最新
    assert cache.get("key1") == "y" * 6
    assert cache.size() == 30  # 覆盖不增加计数（旧值同长）
    cache.put("key4", "x" * 6)  # 超限：淘汰最旧的 key2（而非刚覆盖的 key1）
    assert cache.get("key2") is None
    assert cache.get("key1") == "y" * 6
    assert cache.get("key3") == "x" * 6
    assert cache.get("key4") == "x" * 6
    assert cache.size() == 30
    assert cache.count() == 3


def test_fifo_eviction_by_write_order():
    cache = TranslationCache(max_bytes=30)
    for name in ("key1", "key2", "key3"):
        assert cache.put(name, "x" * 6) is True
    assert cache.put("key4", "x" * 6) is True  # 第 4 条挤掉最旧的 key1
    assert cache.get("key1") is None
    assert cache.get("key2") == "x" * 6
    assert cache.size() == 30


def test_eviction_loop_until_enough_room():
    # 上限 30：先放 2 条 10 字节；再写入一条 25 字节 → 需连续淘汰 2 条
    cache = TranslationCache(max_bytes=30)
    cache.put("a", "x" * 6)  # 10
    cache.put("b", "x" * 6)  # 10
    assert cache.put("cccc", "y" * 21) is True  # 4 + 21 = 25
    assert cache.get("a") is None
    assert cache.get("b") is None
    assert cache.get("cccc") == "y" * 21
    assert cache.size() == 25


def test_oversized_single_entry_rejected():
    cache = TranslationCache(max_bytes=20)
    assert cache.put("key", "x" * 100) is False
    assert cache.get("key") is None
    assert cache.size() == 0
    # 已有内容不受影响
    assert cache.put("k", "x" * 6) is True
    assert cache.put("big", "y" * 100) is False
    assert cache.get("k") == "x" * 6


def test_empty_inputs_rejected():
    cache = TranslationCache()
    assert cache.put("", "译文") is False
    assert cache.put("原文", "") is False
    assert cache.size() == 0


def test_clear():
    cache = TranslationCache()
    cache.put("Hello", "你好")
    assert cache.size() > 0
    cache.clear()
    assert cache.size() == 0
    assert cache.count() == 0
    assert cache.get("Hello") is None
