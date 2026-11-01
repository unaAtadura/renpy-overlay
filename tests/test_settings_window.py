"""设置窗口的离线验证：元数据完整性、键冲突优先级、值转换与合并写盘。

GUI 构造本身不在此覆盖（离线测试约定：不构造真实窗口），聚焦纯函数层
（栏目表 / SettingSpec / 值转换）与 ``config.update_config_keys`` 合并写盘
行为——这两块承载了需求的全部关键语义。
"""

from __future__ import annotations

import json

from renpy_overlay import config
from renpy_overlay.settings_window import (
    KIND_BOOL,
    KIND_ENUM,
    KIND_FLOAT,
    KIND_INT,
    KIND_LANGS,
    KIND_STR,
    SettingSpec,
    commit_value,
    format_langs,
    hidden_spec_keys,
    parse_langs,
    raw_value_for,
    restart_keys,
    section_names,
    spec_by_key,
    specs_for_section,
    visible_spec_keys,
)

# ------------------------------------------------------------------ 元数据完整性


def test_sections_cover_expected_names():
    """栏目划分覆盖计划给定的功能域（名称与顺序）。"""
    assert section_names() == [
        "翻译",
        "预构建翻译缓存",
        "视觉/识图",
        "语音识别（ASR）",
        "实时语音识别",
        "截图翻译与快捷键",
        "遥控截图",
        "听歌识曲",
        "视频录制与剪辑",
        "流式窗口样式",
        "AI 对话窗",
    ]


def test_every_spec_key_is_unique_within_section():
    """同一栏目内键不重复（同键复现只允许跨栏目）。"""
    for name in section_names():
        keys = [spec.key for spec in specs_for_section(name)]
        assert len(keys) == len(set(keys)), f"栏目 {name} 内存在重复键"


def test_same_key_across_sections_shares_one_spec():
    """API 主链路键在翻译与预构建栏目复现且为同一 spec 实例（共用一份配置）。"""
    for key in ("api_base_url", "api_timeout", "model", "api_key"):
        in_translation = spec_by_key_in(key, "翻译")
        in_prebuild = spec_by_key_in(key, "预构建翻译缓存")
        assert in_translation is not None and in_prebuild is not None
        assert in_translation is in_prebuild, "跨栏目同键应为同一 SettingSpec 实例"


def spec_by_key_in(key: str, section: str) -> SettingSpec | None:
    for spec in specs_for_section(section):
        if spec.key == key:
            return spec
    return None


