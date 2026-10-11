"""视频编辑纯函数的离线验证：帧率换算、文本解析、单击换算与音量步进。

遵循项目离线测试约定：不构造真实 QWidget（窗口与 QMediaPlayer 交互由
人工联测覆盖），仅验证模块级纯函数的换算/解析/边界行为。
"""

from __future__ import annotations

from renpy_overlay.video.editor_window import (
    DEFAULT_FPS,
    STEP_MS,
    fps_from_metadata,
    frame_duration_ms,
    frame_to_ms,
    max_frame_for,
    ms_to_frame,
    parse_frame_text,
    slider_value_from_click,
    volume_from_wheel,
)
from renpy_overlay.video.frame_viewer import MAX_SCALE, MIN_SCALE, next_scale


def test_fps_from_metadata_valid_and_fallback():
    """元数据帧率解析（防回归：PyQt6 metaDataChanged 信号无参，槽内经
    QMediaPlayer.metaData() 主动读取后交本纯函数换算）。"""
    assert fps_from_metadata(30.0) == 30.0
    assert fps_from_metadata(23.976) == 23.976
    assert fps_from_metadata(0) == DEFAULT_FPS  # 零值回退
    assert fps_from_metadata(-5) == DEFAULT_FPS  # 非法回退
    assert fps_from_metadata(None) == DEFAULT_FPS  # 元数据缺失回退
    assert fps_from_metadata("abc") == DEFAULT_FPS  # 类型非法回退


def test_frame_duration_ms_default_and_invalid_fps():
    assert frame_duration_ms(DEFAULT_FPS) == 33  # 1000/30 ≈ 33ms
    assert frame_duration_ms(0) == 33  # 非法回退 30fps
    assert frame_duration_ms(-5) == 33
    assert frame_duration_ms(60) == 17  # 1000/60 ≈ 17ms


def test_ms_frame_roundtrip():
    assert ms_to_frame(0, 30) == 0
    assert ms_to_frame(33, 30) == 1
    assert frame_to_ms(1, 30) == 33
    # 帧号 → 毫秒 → 帧号往返稳定
    for frame in (0, 1, 42, 900):
        assert ms_to_frame(frame_to_ms(frame, 30), 30) == frame


def test_frame_to_ms_clamps_negative():
    assert frame_to_ms(-3, 30) == 0


def test_parse_frame_text_valid_and_boundary():
    assert parse_frame_text("0", 100) == 0
    assert parse_frame_text(" 42 ", 100) == 42
    assert parse_frame_text("100", 100) == 100  # 边界内（含最大帧）


def test_parse_frame_text_rejects_invalid_or_out_of_range():
    """非法与越界一律不响应（返回 None）：负数、非数字、空串、超出范围。"""
    assert parse_frame_text("-1", 100) is None
    assert parse_frame_text("abc", 100) is None
    assert parse_frame_text("", 100) is None
    assert parse_frame_text("101", 100) is None
    assert parse_frame_text("1.5", 100) is None


def test_max_frame_for_unknown_duration_blocks_everything():
    """未打开视频（duration=0）时任何帧号都视为越界。"""
    assert max_frame_for(0, 30) == -1
    assert parse_frame_text("0", max_frame_for(0, 30)) is None


def test_max_frame_for_known_duration():
    # duration=10s，30fps → ~303 帧
    assert max_frame_for(10_000, 30) == ms_to_frame(10_000, 30)


def test_slider_value_from_click_ratio_and_clamp():
    assert slider_value_from_click(0, 1000, 60_000) == 0
    assert slider_value_from_click(500, 1000, 60_000) == 30_000
    assert slider_value_from_click(1000, 1000, 60_000) == 60_000
    assert slider_value_from_click(1500, 1000, 60_000) == 60_000  # 越界钳满
    assert slider_value_from_click(100, 0, 60_000) == 0  # 宽度非法归零


def test_volume_from_wheel_steps_and_clamps():
    assert volume_from_wheel(120, 50) == 55  # 上滚 +
    assert volume_from_wheel(-120, 50) == 45  # 下滚 -
    assert volume_from_wheel(120, 98) == 100  # 上限钳制
    assert volume_from_wheel(-120, 2) == 0  # 下限钳制


def test_step_ms_constant():
    assert STEP_MS == 5000  # 需求：步退/步进 5 秒


def test_next_scale_zoom_in_out_and_clamp():
    scale = 1.0
    scale = next_scale(scale, 120)  # 放大
    assert scale > 1.0
    scale = next_scale(scale, -120)  # 缩小回原值附近
    assert abs(scale - 1.0) < 1e-9
    # 钳制：反复放大不超过 MAX，反复缩小不低于 MIN
    for _ in range(50):
        scale = next_scale(scale, 120)
    assert scale == MAX_SCALE
    for _ in range(50):
        scale = next_scale(scale, -120)
    assert scale == MIN_SCALE
