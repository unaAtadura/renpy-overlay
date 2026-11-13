"""layout.json 纯逻辑层的离线验证：解析降级、快照组装、路径与离屏判定。

需求（.raw_plans/future_恢复布局.txt）：layout.json 缺失/损坏/字段类型非法/
坐标离屏/数量超限各有确定降级；版本字段向后兼容；全部为纯函数，离线单测
不构造任何 QWidget（沿项目测试约定）。
"""

from __future__ import annotations

from renpy_overlay import layout
from renpy_overlay.config import (
    DEFAULT_STREAM_WINDOW_HEIGHT,
    DEFAULT_STREAM_WINDOW_WIDTH,
)
from renpy_overlay.translation_store import CACHE_DIR_NAME


def _snapshot(**overrides) -> dict:
    """一份字段齐全的合法快照（collect_layout 组装），供逐项改写。"""
    kwargs = dict(
        stream_width=1000,
        stream_height=250,
        stream_locked=True,
        stream_user_offset=(12, -34),
        stream_abs_rect=(100, 200, 1100, 450),
        screenshot_windows=[(10, 20, 300, 200, True), (30, 40, 150, 100, False)],
        hotkey_mode=True,
        choice_log_open=True,
        choice_log_rect=(5, 6, 160, 320),
        choice_log_locked=True,
    )
    kwargs.update(overrides)
    return layout.collect_layout(**kwargs)


# ---- 路径 ---------------------------------------------------------------------


def test_layout_path_for_empty_game_dir_is_none():
    # 存储降级场景（无 game_dir）：跳过读写
    assert layout.layout_path_for("") is None
    assert layout.layout_path_for(None) is None


def test_layout_path_for_normal_game_dir(tmp_path):
    path = layout.layout_path_for(str(tmp_path))
    # 与四库同一 renpy_overlay_cache 目录（需求确认的落位）
    assert path == tmp_path / CACHE_DIR_NAME / "layout.json"


# ---- 解析：合法往返与版本兼容 ---------------------------------------------------


def test_collect_parse_roundtrip_preserves_all_fields():
    state = layout.parse_layout(_snapshot())
    assert state is not None
    assert state.version == layout.LAYOUT_VERSION
    assert (state.stream_window.width, state.stream_window.height) == (1000, 250)
    assert state.stream_window.locked is True
    assert state.stream_window.user_offset == (12, -34)
    assert state.stream_window.abs_rect == (100, 200, 1100, 450)
    assert [(w.x, w.y, w.width, w.height, w.locked) for w in state.screenshot_windows] == [
        (10, 20, 300, 200, True),
        (30, 40, 150, 100, False),
    ]
    assert state.hotkey_mode is True
    assert state.choice_log_window.open is True
    assert (state.choice_log_window.x, state.choice_log_window.y) == (5, 6)
    assert (state.choice_log_window.width, state.choice_log_window.height) == (160, 320)
    assert state.choice_log_window.locked is True


def test_parse_ignores_unknown_fields_forward_compat():
    payload = _snapshot()
    payload["future_section"] = {"whatever": 1}
    state = layout.parse_layout(payload)
    assert state is not None  # 未知字段忽略，不整份作废


def test_version_missing_treated_as_current():
    payload = _snapshot()
    del payload["version"]
    assert layout.parse_layout(payload) is not None


def test_version_higher_rejected_entirely():
    payload = _snapshot()
    payload["version"] = layout.LAYOUT_VERSION + 1
    assert layout.parse_layout(payload) is None


def test_version_wrong_type_rejected():
    for bad in ("1", True, 1.5, None):
        payload = _snapshot()
        payload["version"] = bad
        assert layout.parse_layout(payload) is None, bad


# ---- 解析：异常与兼容降级 -------------------------------------------------------


def test_parse_rejects_non_dict_top_level():
    assert layout.parse_layout([1, 2]) is None
    assert layout.parse_layout("layout") is None


def test_stream_invalid_size_falls_back_to_config_defaults():
    payload = _snapshot()
    payload["stream_window"]["width"] = "wide"
    payload["stream_window"]["height"] = 10
    state = layout.parse_layout(payload)
    assert state.stream_window.width == DEFAULT_STREAM_WINDOW_WIDTH
    assert state.stream_window.height == DEFAULT_STREAM_WINDOW_HEIGHT


