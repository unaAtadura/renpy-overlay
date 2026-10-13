"""source_filter 单测：读透分页 LRU 与 file-like 协议（临时文件，无 av 依赖）。"""

import os

import pytest

from renpy_overlay.video.player.source_filter import PAGE_SIZE, CachedSource, PageCache


@pytest.fixture()
def payload_file(tmp_path):
    """生成 2.5 页大小的确定性伪随机文件（页边界对齐可校验）。"""
    data = bytes((i * 7 + 13) % 256 for i in range(PAGE_SIZE * 2 + 1000))
    path = tmp_path / "src.bin"
    path.write_bytes(data)
    return path, data


class TestCachedSource:
    def test_sequential_read_matches_file(self, payload_file):
        path, data = payload_file
        src = CachedSource(path)
        out = bytearray()
        while chunk := src.read(1000):
            out += chunk
        assert bytes(out) == data

    def test_random_seek_read(self, payload_file):
        path, data = payload_file
        src = CachedSource(path)
        for pos, n in ((0, 10), (PAGE_SIZE - 5, 40), (PAGE_SIZE * 2, 500), (10, PAGE_SIZE)):
            assert src.seek(pos) == pos
            assert src.read(n) == data[pos : pos + n]

    def test_second_read_hits_cache(self, payload_file):
        path, data = payload_file
        cache = PageCache()
        src = CachedSource(path, cache)
        src.seek(0)
        first = src.read(5000)
        assert cache.misses > 0
        misses_before = cache.misses
        src.seek(0)
        assert src.read(5000) == first
        assert cache.misses == misses_before  # 零回源：全部命中
        assert cache.hits > 0

    def test_shared_cache_across_instances(self, payload_file):
        path, data = payload_file
        cache = PageCache()
        s1 = CachedSource(path, cache)
        s1.seek(0)
        s1.read(PAGE_SIZE)
        # 第二个实例（模拟播放容器）直接命中扫描实例（模拟索引扫描）预热的页
        s2 = CachedSource(path, cache)
        misses_before = cache.misses
        assert s2.read(PAGE_SIZE) == data[:PAGE_SIZE]
        assert cache.misses == misses_before

    def test_lru_eviction(self, payload_file):
        path, _ = payload_file
        cache = PageCache(max_bytes=PAGE_SIZE * 2)
        src = CachedSource(path, cache)
        for page in range(3):  # 3 页 > 2 页预算：最旧页被逐出
            src.seek(page * PAGE_SIZE)
            src.read(10)
        assert cache.evictions >= 1
        assert cache.page_count() <= 2

    def test_file_like_protocol(self, payload_file):
        path, data = payload_file
        src = CachedSource(path)
        assert src.seekable() and src.readable()
        assert src.tell() == 0
        assert src.seek(100, os.SEEK_CUR) == 100
        assert src.read(4) == data[100:104]
        assert src.seek(-4, os.SEEK_END) == len(data) - 4
        assert src.read(100) == data[-4:]  # 越过末尾截断
        src.close()
        src.close()  # 重复关闭容错
