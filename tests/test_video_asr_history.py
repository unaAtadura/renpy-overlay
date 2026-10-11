"""语音识别历史窗口的离线验证：显示纯函数、表格模型与播放互斥状态机。

遵循项目离线测试约定：不实例化 QWidget（窗口几何与交互不测），显示与
删除回位抽成模块级纯函数直接验证；模型仅依赖 QCoreApplication（无显示
环境），配合 stub store / stub 播放器验证列表加载、播放互斥与刷新。
"""

from __future__ import annotations

import types

import pytest
from PyQt6.QtCore import QCoreApplication, Qt

from renpy_overlay.video.asr_history_window import (
    HEADERS,
    MATCH_COLOR,
    TEXT_COLUMN,
    VOLUME_WHEEL_STEP,
    AsrHistoryWindow,
    _AsrTableModel,
    _index_after_delete,
    clamp_volume,
    duration_cell_text,
    format_duration,
    next_match,
    normalize,
    volume_from_wheel,
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


# ---------------------------------------------------------------- 查找（参考翻译历史）


def test_normalize_strips_and_casefolds():
    assert normalize(None) == ""
    assert normalize("  Hello World ") == "hello world"


def test_next_match_partial_text_match_any_column():
    """部分文字匹配：任一数据列包含查询词即命中（时长/文本两列参与）。"""
    rows = [("01:30", "你好世界"), ("02:00", "Goodbye"), ("00:45", "再见")]
    assert next_match(rows, "世界", -1, True) == 0  # 文本列子串
    assert next_match(rows, "01:3", -1, True) == 0  # 时长列子串
    assert next_match(rows, "goodbye", -1, True) == 1  # 不区分大小写
    assert next_match(rows, "不存在的词", -1, True) == -1


def test_next_match_cycle_and_direction():
    rows = [("01:00", "A"), ("02:00", "B"), ("03:00", "AB")]
    # forward：从当前行下一行开始循环
    assert next_match(rows, "a", 0, True) == 2
    assert next_match(rows, "a", 2, True) == 0  # 循环回绕
    # backward：从当前行上一行开始循环
    assert next_match(rows, "a", 2, False) == 0
    assert next_match(rows, "a", 0, False) == 2  # 循环回绕
    # start=-1：next 从第 0 行、prev 从最后一行开始
    assert next_match(rows, "a", -1, True) == 0
    assert next_match(rows, "a", -1, False) == 2


def test_next_match_empty_query_or_rows():
    assert next_match([], "a", -1, True) == -1  # 空表
    assert next_match([("01:00", "A")], "", -1, True) == -1  # 空查询
    assert next_match([("01:00", "A")], "  ", -1, True) == -1  # 空白查询


def test_model_query_highlights_matching_rows():
    model = _AsrTableModel(None)
    model.set_records([(1, 65, "你好世界"), (2, 30, "Goodbye")])
    model.set_query("世界")
    assert model.data(model.index(0, TEXT_COLUMN), Qt.ItemDataRole.BackgroundRole) == MATCH_COLOR
    assert model.data(model.index(1, TEXT_COLUMN), Qt.ItemDataRole.BackgroundRole) is None
    # 时长列也参与匹配：查询子串命中时长文本
    model.set_query("00:3")
    assert model.data(model.index(1, 1), Qt.ItemDataRole.BackgroundRole) == MATCH_COLOR
    assert model.data(model.index(0, 1), Qt.ItemDataRole.BackgroundRole) is None
    # 清空查询：高亮消失
    model.set_query("")
    assert model.data(model.index(1, 1), Qt.ItemDataRole.BackgroundRole) is None


# ---------------------------------------------------------------- 音量控制条


def test_volume_from_wheel_step_and_clamp():
    """音量条滚轮步进（与视频编辑窗口同口径）：上滚增、下滚减、钳制 0..100。"""
    assert volume_from_wheel(120, 50) == 50 + VOLUME_WHEEL_STEP
    assert volume_from_wheel(-120, 50) == 50 - VOLUME_WHEEL_STEP
    assert volume_from_wheel(120, 98) == 100  # 上限钳制
    assert volume_from_wheel(-120, 2) == 0  # 下限钳制


def test_clamp_volume():
    assert clamp_volume(0) == 0
    assert clamp_volume(100) == 100
    assert clamp_volume(-5) == 0  # 负值钳零
    assert clamp_volume(150) == 100  # 超界钳满


def test_on_volume_changed_forwards_to_player():
    """音量滑块 → 播放适配器：所有条目共用同一音量（切换条目不重置）。"""
    calls: list[int] = []
    fake = types.SimpleNamespace(
        _player=types.SimpleNamespace(set_volume=lambda v: calls.append(v))
    )
    AsrHistoryWindow._on_volume_changed(fake, 40)
    AsrHistoryWindow._on_volume_changed(fake, 80)
    assert calls == [40, 80]


def test_reset_volume_sets_slider_to_mute():
    """每次打开（含隐藏后再显示）音量重置为静音 0。

    须显式同步适配器：Qt 的 valueChanged 信号仅在数值变化时发出，滑块
    已为 0 时重复 setValue(0) 不触发——若依赖信号，初始/重复重置场景下
    适配器保持 QAudioOutput 默认最大音量，滑块显示与实际播放脱节
    （实测 logs/renpy_overlay_20261011_211608.log）。"""
    slider_sets: list[int] = []
    player_calls: list[int] = []
    blocks: list[bool] = []
    fake = types.SimpleNamespace(
        _volume_slider=types.SimpleNamespace(
            setValue=lambda v: slider_sets.append(v),
            blockSignals=lambda flag: blocks.append(flag),
        ),
        _player=types.SimpleNamespace(set_volume=lambda v: player_calls.append(v)),
    )
    AsrHistoryWindow._reset_volume(fake)
    assert slider_sets == [0], "滑块应重置为静音显示"
    assert player_calls == [0], "重置必须显式应用静音到适配器（不依赖信号）"
    assert blocks == [True, False], "setValue 期间应屏蔽信号（避免重复同步）"
