"""本地配置（config.json）：位于项目根目录，首次运行时自动创建。

设计原则：配置问题绝不导致工具崩溃 —— 文件缺失时写出默认值；读取失败、
JSON 非法、字段类型不对时逐项回退默认值并记录日志（不回写用户的坏文件，
以免覆盖手工编辑的内容）。

当前配置项（与 ``AppConfig`` 字段一一对应）::

    {
      "auto_translate": false,            # 自动翻译开关（仅悬浮窗锁定状态生效）
      "auto_translate_interval": 3.0,     # 自动翻译轮询间隔（秒）
      "show_original_text": true,         # 悬浮窗正文区是否随对话显示游戏原文
      "translation_cache_size_kb": 256     # 内存翻译缓存上限（KB）
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renpy_overlay.config")

CONFIG_FILENAME = "config.json"

DEFAULT_AUTO_TRANSLATE = False
DEFAULT_AUTO_TRANSLATE_INTERVAL = 3.0
DEFAULT_SHOW_ORIGINAL_TEXT = True
DEFAULT_TRANSLATION_CACHE_SIZE_KB = 256
#: 轮询间隔的下限（秒）：过小的值会让 after 循环空转
MIN_AUTO_TRANSLATE_INTERVAL = 0.1


@dataclass(frozen=True)
class AppConfig:
    """config.json 的解析结果（自动翻译 + 正文原文显示 + 缓存容量）。"""

    auto_translate: bool = DEFAULT_AUTO_TRANSLATE
    auto_translate_interval: float = DEFAULT_AUTO_TRANSLATE_INTERVAL
    show_original_text: bool = DEFAULT_SHOW_ORIGINAL_TEXT
    translation_cache_size_kb: int = DEFAULT_TRANSLATION_CACHE_SIZE_KB


def default_path() -> Path:
    """默认配置文件路径：项目根目录（与 pyproject.toml 同级）。

    以包文件位置反推项目根（src/renpy_overlay/config.py -> 项目根），
    这样无论从哪个工作目录启动工具都会命中同一份配置；包被安装到
    site-packages 等无法反推的场景回退到当前工作目录。
    """
    package_root = Path(__file__).resolve().parents[2]
    if (package_root / "pyproject.toml").is_file():
        return package_root / CONFIG_FILENAME
    return Path.cwd() / CONFIG_FILENAME


def _write_defaults(target: Path) -> None:
    payload = {
        "auto_translate": DEFAULT_AUTO_TRANSLATE,
        "auto_translate_interval": DEFAULT_AUTO_TRANSLATE_INTERVAL,
        "show_original_text": DEFAULT_SHOW_ORIGINAL_TEXT,
        "translation_cache_size_kb": DEFAULT_TRANSLATION_CACHE_SIZE_KB,
    }
    try:
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # 目录只读等场景：不影响工具运行
        logger.warning("创建默认配置失败（%s）：%s", target, exc)
        return
    logger.info("首次运行：已创建默认配置 %s", target)


def _read_auto_translate(raw: dict) -> bool:
    value = raw.get("auto_translate", DEFAULT_AUTO_TRANSLATE)
    if not isinstance(value, bool):
        logger.warning(
            "auto_translate 不是布尔值（%r），回退默认 %s", value, DEFAULT_AUTO_TRANSLATE
        )
        return DEFAULT_AUTO_TRANSLATE
    return value


def _read_show_original_text(raw: dict) -> bool:
    value = raw.get("show_original_text", DEFAULT_SHOW_ORIGINAL_TEXT)
    if not isinstance(value, bool):
        logger.warning(
            "show_original_text 不是布尔值（%r），回退默认 %s",
            value,
            DEFAULT_SHOW_ORIGINAL_TEXT,
        )
        return DEFAULT_SHOW_ORIGINAL_TEXT
    return value


def _read_cache_size_kb(raw: dict) -> int:
    value = raw.get("translation_cache_size_kb", DEFAULT_TRANSLATION_CACHE_SIZE_KB)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning(
            "translation_cache_size_kb 不是数字（%r），回退默认 %d",
            value,
            DEFAULT_TRANSLATION_CACHE_SIZE_KB,
        )
        return DEFAULT_TRANSLATION_CACHE_SIZE_KB
    if value <= 0:
        logger.warning(
            "translation_cache_size_kb 非正数（%r），回退默认 %d",
            value,
            DEFAULT_TRANSLATION_CACHE_SIZE_KB,
        )
        return DEFAULT_TRANSLATION_CACHE_SIZE_KB
    return int(value)


def _read_interval(raw: dict) -> float:
    value = raw.get("auto_translate_interval", DEFAULT_AUTO_TRANSLATE_INTERVAL)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning(
            "auto_translate_interval 不是数字（%r），回退默认 %.1f",
            value,
            DEFAULT_AUTO_TRANSLATE_INTERVAL,
        )
        return DEFAULT_AUTO_TRANSLATE_INTERVAL
    if value < MIN_AUTO_TRANSLATE_INTERVAL:
        logger.warning(
            "auto_translate_interval 过小（%r），回退默认 %.1f",
            value,
            DEFAULT_AUTO_TRANSLATE_INTERVAL,
        )
        return DEFAULT_AUTO_TRANSLATE_INTERVAL
    return float(value)


def load_config(path: Path | None = None) -> AppConfig:
    """读取配置；文件不存在时创建默认值。任何异常都回退默认值并记日志。"""
    target = Path(path) if path is not None else default_path()
    if not target.is_file():
        _write_defaults(target)
        return AppConfig()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取配置失败（%s），回退默认值：%s", target, exc)
        return AppConfig()
    if not isinstance(raw, dict):
        logger.warning("配置文件顶层不是对象（%s），回退默认值", target)
        return AppConfig()

    app_config = AppConfig(
        auto_translate=_read_auto_translate(raw),
        auto_translate_interval=_read_interval(raw),
        show_original_text=_read_show_original_text(raw),
        translation_cache_size_kb=_read_cache_size_kb(raw),
    )
    logger.info(
        "配置已加载：auto_translate=%s，interval=%.1fs，show_original_text=%s，"
        "cache_size=%dKB（%s）",
        app_config.auto_translate,
        app_config.auto_translate_interval,
        app_config.show_original_text,
        app_config.translation_cache_size_kb,
        target,
    )
    return app_config
