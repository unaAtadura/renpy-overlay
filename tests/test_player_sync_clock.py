"""sync_clock 纯函数单测：时钟换算 / 节拍判定 / 丢帧判定。"""

from fractions import Fraction

from renpy_overlay.video.player import sync_clock as sc


class TestClocks:
    def test_audio_clock(self):
        assert sc.audio_clock_ms(48_000, 48_000) == 1000
        assert sc.audio_clock_ms(0, 48_000) == 0
        assert sc.audio_clock_ms(240, 48_000) == 5
        assert sc.audio_clock_ms(100, 0) == 0  # 非法采样率不炸

    def test_wall_clock(self):
        assert sc.wall_clock_ms(1000, 10_000.0, 15_000.0) == 6000
        assert sc.wall_clock_ms(1000, 10_000.0, 5_000.0) == 1000  # 时钟倒流钳零流逝

    def test_frame_time_ms(self):
        class F:
            def __init__(self, t):
                self.time = t

        assert sc.frame_time_ms(F(1.5)) == 1500
        assert sc.frame_time_ms(F(None)) == 0
        assert sc.frame_time_ms(F(-0.5)) == 0

    def test_media_duration_ms(self):
        class S:
            duration = 72  # 72 单位 × (1/24)s = 3s
            time_base = Fraction(1, 24)

        assert sc.media_duration_ms(S(), None) == 3000
        assert sc.media_duration_ms(None, 4_000_000) == 4000
        assert sc.media_duration_ms(None, None) == -1


class TestPacing:
    def test_due_immediately(self):
        # 帧 pts 已到（含 10ms 提前量）→ 无需等待
        assert sc.pace_wait_ms(1000, 995) == 0
        assert sc.pace_wait_ms(1000, 1000) == 0

    def test_wait_until_due(self):
        # 帧提前于时钟 50ms → 扣除提前量等 40ms
        assert sc.pace_wait_ms(1000, 950) == 40

    def test_wait_clamped(self):
        # 主时钟停跳时等待封顶，不死等
        assert sc.pace_wait_ms(10_000, 0) == sc.MAX_PACE_WAIT_MS

    def test_drop_decision(self):
        # 落后 ≤40ms 容忍（网络/解码抖动），>40ms 丢弃追平
        assert not sc.should_drop(1000, 1030)
        assert not sc.should_drop(1000, 1040)
        assert sc.should_drop(1000, 1041)
        assert not sc.should_drop(1000, 900)  # 帧超前不丢
