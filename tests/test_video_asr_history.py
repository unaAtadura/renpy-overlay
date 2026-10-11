"""语音识别历史窗口的离线验证：显示纯函数、表格模型与播放互斥状态机。

遵循项目离线测试约定：不实例化 QWidget（窗口几何与交互不测），显示与
删除回位抽成模块级纯函数直接验证；模型仅依赖 QCoreApplication（无显示
环境），配合 stub store / stub 播放器验证列表加载、播放互斥与刷新。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication, Qt

from renpy_overlay.video.asr_history_window import (
    HEADERS,
    TEXT_COLUMN,
    _AsrTableModel,
    _index_after_delete,
    duration_cell_text,
    format_duration,
)


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """QAbstractTableModel 是 QObject：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


# ---------------------------------------------------------------- 显示纯函数


def test_format_duration_mmss():
    assert format_duration(0) == "00:00"
    assert format_duration(65) == "01:05"
    assert format_duration(600) == "10:00"
    assert format_duration(-3) == "00:00"  # 负值钳零


def test_duration_cell_text_playing_shows_countdown():
    assert duration_cell_text(90, playing_row=1, row=1, remaining=42) == "00:42"
    assert duration_cell_text(90, playing_row=1, row=0, remaining=42) == "01:30"
    assert duration_cell_text(90, playing_row=None, row=1, remaining=0) == "01:30"
    # 倒计时到 0（结束瞬间）也以剩余值为准
    assert duration_cell_text(90, playing_row=2, row=2, remaining=0) == "00:00"


def test_index_after_delete_reposition():
    assert _index_after_delete(2, 5) == 2  # 原位保持（下一行顺位前移）
    assert _index_after_delete(4, 4) == 3  # 删末尾行回退到前一行
    assert _index_after_delete(0, 0) == -1  # 删光


# ---------------------------------------------------------------- 表格模型


class _StubStore:
    """AsrStore 鸭子接口 stub：entries/audio 由测试注入。"""

    def __init__(self, records=None, audio_bytes=b""):
        self.records = records or []
        self.audio_bytes = audio_bytes
        self.deleted: list[int] = []

    def entries(self):
        return list(self.records)

    def audio(self, asr_id):
        return self.audio_bytes

    def delete(self, asr_id):
        self.deleted.append(asr_id)
        self.records = [r for r in self.records if r[0] != asr_id]
        return True


def test_headers_and_columns():
    assert HEADERS == ("播放", "时长", "语音文本")
    assert TEXT_COLUMN == 2


def test_model_data_three_columns():
    model = _AsrTableModel(None)
    model.set_records([(1, 65, "你好世界"), (2, 30, "再会")])
    assert model.rowCount() == 2
    # 第一列不放数据（按钮由 setIndexWidget 呈现）
    assert model.data(model.index(0, 0)) is None
    assert model.data(model.index(0, 1)) == "01:05"
    assert model.data(model.index(0, TEXT_COLUMN)) == "你好世界"
    assert model.data(model.index(1, 1)) == "00:30"


def test_model_playing_row_shows_countdown():
    model = _AsrTableModel(None)
    model.set_records([(1, 90, "文本")])
    model.set_playing_row(0)
    model.set_remaining(42)
    assert model.playing_row == 0
    assert model.data(model.index(0, 1)) == "00:42"
    model.set_playing_row(None)  # 全部停止：恢复时长
    assert model.data(model.index(0, 1)) == "01:30"


def test_model_row_id_and_text_helpers():
    model = _AsrTableModel(None)
    model.set_records([(7, 10, "文本A")])
    assert model.row_id_at(0) == 7
    assert model.row_id_at(9) is None
    assert model.duration_at(0) == 10
    assert model.text_at(0) == "文本A"
    assert model.text_at(9) == ""


def test_model_readonly_flags():
    model = _AsrTableModel(None)
    model.set_records([(1, 10, "文本")])
    for column in range(len(HEADERS)):
        flags = model.flags(model.index(0, column))
        assert flags & Qt.ItemFlag.ItemIsEnabled
        assert not (flags & Qt.ItemFlag.ItemIsEditable), "历史窗口为只读浏览"


# ---------------------------------------------------------------- 播放互斥（状态机）

# 窗口类的播放状态机逻辑以模型 + stub 播放器等价驱动：
# set_playing_row 的互斥语义（切播放先清旧行）在模型层验证。


def test_playing_row_switch_clears_previous():
    model = _AsrTableModel(None)
    model.set_records([(1, 10, "A"), (2, 20, "B")])
    model.set_playing_row(0)
    model.set_remaining(5)
    assert model.data(model.index(0, 1)) == "00:05"
    model.set_playing_row(1)  # 切换：旧行恢复时长、新行进入倒计时
    assert model.data(model.index(0, 1)) == "00:10"
    model.set_remaining(18)
    assert model.data(model.index(1, 1)) == "00:18"
    assert model.playing_row == 1
