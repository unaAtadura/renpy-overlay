"""屏幕截图：Pillow ImageGrab 截取指定屏幕区域（支持多显示器负坐标）。

bbox 一律为 Windows 物理像素（进程已启用 DPI 感知，与 ``win32api.window_rect``
同一坐标系）。Pillow 的 ``grab(all_screens=True)`` 返回以虚拟屏左上角为原点的
全图，副屏坐标可能为负，因此先取虚拟屏原点再平移裁剪。
"""

from __future__ import annotations

import ctypes
import logging
import sys

from PIL import Image, ImageGrab

logger = logging.getLogger("renpy_overlay.screenshot.capture")

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77


def virtual_screen_origin() -> tuple[int, int]:
    """虚拟屏左上角的物理像素坐标（副屏在左/上时为负值；失败回退 (0, 0)）。"""
    if sys.platform != "win32":  # pragma: no cover - 非 Windows 不支持截图
        return (0, 0)
    try:
        x = ctypes.windll.user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        y = ctypes.windll.user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    except OSError:  # pragma: no cover - 极端环境兜底
        return (0, 0)
    return (int(x), int(y))


def capture_region(bbox: tuple[int, int, int, int]) -> Image.Image:
    """截取屏幕物理像素区域 ``(left, top, right, bottom)``。

    截取整个虚拟屏后按 bbox - 虚拟屏原点 裁剪，天然支持副屏负坐标。
    截图失败（锁屏 / 无桌面等）抛出原始异常，由调用方提示并恢复界面。
    """
    left, top, right, bottom = bbox
    if right <= left or bottom <= top:
        raise ValueError(f"截图区域无效：{bbox}")
    origin_x, origin_y = virtual_screen_origin()
    screen = ImageGrab.grab(all_screens=True)
    crop_box = (left - origin_x, top - origin_y, right - origin_x, bottom - origin_y)
    logger.info(
        "已截取屏幕区域 %s（虚拟屏原点 %s，尺寸 %dx%d）",
        bbox,
        (origin_x, origin_y),
        right - left,
        bottom - top,
    )
    return screen.crop(crop_box)
