"""流式悬浮窗的离线几何验证。

双窗口定位的核心是"整体几何 → 标题/正文几何"的纯计算，这里把
``pair_layout`` 当成数学函数来验证（不实例化 QWidget，无需显示环境）。
"""

from __future__ import annotations

from renpy_overlay.overlay import MARGIN, compute_geometry
from renpy_overlay.stream_window import pair_layout, pair_offset_from_body

GAME = (100, 200, 1300, 900)  # 1200 x 700
TITLE_H = 26
GAP = 4
BODY = (880, 200)
TOTAL_SIZE = (BODY[0], BODY[1] + TITLE_H + GAP)


def test_pair_layout_left_aligned_and_stacked():
    total = (10, 20, 880, 200 + TITLE_H + GAP)
    title, body = pair_layout(total, TITLE_H, GAP)
    # 两窗水平左对齐
    assert title[0] == body[0] == total[0]
    assert title[2] == body[2] == total[2]
    # 标题在上、正文在下，中间空出一个间距
    assert title[1] == total[1]
    assert body[1] == total[1] + TITLE_H + GAP
    # 高度拆分恰好用尽整体高度
    assert title[3] == TITLE_H
    assert body[3] == 200


def test_pair_layout_zero_gap():
    total = (0, 0, 500, 100 + TITLE_H)
    title, body = pair_layout(total, TITLE_H, 0)
    assert body[1] == title[1] + TITLE_H
    assert body[3] == 100


def test_pair_layout_clamps_degenerate_values():
    """极端入参下不产生零尺寸/负尺寸窗口。"""
    title, body = pair_layout((0, 0, 100, 1), TITLE_H, GAP)
    assert title[3] >= 1
    assert body[3] >= 1


def test_pair_layout_with_config_size():
    """正文窗尺寸来自配置（如默认 1760x200）时：整体几何与两窗拆分保持一致，
    超出游戏窗口的宽度仍被 compute_geometry 的既有规则钳制。"""
    config_total = (1760, 200 + TITLE_H + GAP)  # config 默认正文窗尺寸 + 标题/间距
    total = compute_geometry(GAME, config_total, "top-center")
    title, body = pair_layout(total, TITLE_H, GAP)
    assert (body[2], body[3]) == (1176, 200)  # 宽被钳到 1200 - 2*MARGIN，高保持配置值
    assert title[1] + TITLE_H + GAP == body[1]
    assert title[0] == body[0] == total[0]


def test_pair_layout_with_dock_geometry():
    """与 compute_geometry 组合：整体贴靠游戏窗口顶部，正文窗在停靠区内。"""
    total = compute_geometry(GAME, TOTAL_SIZE, "top-center")
    title, body = pair_layout(total, TITLE_H, GAP)
    assert total[1] == GAME[1] + MARGIN  # 整体（含标题）从游戏窗口 margin 处开始
    assert title[1] == total[1]
    assert body[1] == total[1] + TITLE_H + GAP
    assert (body[2], body[3]) == BODY  # 正文窗尺寸不受标题影响
    # 整体水平居中
    assert total[0] == GAME[0] + (1200 - TOTAL_SIZE[0]) // 2
    assert title[0] == body[0] == total[0]


def test_pair_layout_with_user_offset_follows_game():
    """拖动后（user_offset 存在）：整体按偏移跟随游戏窗口，两窗保持相对布局。"""
    total = compute_geometry(GAME, TOTAL_SIZE, "top-center", (33, 44))
    title, body = pair_layout(total, TITLE_H, GAP)
    assert (title[0], title[1]) == (GAME[0] + 33, GAME[1] + 44)
    assert (body[0], body[1]) == (GAME[0] + 33, GAME[1] + 44 + TITLE_H + GAP)


def test_pair_layout_survives_game_window_move():
    """游戏窗口整体移动时，两窗相对布局不变。"""
    moved = (500, 600, 1700, 1300)
    total = compute_geometry(moved, TOTAL_SIZE, "top-center", (33, 44))
    title, body = pair_layout(total, TITLE_H, GAP)
    assert title[1] + TITLE_H + GAP == body[1]
    assert title[0] == body[0]


def test_pair_offset_keeps_release_position_after_relocate():
    """松手后首次重定位不得跳动：拖动结束的正文窗位置经
    pair_offset_from_body → compute_geometry → pair_layout 后必须精确还原。"""
    game_xy = (GAME[0], GAME[1])
    body_at_release = (333, 444)
    offset = pair_offset_from_body(body_at_release, game_xy, TITLE_H, GAP)
    total = compute_geometry(GAME, TOTAL_SIZE, "top-center", offset)
    title, body = pair_layout(total, TITLE_H, GAP)
    assert (body[0], body[1]) == body_at_release  # 正文窗停在松开瞬间的位置
    assert (title[0], title[1]) == (body_at_release[0], body_at_release[1] - TITLE_H - GAP)


def test_pair_offset_regression_without_fix_would_shift_down():
    """回归守卫：若以正文窗为参照（旧实现），重定位会下移「标题高 + 间距」。"""
    body_at_release = (333, 444)
    stale = (body_at_release[0] - GAME[0], body_at_release[1] - GAME[1])  # 旧实现参照系
    total = compute_geometry(GAME, TOTAL_SIZE, "top-center", stale)
    _title, body = pair_layout(total, TITLE_H, GAP)
    assert (body[1],) == (body_at_release[1] + TITLE_H + GAP,)  # 复现旧 bug 的位移量


def test_pair_offset_follows_game_move():
    """拖动锁定后游戏窗口移动：两窗保持同一相对偏移整体跟随。"""
    offset = pair_offset_from_body((333, 444), (GAME[0], GAME[1]), TITLE_H, GAP)
    moved = (500, 600, 1700, 1300)
    total = compute_geometry(moved, TOTAL_SIZE, "top-center", offset)
    title, body = pair_layout(total, TITLE_H, GAP)
    assert (body[0], body[1]) == (733, 844)  # 随游戏窗口平移 (+400, +400)
    assert title[1] + TITLE_H + GAP == body[1]
    assert title[0] == body[0]
