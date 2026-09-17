"""截图图像的纯函数处理：宽高比约束、等比压缩、缩略图与 JPEG 编码。

与 GUI / 线程完全解耦：所有函数只接收 / 返回 ``PIL.Image.Image`` 或 bytes，
便于离线单测（``tests/test_screenshot_processing.py``）。宽高比约束逻辑
（0.10 ~ 10.00，白色像素填充）取自参考实现 ``screen_translaor_4.0/
image_processor.py``，但不含对比度增强与灰度化 —— 识图交给 vision 模型，
保留原始色彩信息更利于界面文字识别。
"""

from __future__ import annotations

import base64
import io
import logging
import math

from PIL import Image

logger = logging.getLogger("renpy_overlay.screenshot.processing")

#: 发送 API 前副本的宽高比允许范围（超出则用纯白像素填充到范围内）
MIN_ASPECT_RATIO = 0.10
MAX_ASPECT_RATIO = 10.00
#: 缩略图像素上限：10w 像素（原始截图超过该值才等比缩小，绝不放大）
THUMBNAIL_MAX_PIXELS = 100_000
#: 原始截图入库的 JPEG 质量（用户选定的体积/保真平衡值）
ORIGINAL_JPEG_QUALITY = 90
#: 缩略图入库的 JPEG 质量
THUMBNAIL_JPEG_QUALITY = 90


def constrain_aspect_ratio(image: Image.Image) -> Image.Image:
    """宽高比约束到 ``MIN_ASPECT_RATIO ~ MAX_ASPECT_RATIO``：超出则白填充。

    过宽 → 在底边填充白色像素增加高度；过高 → 在侧边填充增加宽度。
    """
    width, height = image.size
    ratio = width / height
    if ratio > MAX_ASPECT_RATIO:
        new_height = int(width / MAX_ASPECT_RATIO) + 1
        canvas = Image.new(image.mode, (width, new_height), (255, 255, 255))
        canvas.paste(image, (0, 0))
        logger.debug("图片过宽（宽高比 %.2f），底边填充白色像素至高度 %d", ratio, new_height)
        return canvas
    if ratio < MIN_ASPECT_RATIO:
        new_width = int(height * MIN_ASPECT_RATIO) + 1
        canvas = Image.new(image.mode, (new_width, height), (255, 255, 255))
        canvas.paste(image, (0, 0))
        logger.debug("图片过高（宽高比 %.2f），侧边填充白色像素至宽度 %d", ratio, new_width)
        return canvas
    logger.debug("宽高比 %.2f 在允许范围内，无需填充", ratio)
    return image


def scale_to_percent(image: Image.Image, percent: int) -> Image.Image:
    """等比缩放到原像素面积的 ``percent``%（``scale = sqrt(面积比)``）。"""
    width, height = image.size
    target_pixels = max(1, int(width * height * percent / 100))
    scale = math.sqrt(target_pixels / (width * height))
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    if new_size == (width, height):
        return image.copy()
    return image.resize(new_size, Image.LANCZOS)


def make_thumbnail(image: Image.Image) -> Image.Image:
    """生成入库缩略图：像素数超过 10w 才等比缩小，本身不超限则原样复制。"""
    width, height = image.size
    pixels = width * height
    if pixels <= THUMBNAIL_MAX_PIXELS:
        return image.copy()
    scale = math.sqrt(THUMBNAIL_MAX_PIXELS / pixels)
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(new_size, Image.LANCZOS)


def encode_jpeg_bytes(image: Image.Image, quality: int = ORIGINAL_JPEG_QUALITY) -> bytes:
    """把 PIL Image 编码为 JPEG bytes（带 RGBA/P 等模式的 RGB 兜底）。"""
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    buffered = io.BytesIO()
    image.save(buffered, format="JPEG", quality=quality)
    return buffered.getvalue()


def encode_jpeg_base64(image: Image.Image, quality: int = ORIGINAL_JPEG_QUALITY) -> str:
    """把 PIL Image 编码为 base64 字符串（vision API 的 image_url 载荷）。"""
    return base64.b64encode(encode_jpeg_bytes(image, quality)).decode("ascii")
