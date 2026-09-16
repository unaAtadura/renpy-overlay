"""本地配置（config.json）：位于项目根目录，首次运行时自动创建。

设计原则：配置问题绝不导致工具崩溃 —— 文件缺失时写出默认值；读取失败、
JSON 非法、字段类型不对时逐项回退默认值并记录日志（不回写用户的坏文件，
以免覆盖手工编辑的内容）。

当前配置项（与 ``AppConfig`` 字段一一对应）::

    {
      "auto_translate": false,            # 自动翻译开关（仅悬浮窗锁定状态生效）
      "auto_translate_interval": 3.0,     # 自动翻译轮询间隔（秒）
      "show_original_text": true,         # 悬浮窗正文区是否随对话显示游戏原文
      "translation_cache_size_kb": 256,    # 内存翻译缓存上限（KB）
      "api_base_url": "http://127.0.0.1:1234",  # OpenAI 兼容 API 地址
      "api_timeout": 20.0,                # 请求超时（秒）
      "model": "",                        # 模型代号；空 = 自动用 /v1/models 的第一个
      "system_prompt": "...",             # 系统提示词（默认见 translator.DEFAULT_SYSTEM_PROMPT）
      "api_key": "",                      # API Key；空 = 不携带鉴权头
      "enable_thinking": false,           # 模型思考（reasoning）模式开关，默认关闭
      "reasoning_effort": "none"          # 关闭思考时的 reasoning_effort 取值
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .translator import (
    DEFAULT_BASE_URL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TIMEOUT,
)

logger = logging.getLogger("renpy_overlay.config")

CONFIG_FILENAME = "config.json"

DEFAULT_AUTO_TRANSLATE = False
DEFAULT_AUTO_TRANSLATE_INTERVAL = 3.0
DEFAULT_SHOW_ORIGINAL_TEXT = True
DEFAULT_TRANSLATION_CACHE_SIZE_KB = 256
DEFAULT_MODEL = ""  # 空：自动向 /v1/models 查询第一个已加载模型
DEFAULT_API_KEY = ""  # 空：不携带 Authorization 头
DEFAULT_ENABLE_THINKING = False  # 模型思考（reasoning）模式默认关闭
#: 轮询间隔的下限（秒）：过小的值会让 after 循环空转
MIN_AUTO_TRANSLATE_INTERVAL = 0.1


@dataclass(frozen=True)
class AppConfig:
    """config.json 的解析结果（自动翻译 / 显示 / 缓存 / OpenAI 兼容 API 参数）。"""

    auto_translate: bool = DEFAULT_AUTO_TRANSLATE
    auto_translate_interval: float = DEFAULT_AUTO_TRANSLATE_INTERVAL
    show_original_text: bool = DEFAULT_SHOW_ORIGINAL_TEXT
    translation_cache_size_kb: int = DEFAULT_TRANSLATION_CACHE_SIZE_KB
    api_base_url: str = DEFAULT_BASE_URL
    api_timeout: float = DEFAULT_TIMEOUT
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    api_key: str = DEFAULT_API_KEY
    enable_thinking: bool = DEFAULT_ENABLE_THINKING
    reasoning_effort: str = DEFAULT_REASONING_EFFORT


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
        "api_base_url": DEFAULT_BASE_URL,
        "api_timeout": DEFAULT_TIMEOUT,
        "model": DEFAULT_MODEL,
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "api_key": DEFAULT_API_KEY,
        "enable_thinking": DEFAULT_ENABLE_THINKING,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
    }
    try:
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:  # 目录只读等场景：不影响工具运行
        logger.warning("创建默认配置失败（%s）：%s", target, exc)
        return
    logger.info("首次运行：已创建默认配置 %s", target)


def _read_bool(raw: dict, key: str, default: bool) -> bool:
    """通用布尔字段校验：类型不对 → 回退默认值。"""
    value = raw.get(key, default)
    if not isinstance(value, bool):
        logger.warning("%s 不是布尔值（%r），回退默认 %s", key, value, default)
        return default
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


def _read_str(raw: dict, key: str, default: str, allow_empty: bool = True) -> str:
    """通用字符串字段校验：类型不对或（不允许空时）空串 → 回退默认值。"""
    value = raw.get(key, default)
    if not isinstance(value, str):
        logger.warning("%s 不是字符串（%r），回退默认值", key, value)
        return default
    if not allow_empty and not value.strip():
        logger.warning("%s 为空字符串，回退默认值 %r", key, default)
        return default
    return value


def _read_api_timeout(raw: dict) -> float:
    value = raw.get("api_timeout", DEFAULT_TIMEOUT)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("api_timeout 不是数字（%r），回退默认 %.1f", value, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    if value <= 0:
        logger.warning("api_timeout 非正数（%r），回退默认 %.1f", value, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return float(value)


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
        auto_translate=_read_bool(raw, "auto_translate", DEFAULT_AUTO_TRANSLATE),
        auto_translate_interval=_read_interval(raw),
        show_original_text=_read_bool(raw, "show_original_text", DEFAULT_SHOW_ORIGINAL_TEXT),
        translation_cache_size_kb=_read_cache_size_kb(raw),
        api_base_url=_read_str(raw, "api_base_url", DEFAULT_BASE_URL, allow_empty=False),
        api_timeout=_read_api_timeout(raw),
        system_prompt=_read_str(raw, "system_prompt", DEFAULT_SYSTEM_PROMPT),
        model=_read_str(raw, "model", DEFAULT_MODEL),
        api_key=_read_str(raw, "api_key", DEFAULT_API_KEY),
        enable_thinking=_read_bool(raw, "enable_thinking", DEFAULT_ENABLE_THINKING),
        reasoning_effort=_read_str(raw, "reasoning_effort", DEFAULT_REASONING_EFFORT),
    )
    logger.info(
        "配置已加载：auto_translate=%s，interval=%.1fs，show_original_text=%s，"
        "cache_size=%dKB，api_base_url=%s，api_timeout=%.1fs，model=%s，"
        "system_prompt=%d 字，api_key=%s，enable_thinking=%s，reasoning_effort=%r（%s）",
        app_config.auto_translate,
        app_config.auto_translate_interval,
        app_config.show_original_text,
        app_config.translation_cache_size_kb,
        app_config.api_base_url,
        app_config.api_timeout,
        app_config.model or "<自动取 /v1/models 第一个>",
        len(app_config.system_prompt),
        "已设置" if app_config.api_key else "未设置",
        app_config.enable_thinking,
        app_config.reasoning_effort,
        target,
    )
    return app_config
