"""sync_clock 纯函数单测（离线；不依赖 av/SDL/Qt）。"""

from renpy_overlay.video.clip.player import sync_clock as sc


class TestStreamFps:
    def test_average_rate_priority(self):
        class S:
            average_rate = 24.0
            guessed_rate = 30.0

        assert sc.stream_fps(S()) == 24.0

    def test_guessed_rate_fallback(self):
        class S:
            average_rate = None
            guessed_rate = 25.0

        assert sc.stream_fps(S()) == 25.0

    def test_invalid_falls_back_to_default(self):
        class S:
            average_rate = 0.0
            guessed_rate = -1.0

        assert sc.stream_fps(S()) == sc.DEFAULT_FPS

    def test_non_numeric_falls_back(self):
        class S:
            average_rate = "bad"

        assert sc.stream_fps(S()) == sc.DEFAULT_FPS


class TestMediaDuration:
    def test_stream_time_base_priority(self):
        class S:
            duration = 2500  # 时基单位
            time_base = 0.001  # → 2.5s

        assert sc.media_duration_ms(S(), 9_000_000) == 2500

    def test_container_duration_fallback(self):
        assert sc.media_duration_ms(type("S", (), {"duration": None})(), 9_000_000) == 9000

    def test_missing_returns_negative(self):
        assert sc.media_duration_ms(type("S", (), {"duration": None})(), None) == -1


class TestFrameTime:
    def test_time_seconds_to_ms(self):
        assert sc.frame_time_ms(type("F", (), {"time": 1.5})()) == 1500

    def test_none_time_clamps_zero(self):
        assert sc.frame_time_ms(type("F", (), {"time": None})()) == 0

    def test_negative_time_clamps_zero(self):
        assert sc.frame_time_ms(type("F", (), {"time": -0.2})()) == 0


class TestPacing:
    def test_pace_wait_before_due(self):
        # due = 帧 pts - 提前量 - 时钟；未到期返回剩余等待
        assert sc.pace_wait_ms(1100, 990) == 100

    def test_pace_wait_due_now(self):
        assert sc.pace_wait_ms(1000, 1000) == 0

    def test_pace_wait_late(self):
        assert sc.pace_wait_ms(1000, 1100) <= 0

    def test_pace_wait_capped(self):
        assert sc.pace_wait_ms(10_000, 0) == sc.MAX_PACE_WAIT_MS

    def test_should_drop_within_lag(self):
        assert not sc.should_drop(1000, 1020, max_lag_ms=sc.DROP_LAG_MS)

    def test_should_drop_beyond_lag(self):
        assert sc.should_drop(1000, 1000 + sc.DROP_LAG_MS + 1)

    def test_wall_clock_monotonic(self):
        assert sc.wall_clock_ms(500, 100.0, 250.0) == 650
        assert sc.wall_clock_ms(500, 100.0, 50.0) == 500  # 时钟回拨钳零推进
