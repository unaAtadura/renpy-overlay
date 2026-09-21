"""流式悬浮窗的离线几何验证。

悬浮窗定位的核心是两组纯计算 —— "给定游戏窗口矩形 → 整体几何"的
``compute_geometry`` 与 "整体几何 → 标题/正文几何"的 ``pair_layout``，
以及正文窗滚动方式的纯计算（``body_scroll_target`` / ``body_wheel_target`` /
``body_drag_scroll_rate``），
这里把它们当成数学函数来验证（不实例化 QWidget，无需显示环境）。
"""

from __future__ import annotations

from renpy_overlay.bound_window import BOUND_GAP
from renpy_overlay.stream_window import (
    DRAG_SCROLL_DEBOUNCE_PX,
    DRAG_SCROLL_FAST_FACTOR,
    DRAG_SCROLL_FAST_PX,
    DRAG_SCROLL_SLOW_NOTCHES_PER_SEC,
    MARGIN,
    MASK_PAD_X,
    MASK_PAD_Y,
    WHEEL_LINES_PER_NOTCH,
    body_drag_scroll_rate,
    body_mask_bands,
    body_scroll_target,
    body_wheel_target,
    bound_column_rect,
    choice_translation_text,
    compute_geometry,
    pair_layout,
    pair_offset_from_body,
    triple_offset_from_body,
)

GAME = (100, 200, 1300, 900)  # 1200 x 700
TITLE_H = 26
GAP = 4
BODY = (880, 200)
TOTAL_SIZE = (BODY[0], BODY[1] + TITLE_H + GAP)


# ---- compute_geometry：整体几何（停靠 / 偏移 / 钳制） -----------------------


def test_dock_top_center():
    x, y, width, height = compute_geometry(GAME, BODY, "top-center")
    assert (width, height) == BODY
    assert y == GAME[1] + MARGIN
    assert x == GAME[0] + (1200 - 880) // 2


def test_dock_bottom_left():
    x, y, width, height = compute_geometry(GAME, BODY, "bottom-left")
    assert (width, height) == BODY
    assert x == GAME[0] + MARGIN
    assert y == GAME[3] - height - MARGIN


def test_user_offset_overrides_dock():
    """手动拖动过（存在 user_offset）时，位置不再由 dock 决定。"""
    x, y, width, height = compute_geometry(GAME, BODY, "bottom-right", (33, 44))
    assert (x, y) == (GAME[0] + 33, GAME[1] + 44)
    assert (width, height) == BODY


def test_user_offset_follows_game_window_move():
    """游戏窗口整体移动时，悬浮窗保持相对偏移。"""
    moved = (500, 600, 1700, 1300)
    x, y, _width, _height = compute_geometry(moved, BODY, "top-center", (33, 44))
    assert (x, y) == (moved[0] + 33, moved[1] + 44)


def test_size_clamped_to_game_window():
    tiny = (0, 0, 300, 160)
    _x, y, width, height = compute_geometry(tiny, BODY, "top-center")
    assert width == 300 - 2 * MARGIN
    assert height == 80  # 160 // 2 = 80，恰好等于下限
    assert y == MARGIN


# ---- pair_layout / pair_offset_from_body：两窗拆分与拖动参照系 --------------


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


def test_body_mask_bands_per_row_with_gaps():
    """蒙版按行聚合：同行字符一条带，相邻行条带之间留出透明空隙。"""
    spans = [(0, 24.0, 8.0), (0, 32.0, 8.0), (1, 24.0, 8.0)]
    bands = body_mask_bands(spans, line_h=20.0, box_h=16.0)
    assert len(bands) == 2
    (x0, y0, w0, h0), (x1, y1, w1, h1) = bands
    # 同一行字符聚合为一条带，水平含内边距
    assert (x0, w0) == (24.0 - MASK_PAD_X, (40.0 - 24.0) + 2 * MASK_PAD_X)
    assert (x1, w1) == (24.0 - MASK_PAD_X, 8.0 + 2 * MASK_PAD_X)
    # 条带高度不超过字形盒高，并向内收出垂直内边距
    assert h0 == h1 == min(16.0, 20.0) - 2 * MASK_PAD_Y
    # 相邻行蒙版之间留出透明空隙
    assert y1 - (y0 + h0) >= 2 * MASK_PAD_Y


