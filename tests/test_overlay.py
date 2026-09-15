"""悬浮窗定位计算的离线验证。

拖动与跟随的核心是"给定游戏窗口矩形 → 悬浮窗几何"的纯计算，
这里直接把 ``compute_geometry`` 当成数学函数来验证（不需要真实窗口）。
"""

from __future__ import annotations

from renpy_overlay.overlay import MARGIN, compute_geometry

GAME = (100, 200, 1300, 900)  # 1200 x 700
SIZE = (880, 200)


def test_dock_top_center():
    x, y, width, height = compute_geometry(GAME, SIZE, "top-center")
    assert (width, height) == SIZE
    assert y == GAME[1] + MARGIN
    assert x == GAME[0] + (1200 - 880) // 2


def test_dock_bottom_left():
    x, y, width, height = compute_geometry(GAME, SIZE, "bottom-left")
    assert (width, height) == SIZE
    assert x == GAME[0] + MARGIN
    assert y == GAME[3] - height - MARGIN


def test_user_offset_overrides_dock():
    """手动拖动过（存在 user_offset）时，位置不再由 dock 决定。"""
    x, y, width, height = compute_geometry(GAME, SIZE, "bottom-right", (33, 44))
    assert (x, y) == (GAME[0] + 33, GAME[1] + 44)
    assert (width, height) == SIZE


def test_user_offset_follows_game_window_move():
    """游戏窗口整体移动时，悬浮窗保持相对偏移。"""
    moved = (500, 600, 1700, 1300)
    x, y, _width, _height = compute_geometry(moved, SIZE, "top-center", (33, 44))
    assert (x, y) == (moved[0] + 33, moved[1] + 44)


def test_size_clamped_to_game_window():
    tiny = (0, 0, 300, 160)
    _x, y, width, height = compute_geometry(tiny, SIZE, "top-center")
    assert width == 300 - 2 * MARGIN
    assert height == 80  # 160 // 2 = 80，恰好等于下限
    assert y == MARGIN
