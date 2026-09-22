"""win32api 纯函数的离线验证（不触达任何 Win32 调用）。"""

from __future__ import annotations

from renpy_overlay import win32api
from renpy_overlay.win32api import ltrb_to_xywh


def test_ltrb_to_xywh_basic():
    # GetWindowRect 的 (l, t, r, b) -> 位置 + 尺寸
    assert ltrb_to_xywh((100, 200, 1860, 400)) == (100, 200, 1760, 200)


def test_ltrb_to_xywh_negative_origin():
    # 副屏负坐标：位置保留负值，宽高 = 差值
    assert ltrb_to_xywh((-1920, 100, -720, 500)) == (-1920, 100, 1200, 400)


def test_ltrb_to_xywh_degenerate_clamps_to_one():
    # 退化矩形（right <= left）：宽高钳到 1，避免 SetWindowPos 拿到 0/负尺寸
    assert ltrb_to_xywh((50, 50, 50, 50)) == (50, 50, 1, 1)
    assert ltrb_to_xywh((100, 100, 40, 80)) == (100, 100, 1, 1)


def test_clip_cursor_safe_degrade_off_windows(monkeypatch):
    """非 Windows 平台：ClipCursor 封装安全降级，不触达任何 Win32 调用。"""
    monkeypatch.setattr(win32api, "_IS_WINDOWS", False)
    assert win32api.clip_cursor((0, 0, 100, 100)) is False
    assert win32api.clip_cursor(None) is False
    assert win32api.get_clip_cursor() is None