def test_config_fields_covered_or_hidden():
    """AppConfig 的全部 JSON 键要么在界面展示、要么在 hidden 集合（无遗漏）。

    以「默认写盘的键集合」为基准（config._write_defaults 的输出键），
    这是配置面的事实清单。
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "config.json"
        config.load_config(target)  # 触发默认写盘
        written = set(json.loads(target.read_text(encoding="utf-8")))
    visible = visible_spec_keys()
    hidden = hidden_spec_keys()
    assert written - visible - hidden == set(), f"未收录进设置窗口的键：{written - visible - hidden}"
    assert visible | hidden <= written, f"设置窗口引用了不存在的键：{visible | hidden - written}"


def test_last_game_dir_is_hidden_maintenance_key():
    assert "last_game_dir" in hidden_spec_keys()
    assert "last_game_dir" not in visible_spec_keys()


def test_restart_keys_marked_with_ranges():
    """需重启生效键：双窗字体/几何与样式、全屏渲染路径、内存缓存容量、对话窗尺寸。

    运行中的双窗/对话窗单例构造时持有字体与几何，无法安全热应用；
    其余键（API 参数 / 热键 / 自动翻译间隔 / 遥控参数等）均热重载。
    """
    assert restart_keys() == {
        "translate_window_style",
        "fullscreen_mode",
        "translation_cache_size_kb",
        "stream_window_width",
        "stream_window_height",
        "stream_window_font_size",
        "stream_window_line_spacing",
        "stream_window_title_font_size",
        "stream_window_title_gap",
        "chat_window_width",
        "chat_window_height",
        "remote_screenshot_enable",
        "remote_screenshot_interval",
        "remote_cache_size_count",
        "remote_screenshot_diameterA",
        "remote_screenshot_diameterB",
    }


def test_spin_ranges_match_config_validation():
    """数值控件范围与 config 校验范围一致（设置合法值启动时必过校验）。"""
    checks = {
        ("max_concurrency", 1, config.MAX_CONCURRENCY_LIMIT),
        ("retries", 0, config.MAX_RETRIES_LIMIT),
        ("circuit_break_threshold", 1, 100),
        ("screenshot_compress_percent", 1, 100),
        ("recording_duration", config.MIN_RECORDING_DURATION, 60),
    }
    for key, minimum, maximum in checks:
        spec = spec_by_key(key)
        assert spec is not None and spec.minimum == minimum and spec.maximum == maximum


def test_enum_specs_have_choices():
    fullscreen = spec_by_key("fullscreen_mode")
    assert fullscreen is not None
    assert fullscreen.kind == KIND_ENUM
    assert fullscreen.choices == ("sdl", "qt")
    style = spec_by_key("translate_window_style")
    assert style is not None
    assert style.choices == ("1", "2")


def test_prebuild_section_has_concurrency_and_scan_keys():
    keys = {spec.key for spec in specs_for_section("预构建翻译缓存")}
    assert {
        "max_concurrency",
        "initial_concurrency",
        "concurrency_increase_after",
        "latency_guard_seconds",
        "circuit_break_threshold",
        "retries",
        "include_tl_strings",
        "tl_langs",
    } <= keys


def test_hotkey_window_keys_are_individual_specs():
    """8 个窗口热键是 8 个独立键（各自读写 config.json 对应键）。"""
    keys = {spec.key for spec in specs_for_section("截图翻译与快捷键")}
    assert {f"hotkey_window_{i}" for i in range(1, 9)} <= keys
    window_4 = spec_by_key("hotkey_window_4")
    assert window_4 is not None and window_4.default == "4"


# ------------------------------------------------------------------ 值转换


def test_parse_and_format_langs_roundtrip():
    assert parse_langs("chinese, japanese , ,korean") == ["chinese", "japanese", "korean"]
    assert parse_langs("") == []
    langs = ["zh", "ja"]
    assert parse_langs(format_langs(langs)) == langs


def test_commit_value_by_kind():
    spec_int = SettingSpec("k", "名", KIND_INT, 8, minimum=1, maximum=64)
    assert commit_value(spec_int, 7.0) == 7
    spec_float = SettingSpec("k", "名", KIND_FLOAT, 15.0, minimum=1.0, maximum=600.0)
    assert commit_value(spec_float, 20) == 20.0
    spec_langs = SettingSpec("k", "名", KIND_LANGS, ())
    assert commit_value(spec_langs, "zh, ja") == ["zh", "ja"]
    spec_bool = SettingSpec("k", "名", KIND_BOOL, False)
    assert commit_value(spec_bool, True) is True
    spec_str = SettingSpec("k", "名", KIND_STR, "")
    assert commit_value(spec_str, " alt+t ") == " alt+t "  # 快捷键交由 load 校验


def test_raw_value_for_falls_back_on_type_mismatch():
    spec = SettingSpec("k", "名", KIND_INT, 8, minimum=1, maximum=64)
    assert raw_value_for(spec, {"k": 16}) == 16
    assert raw_value_for(spec, {}) == 8  # 缺键回退默认
    assert raw_value_for(spec, {"k": "big"}) == 8  # 类型不符
    assert raw_value_for(spec, {"k": True}) == 8  # bool 不算数字
    assert raw_value_for(spec, {"k": 999}) == 8  # 越界回退
    assert raw_value_for(spec, {"k": 2.0}) == 2.0  # 整值浮点接受（写盘时收敛）


def test_raw_value_for_enum_invalid_falls_back():
    spec = SettingSpec("k", "名", KIND_ENUM, "sdl", choices=("sdl", "qt"))
    assert raw_value_for(spec, {"k": "qt"}) == "qt"
    assert raw_value_for(spec, {"k": "vulkan"}) == "sdl"


# ------------------------------------------------------------------ 合并写盘


def test_update_config_keys_merges_without_losing_others(tmp_path):
    """合并写盘：只改目标键，未展示键与手工编辑内容原样保留。"""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"api_key": "sk-user", "custom_manual_key": 123, "max_concurrency": 2}),
        encoding="utf-8",
    )
    spec = spec_by_key("max_concurrency")
    assert spec is not None
    assert config.update_config_keys(
        {spec.key: commit_value(spec, 16)}, path=target
    )
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["max_concurrency"] == 16
    assert written["api_key"] == "sk-user", "未展示键不得丢失"
    assert written["custom_manual_key"] == 123, "手工编辑的未知键不得丢失"


def test_update_config_keys_creates_file_when_missing(tmp_path):
    target = tmp_path / "sub" / "config.json"
    assert config.update_config_keys({"last_game_dir": "D:/G"}, path=target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"last_game_dir": "D:/G"}


def test_update_config_keys_survives_broken_file(tmp_path):
    """坏文件不崩溃：合并写入以空骨架重建（仅含 updates）。"""
    target = tmp_path / "config.json"
    target.write_text("{ broken", encoding="utf-8")
    assert config.update_config_keys({"retries": 1}, path=target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"retries": 1}


def test_written_values_pass_load_config_validation(tmp_path):
    """设置窗口写入的合法值经 load_config 严格校验后语义一致（闭环）。"""
    target = tmp_path / "config.json"
    updates: dict = {}
    for spec_key, widget_value in (
        ("max_concurrency", 16),
        ("retries", 3),
        ("include_tl_strings", False),
        ("tl_langs", "zh, ja"),
        ("api_timeout", 45.0),
        ("recording_duration", 10),
    ):
        spec = spec_by_key(spec_key)
        assert spec is not None
        updates[spec_key] = commit_value(spec, widget_value)
    assert config.update_config_keys(updates, path=target)
    loaded = config.load_config(target)
    assert loaded.max_concurrency == 16
    assert loaded.retries == 3
    assert loaded.include_tl_strings is False
    assert loaded.tl_langs == ("zh", "ja")
    assert loaded.api_timeout == 45.0
    assert loaded.recording_duration == 10


# ------------------------------------------------------------------ 键冲突优先级


def test_existing_user_values_not_overwritten_by_defaults(tmp_path):
    """键冲突优先级（需求）：本项目已有键的用户取值不被窗口默认覆盖。"""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"api_base_url": "https://user.example", "api_timeout": 45.0}),
        encoding="utf-8",
    )
    raw = json.loads(target.read_text(encoding="utf-8"))
    # 模拟窗口刷新读取：用户取值原样显示（raw_value_for 不落默认）
    spec = spec_by_key("api_base_url")
    assert spec is not None
    assert raw_value_for(spec, raw) == "https://user.example"
