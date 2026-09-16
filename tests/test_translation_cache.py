"""翻译缓存的离线验证：哈希键、原文校验（含冲突）、FIFO 淘汰与容量上限。"""

from __future__ import annotations

from renpy_overlay.translation_cache import MAX_CACHE_BYTES, TranslationCache, hash_original


def _entry_bytes(original: str, translated: str) -> int:
    """单条记录的计量：哈希键 + 原文 + 译文的 UTF-8 字节数之和。"""
    return (
        len(hash_original(original).encode("utf-8"))
        + len(original.encode("utf-8"))
        + len(translated.encode("utf-8"))
    )


def test_hash_original_is_stable_and_fixed_width():
    digest = hash_original("你好，世界")
    assert digest == hash_original("你好，世界"), "同一原文必须得到同一键"
    assert digest != hash_original("你好，世界！")
    assert len(digest) == 32
    assert all(ch in "0123456789abcdef" for ch in digest)


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
    # 计量口径：哈希 + 原文 + 译文的 UTF-8 字节数之和
    assert cache.size() == _entry_bytes("Hello", "你好")
    assert cache.count() == 1


def test_default_capacity_is_256kb():
    assert MAX_CACHE_BYTES == 256 * 1024
    assert TranslationCache().max_bytes == 256 * 1024


def test_same_hash_multiple_originals_coexist_in_bucket():
    # 注入固定哈希模拟碰撞：不同原文进入同一桶并共存
    cache = TranslationCache(hash_func=lambda _text: "same-key")
    assert cache.put("原文A", "译文A") is True
    assert cache.put("原文B", "译文B") is True  # 同哈希不同原文 → 桶内追加
    assert cache.get("原文A") == "译文A"
    assert cache.get("原文B") == "译文B"
    assert cache.count() == 2
    assert cache.get("原文C") is None, "桶内无匹配必须按未命中处理"


def test_bucket_overwrite_same_original():
    cache = TranslationCache(hash_func=lambda _text: "same-key")
    cache.put("原文A", "旧译")
    cache.put("原文A", "新译")  # 同哈希同原文 → 覆盖
    assert cache.get("原文A") == "新译"
    assert cache.count() == 1
    assert cache.get("原文B") is None


def test_bucket_overwrite_refreshes_position():
    def hasher(_text: str) -> str:
        return "same"  # 全部进同一桶

    unit = len("same") + 2 + 6  # 哈希 4 + 原文（如 A1）2 + 译文 6 = 12 字节
    cache = TranslationCache(max_bytes=unit * 3, hash_func=hasher)
    for name in ("A1", "A2", "A3"):
        cache.put(name, "x" * 6)
    cache.put("A1", "y" * 6)  # 覆盖 → 刷新为最新
    cache.put("A4", "x" * 6)  # 超限 → 淘汰 A2（而非刚覆盖的 A1）
    assert cache.get("A2") is None
    assert cache.get("A1") == "y" * 6
    assert cache.get("A4") == "x" * 6
    assert cache.count() == 3


def test_fifo_evicts_oldest_record_not_whole_bucket():
    def hasher(text: str) -> str:
        return "h1" if text.startswith("A") else "h2"  # 两个桶

    unit = len("h1") + 2 + 6  # 每条 10 字节
    cache = TranslationCache(max_bytes=unit * 3, hash_func=hasher)
    cache.put("A1", "x" * 6)
    cache.put("B1", "x" * 6)
    cache.put("A2", "x" * 6)
    assert cache.count() == 3
    cache.put("B2", "x" * 6)  # 淘汰最早的 A1（记录粒度，而非整桶 h1）
    assert cache.get("A1") is None
    assert cache.get("A2") == "x" * 6, "同桶的较新记录不应被整桶淘汰"
    assert cache.get("B1") == "x" * 6
    assert cache.get("B2") == "x" * 6
    assert cache.count() == 3


def test_fifo_eviction_by_write_order():
    unit = _entry_bytes("key1", "x" * 6)
    cache = TranslationCache(max_bytes=unit * 3)
    for name in ("key1", "key2", "key3"):
        assert cache.put(name, "x" * 6) is True
    assert cache.put("key4", "x" * 6) is True  # 第 4 条挤掉最旧的 key1
    assert cache.get("key1") is None
    assert cache.get("key2") == "x" * 6
    assert cache.size() == unit * 3
    assert cache.count() == 3


def test_overwrite_replaces_value_and_refreshes_order():
    unit = _entry_bytes("key1", "x" * 6)
    cache = TranslationCache(max_bytes=unit * 3)
    for name in ("key1", "key2", "key3"):
        cache.put(name, "x" * 6)
    assert cache.put("key1", "y" * 6) is True  # 覆盖刷新为最新
    assert cache.get("key1") == "y" * 6
    assert cache.size() == unit * 3
    cache.put("key4", "x" * 6)  # 超限：淘汰最旧的 key2（而非刚覆盖的 key1）
    assert cache.get("key2") is None
    assert cache.get("key1") == "y" * 6
    assert cache.get("key3") == "x" * 6
    assert cache.get("key4") == "x" * 6
    assert cache.count() == 3


def test_eviction_loop_until_enough_room():
    small = _entry_bytes("a", "x" * 6)
    cache = TranslationCache(max_bytes=small * 2)
    cache.put("a", "x" * 6)
    cache.put("b", "x" * 6)
    big_original, big_translated = "cccc", "y" * 21
    big = _entry_bytes(big_original, big_translated)
    assert big > small  # 新记录更大 → 需要连续淘汰两条
    assert cache.put(big_original, big_translated) is True
    assert cache.get("a") is None
    assert cache.get("b") is None
    assert cache.get(big_original) == big_translated
    assert cache.size() == big


def test_oversized_single_entry_rejected():
    tight = _entry_bytes("k", "x" * 6)
    cache = TranslationCache(max_bytes=tight)
    assert cache.put("big", "y" * 100) is False
    assert cache.get("big") is None
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
