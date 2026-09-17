"""截图图像处理的离线验证：宽高比约束、百分比压缩、缩略图与 JPEG 编码。"""

from __future__ import annotations

import base64
import io

from PIL import Image

from renpy_overlay.screenshot import processing


def _solid(width: int, height: int, color=(200, 30, 30)) -> Image.Image:
    return Image.new("RGB", (width, height), color)


# ---- constrain_aspect_ratio：宽高比约束（0.10 ~ 10.00，白填充） ----------------


def test_aspect_ratio_normal_untouched():
    image = _solid(800, 600)
    result = processing.constrain_aspect_ratio(image)
    assert result.size == (800, 600)  # 1.33 在范围内：原样返回
    assert result.getpixel((0, 0)) == (200, 30, 30)


def test_aspect_ratio_too_wide_pads_bottom():
    image = _solid(2000, 100)  # ratio = 20 > 10
    result = processing.constrain_aspect_ratio(image)
    assert result.size[0] == 2000
    assert result.size[1] == int(2000 / 10.0) + 1
    # 原图内容贴顶：顶行保持原色，底行是白色填充
    assert result.getpixel((10, 10)) == (200, 30, 30)
    assert result.getpixel((10, result.size[1] - 1)) == (255, 255, 255)


def test_aspect_ratio_too_tall_pads_side():
    image = _solid(50, 2000)  # ratio = 0.025 < 0.1
    result = processing.constrain_aspect_ratio(image)
    assert result.size[1] == 2000
    assert result.size[0] == int(2000 * 0.10) + 1
    assert result.getpixel((10, 10)) == (200, 30, 30)
    assert result.getpixel((result.size[0] - 1, 10)) == (255, 255, 255)


def test_aspect_ratio_extreme_one_pixel_high():
    image = _solid(1000, 1)  # ratio = 1000
    result = processing.constrain_aspect_ratio(image)
    ratio = result.size[0] / result.size[1]
    assert ratio <= 10.0 + 0.01


# ---- scale_to_percent：等比压缩 ------------------------------------------------


def test_scale_to_percent_reduces_area():
    image = _solid(1000, 1000)  # 100w 像素
    result = processing.scale_to_percent(image, 10)
    assert result.size[0] * result.size[1] <= 100_000 * 1.05
    assert result.size[0] * result.size[1] >= 100_000 * 0.85
    assert result.size[0] == result.size[1]  # 正方形等比缩小仍为正方形


def test_scale_to_percent_100_returns_same_size():
    image = _solid(320, 240)
    result = processing.scale_to_percent(image, 100)
    assert result.size == (320, 240)


# ---- make_thumbnail：≤10w 像素缩略图 -------------------------------------------


def test_thumbnail_scales_down_large_image():
    image = _solid(1000, 1000)  # 100w 像素 > 10w
    result = processing.make_thumbnail(image)
    assert result.size[0] * result.size[1] <= processing.THUMBNAIL_MAX_PIXELS
    assert result.size[0] == result.size[1]  # 等比


def test_thumbnail_keeps_small_image():
    image = _solid(300, 200)  # 6w 像素 ≤ 10w
    result = processing.make_thumbnail(image)
    assert result.size == (300, 200)
    assert result is not image  # 副本而非引用


# ---- JPEG 编码 -----------------------------------------------------------------


def test_jpeg_bytes_roundtrip():
    image = _solid(64, 48)
    raw = processing.encode_jpeg_bytes(image)
    decoded = Image.open(io.BytesIO(raw))
    assert decoded.format == "JPEG"
    assert decoded.size == (64, 48)


def test_jpeg_base64_roundtrip():
    image = _solid(64, 48)
    encoded = processing.encode_jpeg_base64(image)
    decoded = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert decoded.size == (64, 48)


def test_jpeg_converts_rgba():
    image = Image.new("RGBA", (32, 32), (10, 20, 30, 255))
    raw = processing.encode_jpeg_bytes(image)  # RGBA 不能直接存 JPEG，应自动转 RGB
    assert Image.open(io.BytesIO(raw)).size == (32, 32)
