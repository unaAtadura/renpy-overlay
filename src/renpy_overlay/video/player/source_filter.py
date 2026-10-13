"""源滤镜文件缓存：读透式分页 LRU，替代直连磁盘 IO。

「源滤镜（source filter）」借自 DirectShow 术语——demuxer 与物理文件
之间的读取层。本模块实现 file-like 对象（read/seek/tell/seekable），
``av.open(fileobj)`` 经由它读文件：所有读取以页（256KB）为单位进入
LRU 缓存，重复区间（编辑定位场景的回拖、关键帧索引扫描后的正式播放）
全部命中内存，磁盘 IO 归零。

关键帧索引扫描（keyframe_index）经由同一缓存层执行：首开的全量扫描
即完成页面预热，正式播放从热缓存起步。热身效果随 LRU 驻留，跨容器
（扫描容器 → 播放容器）共享同一 :class:`PageCache`。
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger("renpy_overlay.video.player.source_filter")

#: 页大小：256KB 兼顾顺序读吞吐与回拖粒度（h264 720p 25 分钟约 500MB，
#: 全量扫描页数 ~2000，页表内存可忽略）
PAGE_SIZE = 256 * 1024
#: LRU 预算（字节）：64MB ≈ 250 页，覆盖一个 GOP 邻域来回拖拽
DEFAULT_CACHE_BYTES = 64 * 1024 * 1024


class PageCache:
    """跨容器共享的分页 LRU（线程安全；键 = (文件标识, 页号)）。"""

    def __init__(self, max_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._pages: OrderedDict[tuple, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: tuple) -> bytes | None:
        with self._lock:
            page = self._pages.get(key)
            if page is not None:
                self._pages.move_to_end(key)
                self.hits += 1
            else:
                self.misses += 1
            return page

    def put(self, key: tuple, page: bytes) -> None:
        with self._lock:
            self._pages[key] = page
            self._pages.move_to_end(key)
            while len(self._pages) * PAGE_SIZE > self._max_bytes:
                self._pages.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._pages.clear()

    def page_count(self) -> int:
        with self._lock:
            return len(self._pages)


class CachedSource:
    """file-like 读透缓存源：av.open 的读路径全部经过 PageCache。

    实例持有独立文件句柄（多容器可各持一个实例共享同一 PageCache，
    互不干扰 seek 位置）；线程安全由两层保证——实例内读位置仅限所属
    容器线程访问，页级并发由 PageCache 的锁承担。
    """

    def __init__(self, path: str | Path, cache: PageCache | None = None) -> None:
        self._path = str(path)
        self._cache = cache if cache is not None else PageCache()
        self._file = open(self._path, "rb")
        self._size = os.fstat(self._file.fileno()).st_size
        self._pos = 0
        self.key = self._path  # 页键的文件标识（同一路径多实例共享缓存）

    # ---- file-like 协议（PyAV 经 ctypes 回调访问） ------------------------

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data = self._read_range(self._pos, self._size - self._pos)
            self._pos = self._size
            return data
        size = min(int(size), max(0, self._size - self._pos))
        data = self._read_range(self._pos, size)
        self._pos += len(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self._pos + offset
        elif whence == os.SEEK_END:
            pos = self._size + offset
        else:
            raise ValueError(f"非法 whence：{whence}")
        self._pos = max(0, min(int(pos), self._size))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:  # pragma: no cover - 重复关闭容错
            pass

    @property
    def size(self) -> int:
        return self._size

    @property
    def cache(self) -> PageCache:
        return self._cache

    # ---- 内部 -----------------------------------------------------------

    def _read_range(self, start: int, length: int) -> bytes:
        """按页读 [start, start+length)，命中页直接拼装，缺口页回源。"""
        if length <= 0:
            return b""
        parts: list[bytes] = []
        page_no = start // PAGE_SIZE
        end = start + length
        while page_no * PAGE_SIZE < end:
            key = (self.key, page_no)
            page = self._cache.get(key)
            if page is None:
                page = self._read_page(page_no)
                self._cache.put(key, page)
            parts.append(page)
            page_no += 1
        data = b"".join(parts)
        head = start - (start // PAGE_SIZE) * PAGE_SIZE
        return data[head : head + length]

    def _read_page(self, page_no: int) -> bytes:
        self._file.seek(page_no * PAGE_SIZE)
        return self._file.read(PAGE_SIZE)
