"""缓存键清洗口径的对齐验证：离线 clean_text 必须与运行时 agent 逐字节一致。

预构建写入 translations.db 的原文能否命中，取决于清洗口径与注入代理
（``payload/agent.py`` 的 ``_clean_text``）完全同构；本测试用样本表把两条
实现钉在一起，任一侧改动口径即在此处失败。
"""

from __future__ import annotations

import pytest

from renpy_overlay.payload.agent import _clean_text
from renpy_overlay.pretranslate.cleaning import build_choice_key, clean_text
from renpy_overlay.translation_cache import choice_translation_text

# (原始文本, 期望清洗结果)——覆盖标签、转义、空白与组合场景
_SAMPLES = [
    ("普通文本", "普通文本"),
    ("  前后空白  ", "前后空白"),
    ("带{w}停顿标签", "带停顿标签"),
    ("{i}斜体{/i}与{b}粗体{/b}", "斜体与粗体"),
    ("转义大括号{{原样}}", "转义大括号{原样}}"),
    ("转义方括号[[原样", "转义方括号[原样"),
    ("标签转义组合{w}{{x}}[[y", "标签转义组合{x}}[y"),
    ("嵌套{a{b}c}", "嵌套{ac}"),  # 正则 \{[^{}]*\} 只剥最内层一次
    ("多行\n保留换行\n只去首尾", "多行\n保留换行\n只去首尾"),
    ("", ""),
    ("{w}", ""),
    ("{i}   {/i}", ""),
]


@pytest.mark.parametrize(("raw", "expected"), _SAMPLES)
def test_clean_text_matches_agent(raw, expected):
    """离线 clean_text 与 agent._clean_text 对全部样本输出完全相等。"""
    assert clean_text(raw) == expected
    assert clean_text(raw) == _clean_text(raw)


def test_clean_text_is_agent_on_tricky_case():
    """占位保护顺序一致：[[ 在 {{ 之前出现的混合转义。"""
    raw = "[[back {{curly} {color=#fff}tag{/color}"
    assert clean_text(raw) == _clean_text(raw)


def test_build_choice_key_numbers_and_filters_blanks():
    """剔空后编号拼接，与运行时 _handle_choice 的口径一致。"""
    items = ["  A ", "", "\t", "B"]
    stripped = [item.strip() for item in items if item.strip()]
    assert build_choice_key(items) == choice_translation_text(stripped)
    assert build_choice_key(items) == "1. A\n2. B"


def test_build_choice_key_all_blank_returns_empty():
    assert build_choice_key(["", "   "]) == ""
    assert build_choice_key([]) == ""
