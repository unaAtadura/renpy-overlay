"""player_window 换算/节流纯函数单测（离线；不构造 QWidget）。

导入本模块即触发 PyQt6 导入（纯函数与控件同文件，与参考实现一致）；
环境无显示服务器亦可导入（QWidget 不实例化）。
"""

import pytest

from renpy_overlay.video.clip.player.sync_clock import DEFAULT_FPS
from renpy_overlay.video.clip.player_pane import (
    format_duration,
    frame_duration_ms,
    frame_to_ms,
    is_playback_toggle_key,
    max_frame_for,
    ms_to_frame,
    should_seek_now,
    slider_value_from_click,
    volume_from_wheel,
)
from renpy_overlay.video.clip.waveform_view import clip_visible_range


class TestFrameDuration:
    def test_normal_fps(self):
        assert frame_duration_ms(25.0) == 40

    def test_none_falls_back_to_default(self):
        assert frame_duration_ms(None) == frame_duration_ms(DEFAULT_FPS)

    def test_invalid_falls_back(self):
        assert frame_duration_ms(0) == frame_duration_ms(DEFAULT_FPS)
        assert frame_duration_ms(-5) == frame_duration_ms(DEFAULT_FPS)

    def test_floor_one_ms(self):
        assert frame_duration_ms(100000.0) == 1


class TestFrameConversions:
    def test_roundtrip(self):
        fps = 30.0
        for frame in (0, 1, 10, 100):
            assert ms_to_frame(frame_to_ms(frame, fps), fps) == frame

    def test_frame_to_ms_clamps_negative(self):
        assert frame_to_ms(-1, 30.0) == 0

    def test_max_frame(self):
        assert max_frame_for(10_000, 25.0) == 250

    def test_max_frame_no_media(self):
        assert max_frame_for(0, 25.0) == -1
        assert max_frame_for(-1, 25.0) == -1


class TestFormatDuration:
    def test_under_hour(self):
        assert format_duration(0) == "00:00"
        assert format_duration(65_000) == "01:05"

    def test_over_hour(self):
        assert format_duration(3_661_000) == "1:01:01"

    def test_negative_clamps_zero(self):
        assert format_duration(-5) == "00:00"


class TestHotkey:
    def test_space_toggles(self):
        from PyQt6.QtCore import Qt

        assert is_playback_toggle_key(int(Qt.Key.Key_Space), False)
        assert not is_playback_toggle_key(int(Qt.Key.Key_Space), True)
        assert not is_playback_toggle_key(int(Qt.Key.Key_A), False)


class TestSeekThrottle:
    def test_first_seek_always_passes(self):
        assert should_seek_now(1000.0, -1.0)

    def test_within_window_pends(self):
        assert not should_seek_now(1100.0, 1000.0, throttle_ms=150)

    def test_after_window_passes(self):
        assert should_seek_now(1150.0, 1000.0, throttle_ms=150)


class TestSliderClick:
    def test_proportional_mapping(self):
        assert slider_value_from_click(50, 100, 10_000) == 5_000

    def test_clamps(self):
        assert slider_value_from_click(-10, 100, 10_000) == 0
        assert slider_value_from_click(200, 100, 10_000) == 10_000

    def test_degenerate(self):
        assert slider_value_from_click(10, 0, 10_000) == 0
        assert slider_value_from_click(10, 100, 0) == 0


class TestVolumeWheel:
    def test_step_up_down(self):
        assert volume_from_wheel(120, 50) == 55
        assert volume_from_wheel(-120, 50) == 45

    def test_clamps(self):
        assert volume_from_wheel(120, 98) == 100
        assert volume_from_wheel(-120, 3) == 0


@pytest.mark.parametrize("fps", [23.976, 24, 25, 29.97, 30, 50, 60])
def test_common_fps_frame_durations_positive(fps):
    assert frame_duration_ms(fps) >= 1


class TestClipVisibleRange:
    """声波图绘制裁剪（可视窗口交集）：长视频放大视野下绘制列数
    必须以交集为界，否则按整段 clip 像素宽逐列绘制会退化为数十万次
    fillRect（起播断供风暴根因）。"""

    def test_clip_contains_view(self):
        # 整段 clip（0-27min）∩ 默认 10s 视野 → 整个视野
        assert clip_visible_range(0, 1_618_470, 0, 10_000) == (0, 10_000)

    def test_view_contains_clip(self):
        # 短片段完整落在视野内
        assert clip_visible_range(2_000, 3_000, 0, 10_000) == (2_000, 3_000)

    def test_clip_left_overhang(self):
        assert clip_visible_range(-500, 5_000, 0, 10_000) == (0, 5_000)

    def test_clip_right_overhang(self):
        assert clip_visible_range(8_000, 20_000, 0, 10_000) == (8_000, 10_000)

    def test_clip_spans_window(self):
        assert clip_visible_range(1_000, 20_000, 5_000, 6_000) == (5_000, 6_000)

    def test_no_intersection_left(self):
        assert clip_visible_range(0, 1_000, 2_000, 3_000) is None

    def test_no_intersection_right(self):
        assert clip_visible_range(5_000, 6_000, 0, 1_000) is None

    def test_touching_edges_excluded(self):
        # 零宽交集视为无交集（无可画像素）
        assert clip_visible_range(0, 1_000, 1_000, 2_000) is None
        assert clip_visible_range(1_000, 2_000, 0, 1_000) is None

    def test_single_ms_intersection(self):
        assert clip_visible_range(0, 1_001, 1_000, 2_000) == (1_000, 1_001)