def test_stream_invalid_offset_falls_back_to_auto_dock():
    payload = _snapshot()
    payload["stream_window"]["user_offset"] = "up"
    state = layout.parse_layout(payload)
    assert state.stream_window.user_offset is None


def test_stream_invalid_abs_rect_ignored_diagnostic_only():
    payload = _snapshot()
    payload["stream_window"]["abs_rect"] = [1, 2, 3]
    state = layout.parse_layout(payload)
    assert state.stream_window.abs_rect is None


def test_missing_stream_section_uses_defaults():
    payload = _snapshot()
    del payload["stream_window"]
    state = layout.parse_layout(payload)
    assert state.stream_window == layout.StreamLayout()


def test_missing_hotkey_mode_means_off():
    payload = _snapshot()
    del payload["hotkey_mode"]
    state = layout.parse_layout(payload)
    assert state.hotkey_mode is False


def test_choice_log_missing_section_means_closed():
    payload = _snapshot()
    del payload["choice_log_window"]
    state = layout.parse_layout(payload)
    assert state.choice_log_window.open is False


def test_screenshot_entries_truncated_to_limit():
    entries = [
        (i * 10, 0, 300, 200, False)
        for i in range(layout.MAX_SCREENSHOT_WINDOWS + 4)
    ]
    state = layout.parse_layout(_snapshot(screenshot_windows=entries))
    # 需求：不得超过 8 个——截断保留前 8 个（创建序优先）
    assert len(state.screenshot_windows) == layout.MAX_SCREENSHOT_WINDOWS


def test_screenshot_non_dict_entry_dropped():
    payload = _snapshot()
    payload["screenshot_windows"] = [{"x": 0, "y": 0, "width": 300, "height": 200}, "bad"]
    state = layout.parse_layout(payload)
    assert len(state.screenshot_windows) == 1


def test_screenshot_list_wrong_type_ignored():
    payload = _snapshot()
    payload["screenshot_windows"] = "three"
    state = layout.parse_layout(payload)
    assert state.screenshot_windows == ()


def test_screenshot_invalid_size_falls_back_to_default():
    payload = _snapshot()
    payload["screenshot_windows"] = [{"x": 0, "y": 0, "width": 10, "height": 20}]
    state = layout.parse_layout(payload)
    entry = state.screenshot_windows[0]
    # 沿 config.py 同款约定：低于下限回退默认尺寸（非钳制到下限）
    assert (entry.width, entry.height) == (
        layout.DEFAULT_SCREENSHOT_WINDOW_WIDTH,
        layout.DEFAULT_SCREENSHOT_WINDOW_HEIGHT,
    )


# ---- 几何校验纯函数 -------------------------------------------------------------


def test_rect_intersects_any_screen_cases():
    screens = [(0, 0, 1920, 1080), (-1920, 0, 1920, 1080)]
    assert layout.rect_intersects_any_screen(100, 100, 300, 200, screens)
    assert layout.rect_intersects_any_screen(-1800, 100, 300, 200, screens)  # 副屏内
    assert layout.rect_intersects_any_screen(1900, 100, 300, 200, screens)  # 跨屏相交
    assert not layout.rect_intersects_any_screen(5000, 100, 300, 200, screens)  # 全离屏
    assert not layout.rect_intersects_any_screen(1920, 100, 300, 200, screens)  # 相切不算


def test_clamp_size_enforces_minimums():
    assert layout.clamp_size(10, 20, 80, 60) == (80, 60)
    assert layout.clamp_size(300, 200, 80, 60) == (300, 200)


# ---- IO 薄壳 ---------------------------------------------------------------------


def test_load_missing_file_returns_none(tmp_path):
    assert layout.load_layout(tmp_path / CACHE_DIR_NAME / "layout.json") is None


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / CACHE_DIR_NAME / "layout.json"
    assert layout.save_layout(path, _snapshot()) is True
    state = layout.load_layout(path)
    assert state is not None
    assert state.stream_window.user_offset == (12, -34)
    assert len(state.screenshot_windows) == 2
    assert state.choice_log_window.open is True


