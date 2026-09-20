"""翻译历史窗口的离线验证：部分文字查找、删除回位纯函数（不实例化 QWidget）。

交互逻辑依赖 Qt 事件循环，遵循 GUI 逻辑离线测试约定：只对可独立验证的
纯部分（查找匹配、删除回位索引）做单元测试；分桶存储的展开与删除语义
由 test_translation_store.py 覆盖；窗口行为由人工联测覆盖。
"""

from __future__ import annotations

from renpy_overlay.translation_history_window import (
    _index_after_delete,
    next_match,
    normalize,
)

_ROWS = [
    ("Hello World", "你好，世界"),
    ("Python Tutorial", "Python 教程"),
    ("HELLO again", "你好"),
]


def test_normalize_strips_and_casefolds():
    assert normalize("  译 文 ") == "译 文"  # 仅去首尾空白，内部空白保留
    assert normalize(None) == ""


def test_next_match_substring_and_case_insensitive():
    # 需求：匹配字段的部分文字（子串包含）、不区分大小写——hello 命中第 0、2 行
    assert next_match(_ROWS, "hello", -1, forward=True) == 0
    assert next_match(_ROWS, "hello", 0, forward=True) == 2
    assert next_match(_ROWS, "hello", 2, forward=True) == 0  # 循环回绕
    assert next_match(_ROWS, "hello", 2, forward=False) == 0  # prev 从上一行开始


def test_next_match_searches_both_columns():
    # 两列都参与匹配：原文列与译文列的子串同样命中
    assert next_match(_ROWS, "教程", -1, True) == 1
    assert next_match(_ROWS, "你好", -1, True) == 0


def test_next_match_no_match_and_empty_query():
    assert next_match(_ROWS, "不存在", -1, True) == -1
    assert next_match(_ROWS, "", -1, True) == -1
    assert next_match([], "hello", -1, True) == -1


def test_index_after_delete_keeps_position_or_falls_back():
    assert _index_after_delete(0, 3) == 0  # 删除中间行：下一行顺位前移
    assert _index_after_delete(2, 2) == 1  # 删末尾行：回退前一行
    assert _index_after_delete(0, 0) == -1  # 已无任何记录
