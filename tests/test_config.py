"""本地配置加载的离线验证：首建默认值、非法内容回退、合法值读取。"""

from __future__ import annotations

import json

from renpy_overlay import config, translator


def test_missing_file_creates_defaults(tmp_path):
    target = tmp_path / "config.json"
    loaded = config.load_config(target)
    assert loaded == config.AppConfig()
    assert loaded.auto_translate is False
    assert loaded.auto_translate_interval == 3.0
    assert loaded.show_original_text is True
    assert loaded.translation_cache_size_kb == 256
    assert target.is_file(), "首次运行应自动创建配置文件"
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == {
        "auto_translate": False,
        "auto_translate_interval": 3.0,
        "show_original_text": True,
        "translation_cache_size_kb": 256,
        "api_base_url": "http://127.0.0.1:1234",
        "api_timeout": 20.0,
        "model": "",
        "system_prompt": translator.DEFAULT_SYSTEM_PROMPT,
        "api_key": "",
        "enable_thinking": False,
        "reasoning_effort": "none",
        "stream_window_width": 1760,
        "stream_window_height": 200,
        "stream_window_font_size": 14,
        "stream_window_line_spacing": 1.45,
        "stream_window_title_font_size": 8,
        "stream_window_title_gap": 4,
        "screenshot_compress_percent": 10,
        "screenshot_model": "",
        "recording_duration": 8,
    }


def test_api_defaults_loaded(tmp_path):
    target = tmp_path / "config.json"
    loaded = config.load_config(target)
    # 默认值必须与 translator 的模块级常量完全一致（未改配置时零行为变化）
    assert loaded.api_base_url == translator.DEFAULT_BASE_URL
    assert loaded.api_timeout == translator.DEFAULT_TIMEOUT
    assert loaded.system_prompt == translator.DEFAULT_SYSTEM_PROMPT
    assert loaded.model == ""
    assert loaded.api_key == ""
    assert loaded.enable_thinking is False  # 思考模式默认关闭
    assert loaded.reasoning_effort == "none"  # 与 translator 默认常量一致


def test_reasoning_effort_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"reasoning_effort": "low"}), encoding="utf-8")
    assert config.load_config(target).reasoning_effort == "low"


def test_screenshot_compress_percent_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"screenshot_compress_percent": 25}), encoding="utf-8")
    assert config.load_config(target).screenshot_compress_percent == 25


def test_screenshot_compress_percent_out_of_range_falls_back(tmp_path):
    target = tmp_path / "config.json"
    for bad in (0, 101, -10, True, "10", None):
        target.write_text(json.dumps({"screenshot_compress_percent": bad}), encoding="utf-8")
        loaded = config.load_config(target)
        assert loaded.screenshot_compress_percent == 10, f"{bad!r} 应回退默认"


def test_screenshot_model_loaded_and_fallback(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"screenshot_model": "qwen-vl"}), encoding="utf-8")
    assert config.load_config(target).screenshot_model == "qwen-vl"
    target.write_text("{}", encoding="utf-8")  # 缺键：回退空串（运行时回退 model）
    assert config.load_config(target).screenshot_model == ""
    for bad in (123, None, ["vl"]):
        target.write_text(json.dumps({"screenshot_model": bad}), encoding="utf-8")
        assert config.load_config(target).screenshot_model == "", f"{bad!r}"


# ---------------------------------------------------------------- 听歌识曲


def test_recording_duration_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"recording_duration": 12}), encoding="utf-8")
    assert config.load_config(target).recording_duration == 12


def test_recording_duration_defaults_when_missing(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{}", encoding="utf-8")  # 缺键：回退默认 8 秒
    assert config.load_config(target).recording_duration == config.DEFAULT_RECORDING_DURATION


def test_recording_duration_invalid_types_fall_back(tmp_path):
    target = tmp_path / "config.json"
    for bad in ("8", True, 8.5, None, [8]):  # bool 不算数字、非整数浮点回退
        target.write_text(json.dumps({"recording_duration": bad}), encoding="utf-8")
        loaded = config.load_config(target)
        assert loaded.recording_duration == config.DEFAULT_RECORDING_DURATION, f"{bad!r}"


def test_recording_duration_below_minimum_falls_back(tmp_path):
    """低于下限 3 秒的采样难以命中指纹库，回退默认。"""
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"recording_duration": 2}), encoding="utf-8")
    assert config.load_config(target).recording_duration == config.DEFAULT_RECORDING_DURATION