def test_load_corrupt_json_returns_none(tmp_path):
    path = tmp_path / CACHE_DIR_NAME / "layout.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert layout.load_layout(path) is None


def test_save_write_failure_returns_false_without_raise(tmp_path, monkeypatch):
    path = tmp_path / CACHE_DIR_NAME / "layout.json"

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(path), "write_text", _boom)
    assert layout.save_layout(path, _snapshot()) is False


# ---- 遥控器节点（A/B 直径、位置与装填态；需求 future_恢复布局.txt 补充） ----------


def _remote_payload() -> dict:
    return layout.collect_remote_screenshot_layout(
        a_x=100,
        a_y=200,
        a_diameter=80,
        b_x=300,
        b_y=400,
        b_diameter=100,
        loaded_color="#ff3232",
    )


def test_remote_node_roundtrip_preserves_fields():
    state = layout.parse_layout(_snapshot(remote_screenshot=_remote_payload()))
    remote = state.remote_screenshot
    assert remote is not None
    assert (remote.a.x, remote.a.y, remote.a.diameter) == (100, 200, 80)
    assert (remote.b.x, remote.b.y, remote.b.diameter) == (300, 400, 100)
    assert remote.loaded_color == "#ff3232"


def test_remote_node_missing_or_null_means_no_restore():
    """节点缺失 / null → 不恢复（改动前行为：屏幕中心默认摆放）。"""
    assert layout.parse_layout(_snapshot()).remote_screenshot is None
    payload = _snapshot(remote_screenshot=None)
    assert layout.parse_layout(payload).remote_screenshot is None


def test_remote_node_non_dict_rejected():
    state = layout.parse_layout(_snapshot(remote_screenshot="a/b"))
    assert state.remote_screenshot is None


def test_remote_node_invalid_field_rejects_entire_node():
    """需求四.5：字段缺失或类型非法 → 改动前行为（整节点放弃，不做字段级降级）。"""
    mutations = (
        lambda p: p["a"].update(x="left"),
        lambda p: p["a"].update(y=None),
        lambda p: p["a"].pop("diameter"),
        lambda p: p["a"].update(diameter=10),  # 低于下限 20
        lambda p: p["a"].update(diameter=True),  # bool 不是合法数字
        lambda p: p.pop("a"),
        lambda p: p.pop("b"),
        lambda p: p.update(loaded_color="red"),  # 颜色格式非法
        lambda p: p.update(loaded_color=3),
    )
    for mutate in mutations:
        payload = _snapshot(remote_screenshot=_remote_payload())
        mutate(payload["remote_screenshot"])
        state = layout.parse_layout(payload)
        assert state.remote_screenshot is None, mutate


def test_remote_node_idle_color_null_is_valid():
    payload = _snapshot(remote_screenshot=_remote_payload())
    payload["remote_screenshot"]["loaded_color"] = None
    state = layout.parse_layout(payload)
    assert state.remote_screenshot.loaded_color is None  # 圆形待命态


def test_remote_node_color_stripped_on_parse():
    payload = _snapshot(remote_screenshot=_remote_payload())
    payload["remote_screenshot"]["loaded_color"] = " #FF3232 "
    state = layout.parse_layout(payload)
    assert state.remote_screenshot.loaded_color == "#FF3232"  # 大小写保留，控制器侧归一比较


def test_remote_node_unknown_fields_ignored():
    payload = _snapshot(remote_screenshot=_remote_payload())
    payload["remote_screenshot"]["future"] = 1
    assert layout.parse_layout(payload).remote_screenshot is not None


def test_load_layout_raw_missing_or_corrupt_returns_none(tmp_path):
    assert layout.load_layout_raw(tmp_path / "nope.json") is None
    path = tmp_path / "layout.json"
    path.write_text("{broken", encoding="utf-8")
    assert layout.load_layout_raw(path) is None


def test_load_layout_raw_returns_plain_dict_without_validation(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text('{"remote_screenshot": {"a": {"x": 1}}}', encoding="utf-8")
    assert layout.load_layout_raw(path) == {"remote_screenshot": {"a": {"x": 1}}}