def test_body_mask_bands_empty_and_streaming_partial():
    """无文字时无蒙版；打字机中途只覆盖已出现字符所在的行。"""
    assert body_mask_bands([], line_h=20.0, box_h=16.0) == []
    bands = body_mask_bands([(1, 24.0, 8.0)], line_h=20.0, box_h=16.0)
    assert len(bands) == 1  # 仅第 1 行有蒙版，未出现内容的行不提前覆盖


def test_choice_translation_text_numbered_lines():
    """选项翻译输入：与正文一致的编号逐行拼接（决定翻译键 / 缓存键）。"""
    text = choice_translation_text(["Offer a handshake", "Just welcome her"])
    assert text == "1. Offer a handshake\n2. Just welcome her"
    # 同一菜单组合稳定（重放与存档载入可命中两级缓存）
    assert text == choice_translation_text(["Offer a handshake", "Just welcome her"])


def test_choice_translation_text_empty():
    assert choice_translation_text([]) == ""


# ---- body_scroll_target / body_wheel_target：滚动方式（固定首行 + 回看） ----


LINE_H = 20.0
FULL_SCREEN = 400.0  # 可滚动内容高度（px），每格滚轮 = 3 行 = 60px


def test_scroll_target_follow_pins_first_line():
    """跟随态 = 固定显示第一行：目标偏移恒为 0，与内容多少/旧目标无关。"""
    assert body_scroll_target(True, 0.0, FULL_SCREEN) == 0.0
    assert body_scroll_target(True, 120.0, FULL_SCREEN) == 0.0
    assert body_scroll_target(True, 0.0, 0.0) == 0.0  # 不足一屏同样停在首行


def test_scroll_target_review_clamped():
    """回看态（滚轮翻阅中）：目标收敛回 [0, max_scroll]，不越界。"""
    assert body_scroll_target(False, 120.0, FULL_SCREEN) == 120.0
    assert body_scroll_target(False, -5.0, FULL_SCREEN) == 0.0
    assert body_scroll_target(False, 500.0, FULL_SCREEN) == FULL_SCREEN
    assert body_scroll_target(False, 120.0, 0.0) == 0.0  # 内容清空后目标归零


def test_wheel_down_leaves_top_and_pauses_follow():
    """下翻离开顶部：目标向底部推进，暂停固定首行（首行不再固定）。"""
    target, follow = body_wheel_target(0.0, -1.0, LINE_H, FULL_SCREEN)
    assert target == WHEEL_LINES_PER_NOTCH * LINE_H
    assert follow is False
    # 继续下翻：目标继续推进并钳在底部，保持暂停
    target, follow = body_wheel_target(0.0, -10.0, LINE_H, FULL_SCREEN)
    assert target == FULL_SCREEN
    assert follow is False


def test_wheel_up_to_top_restores_follow():
    """上翻滚回顶部：目标归零并恢复固定首行；中途停下则保持暂停。"""
    target, follow = body_wheel_target(FULL_SCREEN, 1.0, LINE_H, FULL_SCREEN)
    assert target == FULL_SCREEN - WHEEL_LINES_PER_NOTCH * LINE_H
    assert follow is False  # 未回到顶部，跟随不恢复
    target, follow = body_wheel_target(FULL_SCREEN, 8.0, LINE_H, FULL_SCREEN)
    assert target == 0.0
    assert follow is True


def test_wheel_short_content_stays_at_top():
    """内容不足一屏：任何滚动目标都钳在顶部，上翻即在首行并恢复固定。"""
    target, follow = body_wheel_target(0.0, -1.0, LINE_H, 0.0)
    assert target == 0.0
    assert follow is False  # 下翻意图明确，不强行恢复
    target, follow = body_wheel_target(0.0, 1.0, LINE_H, 0.0)
    assert target == 0.0
    assert follow is True


def test_drag_scroll_rate_debounce_zone():
    """防抖区（|dy| <= 12px）：速率为 0，不触发滚动。"""
    assert body_drag_scroll_rate(0.0, LINE_H) == 0.0
    assert body_drag_scroll_rate(DRAG_SCROLL_DEBOUNCE_PX, LINE_H) == 0.0
    assert body_drag_scroll_rate(-DRAG_SCROLL_DEBOUNCE_PX, LINE_H) == 0.0


def test_drag_scroll_rate_slow_zone():
    """慢速区：每秒 1 格滚轮；上滑看后文（正），下滑回看上文（负）。"""
    dy = DRAG_SCROLL_DEBOUNCE_PX + 1.0
    slow = DRAG_SCROLL_SLOW_NOTCHES_PER_SEC * WHEEL_LINES_PER_NOTCH * LINE_H
    assert body_drag_scroll_rate(-dy, LINE_H) == slow
    assert body_drag_scroll_rate(dy, LINE_H) == -slow
    # 分界值：恰好慢速区上沿（不超过 FAST_PX）仍是慢速
    assert body_drag_scroll_rate(-DRAG_SCROLL_FAST_PX, LINE_H) == slow