def test_reasoning_effort_invalid_falls_back(tmp_path):
    target = tmp_path / "config.json"
    for bad in (123, True, None, ["low"]):
        target.write_text(json.dumps({"reasoning_effort": bad}), encoding="utf-8")
        assert config.load_config(target).reasoning_effort == "none", f"{bad!r}"


def test_enable_thinking_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"enable_thinking": True}), encoding="utf-8")
    assert config.load_config(target).enable_thinking is True


def test_enable_thinking_invalid_falls_back(tmp_path):
    target = tmp_path / "config.json"
    for bad in ("yes", 1, "false", None):
        target.write_text(json.dumps({"enable_thinking": bad}), encoding="utf-8")
        assert config.load_config(target).enable_thinking is False, f"{bad!r}"


def test_api_values_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "api_base_url": "https://api.deepseek.com",
                "api_timeout": 5.5,
                "system_prompt": "只输出译文",
                "model": "deepseek-chat",
                "api_key": "sk-test",
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.api_base_url == "https://api.deepseek.com"
    assert loaded.api_timeout == 5.5
    assert loaded.system_prompt == "只输出译文"
    assert loaded.model == "deepseek-chat"
    assert loaded.api_key == "sk-test"


def test_api_invalid_types_fall_back_per_field(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "api_base_url": 123,
                "api_timeout": True,  # bool 不得当数字
                "system_prompt": [],
                "model": 7,
                "api_key": None,
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.api_base_url == translator.DEFAULT_BASE_URL
    assert loaded.api_timeout == translator.DEFAULT_TIMEOUT
    assert loaded.system_prompt == translator.DEFAULT_SYSTEM_PROMPT
    assert loaded.model == ""
    assert loaded.api_key == ""


def test_api_base_url_empty_falls_back(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"api_base_url": "   "}), encoding="utf-8")
    assert config.load_config(target).api_base_url == translator.DEFAULT_BASE_URL


def test_api_timeout_non_positive_falls_back(tmp_path):
    target = tmp_path / "config.json"
    for bad in (0, -3.5):
        target.write_text(json.dumps({"api_timeout": bad}), encoding="utf-8")
        assert config.load_config(target).api_timeout == translator.DEFAULT_TIMEOUT, f"{bad!r}"


def test_translation_cache_size_kb_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"translation_cache_size_kb": 128}), encoding="utf-8")
    assert config.load_config(target).translation_cache_size_kb == 128


def test_translation_cache_size_kb_missing_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"auto_translate": True}), encoding="utf-8")
    assert config.load_config(target).translation_cache_size_kb == 256


def test_translation_cache_size_kb_invalid_falls_back(tmp_path):
    target = tmp_path / "config.json"
    for bad in ("big", 0, -5, True):
        target.write_text(json.dumps({"translation_cache_size_kb": bad}), encoding="utf-8")
        assert config.load_config(target).translation_cache_size_kb == 256, f"{bad!r}"


def test_valid_values_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {"auto_translate": True, "auto_translate_interval": 1.5, "show_original_text": False}
        ),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.auto_translate is True
    assert loaded.auto_translate_interval == 1.5
    assert loaded.show_original_text is False


def test_show_original_text_missing_defaults_to_true(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"auto_translate": True}), encoding="utf-8")
    assert config.load_config(target).show_original_text is True


def test_show_original_text_invalid_falls_back(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"show_original_text": "no"}), encoding="utf-8")
    assert config.load_config(target).show_original_text is True


def test_extra_keys_are_ignored(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"auto_translate": True, "future_option": 42}), encoding="utf-8")
    loaded = config.load_config(target)
    assert loaded.auto_translate is True
    assert loaded.auto_translate_interval == 3.0


# ---------------------------------------------------------------- 流式悬浮窗


def test_stream_window_defaults(tmp_path):
    target = tmp_path / "config.json"
    loaded = config.load_config(target)
    assert loaded.stream_window_font_size == 14
    assert loaded.stream_window_line_spacing == 1.45
    assert loaded.stream_window_title_font_size == 8
    assert loaded.stream_window_title_gap == 4


