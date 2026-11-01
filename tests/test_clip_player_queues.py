"""queues 双缓冲基础设施单测（离线；注入桩对象，不依赖 av/SDL/Qt）。"""

import threading

import pytest

from renpy_overlay.video.clip.player.queues import AudioRingBuffer, BoundedFrameQueue, QueueAborted


class TestBoundedFrameQueue:
    def test_put_get_fifo(self):
        q = BoundedFrameQueue(2)
        q.put("a")
        q.put("b")
        assert q.get(timeout_s=0.1) == "a"
        assert q.get(timeout_s=0.1) == "b"

    def test_get_timeout_returns_none(self):
        q = BoundedFrameQueue(1)
        assert q.get(timeout_s=0.01) is None

    def test_put_blocks_when_full(self):
        q = BoundedFrameQueue(1)
        q.put("x")
        done = threading.Event()
        results: list[str] = []

        def writer():
            q.put("y")
            results.append("y")
            done.set()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        assert not done.wait(timeout=0.05)  # 满则阻塞
        assert q.get(timeout_s=0.1) == "x"
        assert done.wait(timeout=1.0)  # 取出后 put 放行
        assert results == ["y"]

    def test_abort_wakes_put_with_exception(self):
        q = BoundedFrameQueue(1)
        q.put("x")
        aborted = threading.Event()

        def writer():
            with pytest.raises(QueueAborted):
                q.put("y")
            aborted.set()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        q.abort()
        assert aborted.wait(timeout=1.0)

    def test_abort_wakes_get_with_none(self):
        q = BoundedFrameQueue(1)
        q.abort()
        assert q.get(timeout_s=1.0) is None

    def test_clear_empties(self):
        q = BoundedFrameQueue(4)
        q.put(1)
        q.put(2)
        q.clear()
        assert q.depth() == 0
        assert q.get(timeout_s=0.01) is None

    def test_unabort_resumes_put(self):
        q = BoundedFrameQueue(1)
        q.abort()
        with pytest.raises(QueueAborted):
            q.put("x")
        q.unabort()
        q.put("x")  # 不再抛异常
        assert q.get(timeout_s=0.1) == "x"


class TestAudioRingBuffer:
    def test_write_read_roundtrip(self):
        ring = AudioRingBuffer(1024)
        assert ring.write(b"\x01\x02\x03\x04") == 4
        assert ring.read(4) == b"\x01\x02\x03\x04"

    def test_read_returns_available_only(self):
        ring = AudioRingBuffer(16)
        ring.write(b"abc")
        assert ring.read(10) == b"abc"

    def test_read_empty_returns_empty_bytes(self):
        ring = AudioRingBuffer(16)
        assert ring.read(4) == b""

    def test_write_rejects_when_full(self):
        ring = AudioRingBuffer(4)
        assert ring.write(b"1234") == 4
        # 满载拒绝写入（不覆盖未播数据，worker 等水位回落再续）
        assert ring.write(b"5678") == 0
        assert ring.read(4) == b"1234"

    def test_partial_write(self):
        ring = AudioRingBuffer(4)
        assert ring.write(b"123456") == 4  # 只写入空闲空间

    def test_wraparound_read(self):
        ring = AudioRingBuffer(4)
        ring.write(b"abcd")
        ring.read(2)
        ring.write(b"ef")  # 写指针回绕
        assert ring.read(4) == b"cdef"

    def test_clear_resets_level(self):
        ring = AudioRingBuffer(8)
        ring.write(b"\x00" * 5)
        ring.clear()
        assert ring.level_bytes() == 0

    def test_level_ms(self):
        ring = AudioRingBuffer(256)
        ring.write(b"\x00" * 192)  # 1ms @ 48kHz × 立体声 × s16（4 字节/帧）
        assert ring.level_ms(48_000, 4) == pytest.approx(1.0)
