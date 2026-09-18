"""截图历史删除回位索引的离线验证（纯函数，不实例化 QWidget）。"""

from __future__ import annotations

import pytest

from renpy_overlay.screenshot.history_window import _index_after_delete


def test_delete_middle_keeps_same_position():
    # 删除中间条目：下一条顺位前移到原位置（索引不变）
    assert _index_after_delete(1, 4) == 1


def test_delete_last_falls_back_to_previous():
    # 删除末尾条目：原索引在新列表中越界，回退选中前一条
    assert _index_after_delete(4, 4) == 3
    assert _index_after_delete(2, 2) == 1  # 删的是最后一条（新列表只剩 2 条）


def test_delete_only_record_enters_empty_state():
    assert _index_after_delete(0, 0) == -1


def test_delete_first_of_many():
    assert _index_after_delete(0, 3) == 0


@pytest.mark.parametrize("deleted,new_count,expected", [(0, 2, 0), (1, 2, 1), (5, 3, 2)])
def test_index_never_out_of_range(deleted, new_count, expected):
    # 任意输入下结果都在新列表范围内（防御越界）
    assert _index_after_delete(deleted, new_count) == expected
    assert expected < new_count