def test_drag_scroll_rate_fast_zone():
    """快速区（超过 120px）：慢速的 5 倍，即每 0.2 秒 1 格滚轮。"""
    dy = DRAG_SCROLL_FAST_PX + 1.0
    fast = (
        DRAG_SCROLL_FAST_FACTOR
        * DRAG_SCROLL_SLOW_NOTCHES_PER_SEC
        * WHEEL_LINES_PER_NOTCH
        * LINE_H
    )
    assert body_drag_scroll_rate(-dy, LINE_H) == fast
    assert body_drag_scroll_rate(dy, LINE_H) == -fast


# ---- 绑定窗口列布局（预构建功能新增的三窗几何） ------------------------------


def test_bound_column_rect_zero_keeps_dual_window_layout():
    """未注入/未显示（宽 0）时其余几何等于整体 —— 与既有双窗布局完全一致。"""
    total = (100, 200, 880, 300)
    bound, rest = bound_column_rect(total, 0, 8)
    assert bound == (100, 200, 0, 300)
    assert rest == total


def test_bound_column_rect_cuts_left_column_with_gap():
    total = (100, 200, 880, 300)
    bound, rest = bound_column_rect(total, 120, 8)
    assert bound == (100, 200, 120, 300)
    assert rest == (228, 200, 752, 300)  # 标题/正文窗整体右移一列 + 间距


def test_bound_column_rect_negative_clamped():
    bound, rest = bound_column_rect((0, 0, 100, 100), -5, 8)
    assert bound == (0, 0, 0, 100)
    assert rest == (0, 0, 100, 100)


# ---- 三窗拖动参照系（松手后额外右移缺陷的回归） ------------------------------


def test_triple_offset_matches_pair_when_no_bound_column():
    """未注入（bound_w=0）时与双窗换算完全等价：旧会话行为不变。"""
    body, game, title_h, gap = (724, 300), (100, 200), 26, 4
    assert triple_offset_from_body(body, game, title_h, gap, 0, 8) == pair_offset_from_body(
        body, game, title_h, gap
    )


def test_triple_offset_roundtrip_keeps_body_at_drop_point():
    """拖动落点往返恒等：落点 → offset → follow 布局 → 正文窗回到落点。

    复现缺陷的用例：双窗参照换算会让正文窗右移「绑定列宽 + 列距」；
    三窗参照（顶点 = 绑定窗左上）必须让正文窗精确停在松手位置。
    """
    game = (100, 200, 1300, 900)
    drop_xy = (724, 300)  # 松手时正文窗左上（物理像素）
    title_h, v_gap, bound_w = 26, 4, 120

    offset = triple_offset_from_body(
        drop_xy, (game[0], game[1]), title_h, v_gap, bound_w, BOUND_GAP
    )
    total = compute_geometry(
        game,
        (880 + bound_w, 200 + title_h + v_gap),
        "top-center",
        offset,
    )
    bound_rect, rest_total = bound_column_rect(total, bound_w, BOUND_GAP)
    title_rect, body_rect = pair_layout(rest_total, title_h, v_gap)
    assert body_rect[:2] == drop_xy, "正文窗必须精确停在松手位置（无额外横向平移）"
    # 绑定窗在标题窗左侧、同顶，列距与拖动联动一致
    assert bound_rect[0] + bound_rect[2] + BOUND_GAP == title_rect[0]
    assert bound_rect[1] == title_rect[1]
    assert bound_rect[0] == drop_xy[0] - bound_w - BOUND_GAP  # 与拖动联动同位


def test_triple_offset_roundtrip_dual_window_unchanged():
    """bound_w=0 的往返：跳过注入/未注入会话的拖动行为与既有完全一致。"""
    game = (100, 200, 1300, 900)
    drop_xy = (724, 300)
    title_h, gap = 26, 4

    offset = triple_offset_from_body(drop_xy, (game[0], game[1]), title_h, gap, 0, BOUND_GAP)
    total = compute_geometry(game, (880, 200 + title_h + gap), "top-center", offset)
    title_rect, body_rect = pair_layout(total, title_h, gap)
    assert body_rect[:2] == drop_xy
