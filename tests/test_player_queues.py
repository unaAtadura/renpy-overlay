"""queues 单测：有界帧队列的阻塞/abort 语义 + 音频环形缓冲读写。"""

import threading

from renpy_overlay.video.player.queues import AudioRingBuffer, BoundedFrameQueue, QueueAborted


class TestBoundedFrameQueue:
    def test_put_get_fifo(self):
        q = BoundedFrameQueue(maxdepth=4)
        for i in range(4):
            q.put(i)
        assert [q.get(timeout_s=0.01) for _ in range(4)] == [0, 1, 2, 3]
        assert q.get(timeout_s=0.01) is None  # 空队列超时返回 None

    def test_put_blocks_when_full(self):
        q = BoundedFrameQueue(maxdepth=1)
        q.put("a")
        done = threading.Event()

        def blocked_put():
            q.put("b")
            done.set()

        t = threading.Thread(target=blocked_put, daemon=True)
        t.start()
        assert not done.wait(0.15)  # 队列满：put 挂起
        assert q.get(timeout_s=0.01) == "a"  # 消费一帧后 put 放行
        assert done.wait(1.0)
        assert q.get(timeout_s=0.01) == "b"

    def test_abort_rejects_put_and_clears(self):
        q = BoundedFrameQueue(maxdepth=4)
        q.put("x")
        q.abort()
        assert q.depth() == 0  # abort 清空
        try:
            q.put("y")
            raise AssertionError("abort 态 put 应抛 QueueAborted")
        except QueueAborted:
            pass

    def test_unabort_resumes(self):
        q = BoundedFrameQueue(maxdepth=4)
        q.abort()
        q.unabort()
        q.put("y")
        assert q.get(timeout_s=0.01) == "y"

    def test_clear_returns_dropped(self):
        q = BoundedFrameQueue(maxdepth=8)
        for i in range(3):
            q.put(i)
        assert q.clear() == 3
        assert q.depth() == 0


class TestAudioRingBuffer:
    def test_write_read_roundtrip(self):
        ring = AudioRingBuffer(16)
        assert ring.write(b"0123456789") == 10
        assert ring.read(4) == b"0123"
        assert ring.read(100) == b"456789"  # 不足返回现有全部
        assert ring.read(1) == b""

    def test_wraparound(self):
        ring = AudioRingBuffer(8)
        assert ring.write(b"aaaaaaaa") == 8
        assert ring.read(6) == b"aaaaaa"
        assert ring.write(b"bb") == 2  # 写指针回绕
        assert ring.read(4) == b"aabb"

    def test_full_write_rejected(self):
        ring = AudioRingBuffer(4)
        assert ring.write(b"abcd") == 4
        assert ring.write(b"x") == 0  # 满：拒绝写入不覆盖
        assert ring.read(2) == b"ab"
        assert ring.write(b"xy") == 2

    def test_partial_write(self):
        ring = AudioRingBuffer(4)
        assert ring.write(b"abcdef") == 4  # 只收下 4 字节

    def test_clear_resets(self):
        ring = AudioRingBuffer(8)
        ring.write(b"1234")
        ring.clear()
        assert ring.level_bytes() == 0
        assert ring.write(b"5678") == 4
        assert ring.read(4) == b"5678"

    def test_level_ms(self):
        ring = AudioRingBuffer(4096)
        ring.write(b"\x00" * 1920)  # 48kHz s16 立体声 = 192 字节/ms
        assert abs(ring.level_ms(48_000, 4) - 10.0) < 1e-6
