"""调色盘取色对话框的离线验证：颜色转换纯函数与位图构建。

遵循离线测试约定：不构造真实窗口（``ColorPickerDialog``/子控件不在此
实例化），仅覆盖模块顶部无 Qt 依赖的纯函数区与渐变面位图（QImage 非窗口）。
转换语义必须与参考实现（test/palette/color_picker.py 的 selftest）一致。
"""

from __future__ import annotations

from PyQt6.QtGui import QImage

from renpy_overlay.color_picker_dialog import (
    AREA_H,
    AREA_W,
    SV_MAX,
    build_hue_sat_plane,
    clamp,
    from_hex,
    hsv_to_rgb255,
    is_valid_hex_color,
    normalize_hex_color,
    rgb255_to_hsv,
    to_hex,
)

# ------------------------------------------------------------------ 转换正确性


def test_hsv_to_rgb255_endpoint_cases():
    """端点与主色：与参考实现 selftest 同组用例（色调 239 回绕为纯红）。"""
    assert hsv_to_rgb255(239, 240, 240) == (255, 0, 0)
    assert hsv_to_rgb255(0, 0, 240) == (255, 255, 255)
    assert hsv_to_rgb255(0, 240, 0) == (0, 0, 0)
    # 附近为绿 / 蓝：色调每步 1/239 ≈ 1.5°，RGB 分量最坏影响约 ±4
    for hsv, expected in (((80, 240, 240), (0, 255, 0)), ((160, 240, 240), (0, 0, 255))):
        rgb = hsv_to_rgb255(*hsv)
        assert all(abs(a - b) <= 4 for a, b in zip(rgb, expected, strict=True))


def test_hsv_rgb_roundtrip_within_tolerance():
    """HSV <-> RGB 往返一致（灰色/纯黑的色调不唯一、色调 239 回绕，跳过）。"""
    cases = [(0, 240, 240), (80, 120, 60), (160, 240, 200), (200, 60, 240)]
    for hsv in cases:
        rgb = hsv_to_rgb255(*hsv)
        back = rgb255_to_hsv(*rgb)
        if hsv[1] != 0 and hsv[2] != 0:
            assert all(abs(a - b) <= 4 for a, b in zip(back, hsv, strict=True)), f"{hsv} -> {back}"


def test_hex_formatting_and_parsing():
    assert to_hex((255, 128, 0)) == "#FF8000"
    assert from_hex("#00FF7F") == (0, 255, 127)
    assert from_hex("#00ff7f") == (0, 255, 127)
    assert from_hex("abc") == (170, 187, 204)  # 3 位短格式扩展
    assert from_hex("ADD8E6") == (173, 216, 230)  # 参考实现宽容语义：无 # 前缀也接受
    assert from_hex("  #FF8000  ") == (255, 128, 0)  # 首尾空白容忍
    for bad in ("", "#XYZXYZ", "#12345", "red", "#1234567"):
        assert from_hex(bad) is None, f"{bad!r} 应解析失败"


def test_is_valid_hex_color_matches_config_semantics():
    """合法性判定与 config.COLOR_HEX_PATTERN 同口径（#RGB/#RRGGBB）。"""
    for good in ("#ADD8E6", "#add8e6", "#abc", "  #FF8000  "):
        assert is_valid_hex_color(good), f"{good!r} 应合法"
    for bad in ("red", "#12345", "", "ADD8E6", 16711680, True, None):
        assert not is_valid_hex_color(bad), f"{bad!r} 应非法"


def test_normalize_hex_color_uppercases_and_expands():
    assert normalize_hex_color("#ff8000") == "#FF8000"
    assert normalize_hex_color("#abc") == "#AABBCC"
    assert normalize_hex_color(" red ") == ""
    assert normalize_hex_color(None) == ""


def test_clamp_bounds():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


# ------------------------------------------------------------------ 渐变面位图


def test_build_hue_sat_plane_dimensions_and_format():
    """基准面为 AREA_W×AREA_H 的 RGB32 位图（构建无 QApplication 依赖）。"""
    plane = build_hue_sat_plane()
    assert plane.width() == AREA_W
    assert plane.height() == AREA_H
    assert plane.format() == QImage.Format.Format_RGB32


def test_plane_corner_pixels_match_hsv_semantics():
    """角点颜色语义（x=色调、y=饱和度反向、v=满）。

    左上 (x=0, y=0)：h=0, s=SV_MAX → 纯红 (255,0,0)；
    左下 (x=0, y=AREA_H-1)：h=0, s=1 → 接近灰阶白（参考实现 y∈[0,239]
    对应 s∈[1,240]，s=0 的纯白在越界处，沿袭不改）。
    pixel() 返回 0xAARRGGBB 布局，掩码低 24 位得 0xRRGGBB。
    """
    plane = build_hue_sat_plane()
    assert plane.pixel(0, 0) & 0xFFFFFF == 0xFF0000
    r, g, b = hsv_to_rgb255(0, 1, SV_MAX)
    assert plane.pixel(0, AREA_H - 1) & 0xFFFFFF == (r << 16) | (g << 8) | b
