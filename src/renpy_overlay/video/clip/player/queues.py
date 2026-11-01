"""双缓冲队列：视频帧有界队列 + 音频 PCM 独立环形缓冲。

视频与音频的缓冲彻底解耦：

- :class:`BoundedFrameQueue`：解码 worker（生产）→ 渲染线程（消费）的
  多帧预解码通道；容量有限（默认 8 帧）使 worker 恒领先渲染又不出内存
  兜底。seek 时整体 clear（配合解码代数号丢弃陈帧）。
- :class:`AudioRingBuffer`：音频独立缓冲，SDL 音频线程（callback，实时
  线程）读取、解码 worker 写入；读写均为短临界区，callback 永不阻塞
  （缺数据补静音，underrun 由 audio_output 侧计量）。

两个类都不感知 av/SDL 对象类型，可注入桩对象离线单测。
"""

from __future__ import annotations

import threading
from collections import deque


class BoundedFrameQueue:
    """有界帧队列：put 满则阻塞等待（可被 abort 唤醒），get 超时返回 None。

    abort 语义：seek/关闭时唤醒所有等待者（put 抛 QueueAborted、get 返
    None），调用方据此放弃当前流水线批次——避免 seek 后仍向新批次投递
    陈帧。
    """

    def __init__(self, maxdepth: int = 8) -> None:
        self._items: deque = deque()
        self._maxdepth = max(1, int(maxdepth))
        self._cond = threading.Condition()
        self._aborted = False

    def put(self, item) -> None:
        """投递一帧；队列满时阻塞（abort 后抛 :class:`QueueAborted`）。"""
        with self._cond:
            while len(self._items) >= self._maxdepth and not self._aborted:
                self._cond.wait(timeout=0.1)
            if self._aborted:
                raise QueueAborted()
            self._items.append(item)
            self._cond.notify_all()

    def get(self, timeout_s: float = 0.05):
        """取一帧；超时返回 None（渲染线程轮询 seek/状态用）。"""
        with self._cond:
            if not self._items and not self._aborted:
                self._cond.wait(timeout=timeout_s)
            if self._items:
                return self._items.popleft()
            return None

    def clear(self) -> int:
        """清空队列（seek/关闭），返回丢弃帧数；唤醒阻塞的 put/get。"""
        with self._cond:
            dropped = len(self._items)
            self._items.clear()
            self._cond.notify_all()
            return dropped

    def unabort(self) -> None:
        """解除 abort 态（新批次开始：seek/open 落定后调用）。"""
        with self._cond:
            self._aborted = False
            self._cond.notify_all()

    def abort(self) -> None:
        """进入 abort 态：put 立即失败、get 返回空（配合 clear 使用）。"""
        with self._cond:
            self._aborted = True
            self._items.clear()
            self._cond.notify_all()

    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    @property
    def maxdepth(self) -> int:
        return self._maxdepth


class QueueAborted(Exception):
    """put 在 abort 态被拒（批次作废信号，worker 捕获后重入主循环）。"""


class AudioRingBuffer:
    """音频 PCM 环形缓冲（s16 交错字节流；独立于视频帧队列）。

    写入（解码 worker）与读取（SDL 音频线程）各持一把锁的短临界区；
    满时拒绝写入返回 0（worker 等水位回落再续，不覆盖未播数据）。
    """

    def __init__(self, capacity_bytes: int) -> None:
        self._buf = bytearray(max(1, int(capacity_bytes)))
        self._cap = len(self._buf)
        self._read = 0  # 读指针（绝对字节偏移，取模使用）
        self._write = 0  # 写指针（绝对字节偏移，取模使用）
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        """写入 PCM；返回实际写入字节数（满则 0，调用方等待水位回落）。"""
        with self._lock:
            used = self._write - self._read
            free = self._cap - used
            n = min(len(data), free)
            if n <= 0:
                return 0
            pos = self._write % self._cap
            first = min(n, self._cap - pos)
            self._buf[pos : pos + first] = data[:first]
            if n > first:
                self._buf[0 : n - first] = data[first:n]
            self._write += n
            return n

    def read(self, need: int) -> bytes:
        """读取至多 need 字节（不足返回现有全部；空返回 b""）。"""
        with self._lock:
            used = self._write - self._read
            n = min(max(0, int(need)), used)
            if n <= 0:
                return b""
            pos = self._read % self._cap
            first = min(n, self._cap - pos)
            if n > first:
                out = bytes(self._buf[pos : pos + first]) + bytes(
                    self._buf[0 : n - first]
                )
            else:
                out = bytes(self._buf[pos : pos + first])
            self._read += n
            return out

    def clear(self) -> None:
        """清空（seek/暂停丢弃旧音频；读写指针同时归零）。"""
        with self._lock:
            self._read = 0
            self._write = 0

    def level_bytes(self) -> int:
        with self._lock:
            return self._write - self._read

    def level_ms(self, sample_rate: int, frame_size: int) -> float:
        """当前水位换算毫秒（frame_size = 声道数 × 样本宽）。"""
        if sample_rate <= 0 or frame_size <= 0:
            return 0.0
        return self.level_bytes() / (sample_rate * frame_size) * 1000.0
