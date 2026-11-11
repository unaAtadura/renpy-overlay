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
    summarize_effect,
)
from renpy_overlay.stream.choice_log_window import render_entries_html

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
    assert "a +1" in html_text and "$ a += 1" in html_text
    assert "去哪里？" in html_text


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


# ------------------------------------------------------------------ 解耦钉子


def test_effects_never_enter_choice_translation_key():
    """选项翻译键仍为纯编号选项文本（effects 是纯旁路，绝不进翻译系统）。"""
    from renpy_overlay.translation_cache import choice_translation_text

    text = choice_translation_text(["A", "B"])
    assert text == "1. A\n2. B"
    assert "$" not in text and "预测" not in text