def test_stream_window_values_loaded(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "stream_window_font_size": 18,
                "stream_window_line_spacing": 1.2,
                "stream_window_title_font_size": 10,
                "stream_window_title_gap": 0,
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.stream_window_font_size == 18
    assert loaded.stream_window_line_spacing == 1.2
    assert loaded.stream_window_title_font_size == 10
    assert loaded.stream_window_title_gap == 0  # 间距允许 0


def test_stream_window_integer_float_accepted(tmp_path):
    """整值浮点（16.0）等价于整数 16；非整数浮点字号回退。"""
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"stream_window_font_size": 16.0}), encoding="utf-8")
    assert config.load_config(target).stream_window_font_size == 16
    target.write_text(json.dumps({"stream_window_font_size": 16.5}), encoding="utf-8")
    assert config.load_config(target).stream_window_font_size == 14


def test_stream_window_invalid_types_fall_back_per_field(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "stream_window_font_size": True,  # bool 不得当数字
                "stream_window_line_spacing": "big",
                "stream_window_title_font_size": None,
                "stream_window_title_gap": [2],
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.stream_window_font_size == 14
    assert loaded.stream_window_line_spacing == 1.45
    assert loaded.stream_window_title_font_size == 8
    assert loaded.stream_window_title_gap == 4


def test_stream_window_out_of_range_falls_back(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "stream_window_font_size": 3,  # 低于下限 6
                "stream_window_line_spacing": 0.5,  # 低于下限 1.0
                "stream_window_title_font_size": 0,
                "stream_window_title_gap": -2,
            }
        ),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.stream_window_font_size == 14
    assert loaded.stream_window_line_spacing == 1.45
    assert loaded.stream_window_title_font_size == 8
    assert loaded.stream_window_title_gap == 4


def test_stream_window_size_loaded_and_fallback(tmp_path):
    """正文窗尺寸：默认 1760x200；合法值生效；类型不符 / 越界逐项回退。"""
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"stream_window_width": 1000, "stream_window_height": 300}),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.stream_window_width == 1000
    assert loaded.stream_window_height == 300
    for bad in ("wide", True, 100.5):  # 类型不符（bool 不算数字、非整数浮点）
        target.write_text(json.dumps({"stream_window_width": bad}), encoding="utf-8")
        assert config.load_config(target).stream_window_width == 1760, f"{bad!r}"
    target.write_text(json.dumps({"stream_window_height": 40}), encoding="utf-8")
    assert config.load_config(target).stream_window_height == 200  # 低于下限 80
    target.write_text(json.dumps({"stream_window_width": 100}), encoding="utf-8")
    assert config.load_config(target).stream_window_width == 1760  # 低于下限 240


def test_invalid_json_falls_back_without_crash(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{ not valid json", encoding="utf-8")
    loaded = config.load_config(target)
    assert loaded == config.AppConfig()
    # 坏文件不应被覆盖，留待用户自行修复
    assert target.read_text(encoding="utf-8") == "{ not valid json"


def test_non_object_top_level_falls_back(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert config.load_config(target) == config.AppConfig()


def test_wrong_types_fall_back_per_field(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"auto_translate": "yes", "auto_translate_interval": -1}),
        encoding="utf-8",
    )
    loaded = config.load_config(target)
    assert loaded.auto_translate is False
    assert loaded.auto_translate_interval == 3.0


def test_bool_is_not_accepted_as_interval(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"auto_translate_interval": True}), encoding="utf-8")
    assert config.load_config(target).auto_translate_interval == 3.0


def test_tiny_interval_falls_back(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"auto_translate_interval": 0.01}), encoding="utf-8")
    assert config.load_config(target).auto_translate_interval == 3.0


def test_frozen_default_path_uses_exe_dir(tmp_path, monkeypatch):
    """PyInstaller 打包后（frozen）配置固定跟随 exe 所在目录。"""
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "renpy-overlay.exe"))
    assert config.default_path() == tmp_path / "config.json"


def test_unfrozen_default_path_falls_back_to_cwd(tmp_path, monkeypatch):
    """未打包且反推不到项目根（如安装到 site-packages）时回退当前工作目录。"""
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    # 伪造包文件位置：其上级没有 pyproject.toml，只能回退 cwd
    monkeypatch.setattr(
        config, "__file__", str(tmp_path / "site-packages" / "renpy_overlay" / "config.py")
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    assert config.default_path() == workdir / "config.json"
