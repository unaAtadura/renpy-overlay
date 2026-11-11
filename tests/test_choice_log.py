"""选项副作用日志纯逻辑层测试：摘要解析、累积/去重/翻转、HTML 渲染与定位钳制。

对应源码：``src/renpy_overlay/stream/choice_log.py``（纯逻辑，无 Qt）与
``choice_log_window.render_entries_html``（HTML 生成纯函数，本文件不构造
任何真实窗口——遵守离线测试规范）。
"""

from __future__ import annotations

from renpy_overlay.stream.choice_log import (
    DUPLICATE_WINDOW_SECONDS,
    ChoiceEffectLog,
    anchored_position,
    is_effect_parsed,
    summarize_effect,
)
from renpy_overlay.stream.choice_log_window import (
    DRAG_START_THRESHOLD_PX,
    exceeds_drag_threshold,
    render_entries_html,
    scroll_drag_rate,
)

# ------------------------------------------------------------------ 摘要解析


def test_summarize_plus_minus_and_bool():
    assert summarize_effect("$ affection += 1") == "affection +1"
    assert summarize_effect("$affection-=3") == "affection -3"
    assert summarize_effect("$ met_lizzie = True") == "met_lizzie = True"
    assert summarize_effect("$ death_flag = False") == "death_flag = False"


def test_summarize_unparsable_falls_back_to_raw():
    # 函数调用：不属于可解析三类，回退原文
    assert summarize_effect("$ add_affection('e', 5)") == "add_affection('e', 5)"


def test_summarize_keeps_conditional_rhs_verbatim():
    # 三元右侧：+= 形态可摘要（右侧表达式原样保留，加减方向留给运行时）
    assert summarize_effect("$ v += 1 if cond else -1") == "v +1 if cond else -1"


def test_summarize_truncates_long_text():
    out = summarize_effect("$ " + "x" * 80)
    assert len(out) <= 48
    assert out.endswith("…")


def test_summarize_empty():
    assert summarize_effect("") == ""
    assert summarize_effect(None) == ""


def test_is_effect_parsed_matches_summarize_three_rules():
    """三类正则命中 → True（含 $ 前缀兼容）；其余形态 → False（回退原文）。"""
    assert is_effect_parsed("$ affection += 1") is True
    assert is_effect_parsed("affection -= 3") is True
    assert is_effect_parsed("$ met_lizzie = True") is True
    assert is_effect_parsed("$ death_flag = False") is True
    assert is_effect_parsed("$ add_affection('e', 5)") is False
    # 三元右侧由 += 规则承载原样保留（与 summarize_effect 同口径）
    assert is_effect_parsed("$ v += 1 if cond else -1") is True
    assert is_effect_parsed("") is False


# ------------------------------------------------------------------ 累积与去重


def _append(log, items=("A", "B"), effects=None, what="上文", ts=100.0):
    return log.append_menu(list(items), effects, who="", what=what, ts=ts)


def test_append_menu_accumulates_with_effects():
    log = ChoiceEffectLog()
    assert _append(log, effects=[["$ a += 1"], []]) is True
    entries = log.entries()
    assert len(entries) == 1
    assert [item.caption for item in entries[0].items] == ["A", "B"]
    assert entries[0].items[0].effects == ["$ a += 1"]
    assert entries[0].items[0].summaries == ["a +1"]
    assert entries[0].items[0].state == "predicted"


def test_append_menu_degrades_when_effects_none_or_mismatched():
    # 长度不齐（或 None）：整体不采用，绝不按错位位置猜测展示
    log = ChoiceEffectLog()
    assert _append(log, effects=None, ts=100.0) is True
    assert log.entries()[0].items[0].effects == []
    log2 = ChoiceEffectLog()
    assert _append(log2, effects=[["$ x += 1"]], ts=100.0) is True
    assert log2.entries()[0].items[0].effects == []


def test_append_menu_merges_duplicate_within_window():
    log = ChoiceEffectLog()
    assert _append(log, ts=100.0) is True
    assert _append(log, ts=100.0 + DUPLICATE_WINDOW_SECONDS / 2) is False
    assert len(log.entries()) == 1


def test_append_menu_keeps_replay_beyond_window_or_new_menu():
    log = ChoiceEffectLog()
    assert _append(log, ts=100.0) is True
    # 同指纹但超窗 = 回退重放/重选（新事件），照常入账
    assert _append(log, ts=100.0 + DUPLICATE_WINDOW_SECONDS * 2) is True
    assert _append(log, items=("C",), ts=200.0) is True
    assert len(log.entries()) == 3


# ------------------------------------------------------------------ 状态翻转


def test_mark_picked_flips_caption_first():
    log = ChoiceEffectLog()
    _append(log, effects=[["$ a += 1"], []], ts=100.0)
    assert log.mark_picked(-1, "B") is True  # caption 命中（index 无效）
    entry = log.entries()[0]
    assert entry.picked_index == -1
    assert [item.state for item in entry.items] == ["predicted", "happened"]


def test_mark_picked_index_fallback_and_next_entry():
    log = ChoiceEffectLog()
    _append(log, ts=100.0)
    assert log.mark_picked(1, "不存在") is True  # caption 不中 → index 兜底
    assert log.entries()[0].items[1].state == "happened"
    assert _append(log, items=("C", "D"), ts=200.0) is True  # 新菜单（指纹不同）
    assert log.mark_picked(0, "") is True
    assert log.entries()[1].items[0].state == "happened"
    assert log.entries()[0].items[1].state == "happened"  # 旧条目不受影响


