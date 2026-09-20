"""跳过注入模式的离线验证：数据目录解析三规则（纯函数，不实例化 UI）。

目录解析是跳过注入模式唯一新增的可离线验证逻辑；IFileOpenDialog（COM）、
选择窗按钮与悬浮窗的跳过注入摆放/禁用均依赖真实窗口环境，由人工联测覆盖。
"""

from __future__ import annotations

from renpy_overlay.picker import SkipSelection, resolve_cache_dir


def test_resolve_case1_selected_is_cache_dir(tmp_path):
    # 情况1：目录名即为 renpy_overlay_cache → 使用当前目录
    selected = tmp_path / "renpy_overlay_cache"
    selected.mkdir()
    assert resolve_cache_dir(selected) == selected


def test_resolve_case2_nested_cache_dir_exists(tmp_path):
    # 情况2：目录名不同但其下已包含 renpy_overlay_cache → 使用该子目录
    nested = tmp_path / "renpy_overlay_cache"
    nested.mkdir()
    assert resolve_cache_dir(tmp_path) == nested


def test_resolve_case3_nested_cache_dir_missing(tmp_path):
    # 情况3：不包含 → 使用（由存储管线自动创建的）renpy_overlay_cache 子目录
    resolved = resolve_cache_dir(tmp_path)
    assert resolved == tmp_path / "renpy_overlay_cache"
    assert not resolved.exists(), "解析不应落盘，目录创建交由存储管线"


def test_resolve_parent_is_pipeline_game_dir(tmp_path):
    # 既有存储管线约定 game_dir 下固定挂 renpy_overlay_cache/：
    # 三种情况的解析结果取 parent 都得到同一 game_dir，正确命中数据库目录
    nested = tmp_path / "renpy_overlay_cache"
    nested.mkdir()
    for selected in (nested, tmp_path):
        assert resolve_cache_dir(selected).parent == tmp_path


def test_skip_selection_carries_raw_path():
    # 选择窗「跳过注入」的结果只携带用户选择的原始路径，解析延迟到会话启动
    selection = SkipSelection("D:/Games/Foo")
    assert selection.path == "D:/Games/Foo"
