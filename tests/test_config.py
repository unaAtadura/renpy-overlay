"""本地配置加载的离线验证：首建默认值、非法内容回退、合法值读取。"""

from __future__ import annotations

import json

from renpy_overlay import config


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
    }


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
