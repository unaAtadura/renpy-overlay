"""对话历史窗口的离线验证：部分文字查找、删除回位纯函数（不实例化 QWidget）。

交互逻辑依赖 Qt 事件循环，遵循 GUI 逻辑离线测试约定：只对可独立验证的
纯部分（查找匹配、删除回位索引）做单元测试；窗口行为由人工联测覆盖。
"""

from __future__ import annotations

from renpy_overlay.screenshot.chat_history_window import (
    _index_after_delete,
    next_match,
    normalize,
)

_ROWS = [
    ("Hello World", "你好，世界"),
    ("Python 对话", "这是回答"),
    ("HELLO again", "Say hi"),
]


def test_normalize_strips_and_casefolds():
    assert normalize("  HeLLo ") == "hello"
    assert normalize(None) == ""


def test_next_match_substring_and_case_insensitive():
    # 需求：匹配字段的部分文字（子串包含）、不区分大小写——hello 命中第 0、2 行
    assert next_match(_ROWS, "hello", -1, forward=True) == 0
    assert next_match(_ROWS, "hello", 0, forward=True) == 2
    assert next_match(_ROWS, "hello", 2, forward=True) == 0  # 循环回绕
    # prev 从上一行开始：从第 0 行向上回绕到最后一个匹配行
    assert next_match(_ROWS, "hello", 0, forward=False) == 2


def test_next_match_searches_both_columns():
    # 两列都参与匹配：用户消息列与 AI 回复列的子串同样命中
    assert next_match(_ROWS, "回答", -1, True) == 1
    assert next_match(_ROWS, "say", -1, True) == 2


def test_next_match_no_match_and_empty_query():
    assert next_match(_ROWS, "不存在", -1, True) == -1
    assert next_match(_ROWS, "", -1, True) == -1
    assert next_match([], "hello", -1, True) == -1


def test_index_after_delete_keeps_position_or_falls_back():
    assert _index_after_delete(1, 4) == 1  # 删除中间行：下一行顺位前移
    assert _index_after_delete(4, 4) == 3  # 删末尾行：回退前一行
    assert _index_after_delete(0, 0) == -1  # 已无任何记录