def test_mark_picked_unmatched_stays_predicted():
    log = ChoiceEffectLog()
    _append(log, ts=100.0)
    assert log.mark_picked(99, "不存在") is False
    entry = log.entries()[0]
    assert all(item.state == "predicted" for item in entry.items)


def test_clear_resets_everything():
    log = ChoiceEffectLog()
    _append(log, ts=100.0)
    log.clear()
    assert log.entries() == []
    assert _append(log, ts=100.0) is True  # 去重状态一并清空：同指纹可重新入账


# ------------------------------------------------------------------ HTML 渲染


def _entry_with_states():
    log = ChoiceEffectLog()
    log.append_menu(
        ["A", "B"], [["$ a += 1"], []], who="", what="去哪里？", ts=1_700_000_000.0
    )
    log.mark_picked(0, "A")
    return log.entries()[0]


def test_render_html_marks_predicted_and_happened():
    html_text = render_entries_html([_entry_with_states()])
    assert "[预测]" in html_text
    assert "[已发生]" in html_text
    # 条件单栏：解析成功行仅显示摘要，不再并列原文（$ a += 1 不再出现）
    assert "a +1" in html_text
    assert "$ a += 1" not in html_text
    assert "去哪里？" in html_text


def test_render_html_conditional_single_column_mixed_lines():
    """混合行逐行二选一：解析成功仅摘要、动态/未命中仅完整原文（不截断），
    语句行不再出现「摘要 ｜ 原文」并列。"""
    log = ChoiceEffectLog()
    log.append_menu(
        ["接受"],
        [["$ met = True", "add_affection('e', 5)"]],
        who="",
        what="",
        ts=100.0,
    )
    html_text = render_entries_html(log.entries())
    assert "met = True" in html_text  # 解析成功：仅摘要（无 $ 前缀）
    assert "$ met = True" not in html_text  # 成功行不再并列原文
    assert "add_affection('e', 5)" in html_text  # 动态行：完整原文
    # 无「摘要 ｜ 原文」并列与孤立管道符（时间行的 ｜ 为既有分隔符，非语句行）
    assert "met = True ｜" not in html_text
    assert "｜ add_affection" not in html_text


def test_render_html_empty_log_hint():
    assert "暂无记录" in render_entries_html([])


def test_render_html_escapes_markup():
    log = ChoiceEffectLog()
    log.append_menu(["<b>选项</b>"], [["$ x = '<i>'"]], who="", what="", ts=100.0)
    html_text = render_entries_html(log.entries())
    assert "&lt;b&gt;" in html_text
    assert "&lt;i&gt;" in html_text


# ------------------------------------------------------------------ 定位钳制


def test_anchored_position_bottom_left_at_anchor_top_right():
    assert anchored_position(500, 300, 150, 300, 0, 0, 1920, 1080) == (508, 0)


def test_anchored_position_clamped_inside_screen():
    # 锚点过左/过下：整体钳进屏内（左缘 0 / 底缘屏高）
    assert anchored_position(-50, 100, 150, 300, 0, 0, 800, 600) == (0, 0)
    assert anchored_position(100, 50, 150, 300, 0, 0, 800, 600) == (108, 0)
    # 右缘溢出 → 贴屏右
    assert anchored_position(780, 300, 150, 300, 0, 0, 800, 600) == (650, 0)


def test_anchored_position_ignores_invalid_screen_geometry():
    assert anchored_position(10, 10, 150, 300, 0, 0, 0, 0) == (18, -290)


# -------------------------------------------------------------- 锁定拖拽滚动


def test_exceeds_drag_threshold_separates_double_click_and_drag():
    """双击/拖动互不误判：双击两次按击位移极低（≤阈值）不进入拖动；
    真正的拖动位移超阈值才启动（任一轴超限即算）。"""
    assert exceeds_drag_threshold(0, 0) is False
    assert exceeds_drag_threshold(3, -3) is False  # 双击按击的典型微动
    assert DRAG_START_THRESHOLD_PX == 12
    assert exceeds_drag_threshold(DRAG_START_THRESHOLD_PX, 0) is False  # 阈值处不启动
    assert exceeds_drag_threshold(DRAG_START_THRESHOLD_PX + 1, 0) is True
    assert exceeds_drag_threshold(0, -(DRAG_START_THRESHOLD_PX + 1)) is True


def test_scroll_drag_rate_direction_matches_stream_window():
    """方向与流式正文窗一致：下拖（dy>0）速率为负 → 内容向上滚回看上文；
    上拖（dy<0）速率为正 → 内容向下滚看后文；防抖区（|dy|≤12px）为 0。"""
    assert scroll_drag_rate(0.0, 14.0) == 0.0
    assert scroll_drag_rate(12.0, 14.0) == 0.0
    down = scroll_drag_rate(60.0, 14.0)
    up = scroll_drag_rate(-60.0, 14.0)
    assert down < 0.0  # 下拖：内容向上滚
    assert up > 0.0  # 上拖：内容向下滚
    assert down == -up  # 对称偏移速率对称


def test_scroll_drag_rate_fast_gear_monotonic():
    """拖得越远速率不减（防抖→慢速→快速档单调）。"""
    slow = abs(scroll_drag_rate(60.0, 14.0))
    fast = abs(scroll_drag_rate(200.0, 14.0))
    assert fast >= slow > 0.0


# ------------------------------------------------------------------ 解耦钉子


def test_effects_never_enter_choice_translation_key():
    """选项翻译键仍为纯编号选项文本（effects 是纯旁路，绝不进翻译系统）。"""
    from renpy_overlay.translation_cache import choice_translation_text

    text = choice_translation_text(["A", "B"])
    assert text == "1. A\n2. B"
    assert "$" not in text and "预测" not in text
