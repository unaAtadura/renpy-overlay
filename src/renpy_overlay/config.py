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
      "reasoning_effort": "none",         # 关闭思考时的 reasoning_effort 取值
      "stream_window_width": 1760,        # 流式正文窗宽度（像素，被游戏窗口宽度钳制）
      "stream_window_height": 200,        # 流式正文窗高度（像素）
      "stream_window_font_size": 14,      # 流式正文窗字号（像素）
      "stream_window_line_spacing": 1.45, # 流式正文行距倍数
      "stream_window_title_font_size": 8, # 流式标题窗字号（像素）
      "stream_window_title_gap": 4,       # 标题窗与正文窗的间距（像素）
      "screenshot_compress_percent": 10,  # 截图翻译发送 API 前的等比压缩百分比（像素面积比）
      "screenshot_model": "",             # 截图识别翻译模型；空 = 回退 model（需 vision 能力）
      "recording_duration": 8,            # 听歌识曲录制系统音频的时长（秒）
      "hotkey_main": "ctrl+alt+p",        # 快捷键模式主开关键（全局热键，Ctrl+Alt+P）
      "hotkey_window_1": "1",             # 红色截图窗口的全局热键
      "hotkey_window_2": "2",             # 橙色截图窗口的全局热键
      "hotkey_window_3": "3",             # 黄色截图窗口的全局热键
      "hotkey_window_4": "4",             # 绿色截图窗口的全局热键
      "hotkey_window_5": "5",             # 青色截图窗口的全局热键
      "hotkey_window_6": "6",             # 蓝色截图窗口的全局热键
      "hotkey_window_7": "7",             # 紫色截图窗口的全局热键
      "hotkey_window_8": "8",             # 黑色截图窗口的全局热键
      "api_base_url_stanby": "",          # 备选 API 链路地址；留空即不启用备选链路
      "model_stanby": "",                 # 备选链路的文本模型（留空则自动发现）
      "api_key_stanby": "",               # 备选链路的 API Key（留空则不携带鉴权头）
      "screenshot_model_stanby": "",      # 备选链路的截图模型；留空回退 model_stanby
    }
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .screenshot.hotkeys import DEFAULT_MAIN_HOTKEY, DEFAULT_WINDOW_HOTKEYS, parse_hotkey
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
#: 正文窗尺寸：默认宽 = 历史 CLI 默认 880 的 2 倍，高与历史默认一致
DEFAULT_STREAM_WINDOW_WIDTH = 1760
DEFAULT_STREAM_WINDOW_HEIGHT = 200
DEFAULT_STREAM_WINDOW_FONT_SIZE = 14  # 正文窗字号（像素）
DEFAULT_STREAM_WINDOW_LINE_SPACING = 1.45  # 正文行距倍数（参考项目验证值）
DEFAULT_STREAM_WINDOW_TITLE_FONT_SIZE = 8  # 标题窗字号（像素）
DEFAULT_STREAM_WINDOW_TITLE_GAP = 4  # 标题窗与正文窗间距（像素）
#: 截图翻译：发送 API 前副本等比压缩到的像素面积百分比（默认 10%）
DEFAULT_SCREENSHOT_COMPRESS_PERCENT = 10
#: 截图识别翻译的模型代号；空 = 回退 model（识图需 vision 多模态模型）
DEFAULT_SCREENSHOT_MODEL = ""
#: 听歌识曲录制系统音频的时长（秒）：参考项目验证值
DEFAULT_RECORDING_DURATION = 8
#: 快捷键模式：主开关键默认 Ctrl+Alt+P，窗口键默认数字 1-8（红橙黄绿青蓝紫黑）
DEFAULT_HOTKEY_MAIN = DEFAULT_MAIN_HOTKEY
DEFAULT_HOTKEY_WINDOWS = DEFAULT_WINDOW_HOTKEYS
#: 备选 API 链路：主链路（api_base_url）不可达时自动切换；base_url 留空即不启用
DEFAULT_API_BASE_URL_STANBY = ""
DEFAULT_MODEL_STANBY = ""
DEFAULT_API_KEY_STANBY = ""
DEFAULT_SCREENSHOT_MODEL_STANBY = ""
#: 录制时长下限：过短的采样难以命中 Shazam 指纹库
MIN_RECORDING_DURATION = 3
#: 字号下限：再小就不可辨认；行距下限：小于 1.0 会上下行重叠
MIN_STREAM_FONT_SIZE = 6
MIN_STREAM_LINE_SPACING = 1.0
#: 正文窗尺寸下限：过小无法阅读（与 compute_geometry 的最小宽/高对齐）
MIN_STREAM_WINDOW_WIDTH = 240
MIN_STREAM_WINDOW_HEIGHT = 80
#: 轮询间隔的下限（秒）：过小的值会让 after 循环空转
MIN_AUTO_TRANSLATE_INTERVAL = 0.1


def resolve_screenshot_model(screenshot_model: str, model: str) -> str:
    """截图模型的退避规则（纯函数）：``screenshot_model`` 留空回退 ``model``。

    主链路与备选链路共用同一规则（备选侧传入各自的 stanby 配置对）。
    """
    return screenshot_model or model


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
    # 流式悬浮窗
    stream_window_width: int = DEFAULT_STREAM_WINDOW_WIDTH
    stream_window_height: int = DEFAULT_STREAM_WINDOW_HEIGHT
    stream_window_font_size: int = DEFAULT_STREAM_WINDOW_FONT_SIZE
    stream_window_line_spacing: float = DEFAULT_STREAM_WINDOW_LINE_SPACING
    stream_window_title_font_size: int = DEFAULT_STREAM_WINDOW_TITLE_FONT_SIZE
    stream_window_title_gap: int = DEFAULT_STREAM_WINDOW_TITLE_GAP
    # 截图翻译
    screenshot_compress_percent: int = DEFAULT_SCREENSHOT_COMPRESS_PERCENT
    screenshot_model: str = DEFAULT_SCREENSHOT_MODEL
    # 听歌识曲
    recording_duration: int = DEFAULT_RECORDING_DURATION
    # 截图翻译快捷键模式（主开关 + 8 个窗口键，均为全局热键）
    hotkey_main: str = DEFAULT_HOTKEY_MAIN
    hotkey_windows: tuple[str, ...] = DEFAULT_HOTKEY_WINDOWS
    # 备选 API 链路（主链路不可达时自动切换；base_url 留空即不启用）
    api_base_url_stanby: str = DEFAULT_API_BASE_URL_STANBY
    model_stanby: str = DEFAULT_MODEL_STANBY
    api_key_stanby: str = DEFAULT_API_KEY_STANBY
    screenshot_model_stanby: str = DEFAULT_SCREENSHOT_MODEL_STANBY


def default_path() -> Path:
    """默认配置文件路径：项目根目录（与 pyproject.toml 同级）。

    以包文件位置反推项目根（src/renpy_overlay/config.py -> 项目根），
    这样无论从哪个工作目录启动工具都会命中同一份配置；包被安装到
    site-packages 等无法反推的场景回退到当前工作目录。
    PyInstaller 打包后（frozen）包位于临时解包目录，反推无意义，
    固定使用 exe 所在目录，保证配置与 exe 放在一起。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / CONFIG_FILENAME
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
        "stream_window_width": DEFAULT_STREAM_WINDOW_WIDTH,
        "stream_window_height": DEFAULT_STREAM_WINDOW_HEIGHT,
        "stream_window_font_size": DEFAULT_STREAM_WINDOW_FONT_SIZE,
        "stream_window_line_spacing": DEFAULT_STREAM_WINDOW_LINE_SPACING,
        "stream_window_title_font_size": DEFAULT_STREAM_WINDOW_TITLE_FONT_SIZE,
        "stream_window_title_gap": DEFAULT_STREAM_WINDOW_TITLE_GAP,
        "screenshot_compress_percent": DEFAULT_SCREENSHOT_COMPRESS_PERCENT,
        "screenshot_model": DEFAULT_SCREENSHOT_MODEL,
        "recording_duration": DEFAULT_RECORDING_DURATION,
        "hotkey_main": DEFAULT_HOTKEY_MAIN,
        "api_base_url_stanby": DEFAULT_API_BASE_URL_STANBY,
        "model_stanby": DEFAULT_MODEL_STANBY,
        "api_key_stanby": DEFAULT_API_KEY_STANBY,
        "screenshot_model_stanby": DEFAULT_SCREENSHOT_MODEL_STANBY,
    }
    for index, hotkey in enumerate(DEFAULT_HOTKEY_WINDOWS, start=1):
        payload[f"hotkey_window_{index}"] = hotkey
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


def _read_positive_int(raw: dict, key: str, default: int, minimum: int) -> int:
    """通用正整数字段校验：类型不对或低于下限 → 回退默认值。"""
    value = raw.get(key, default)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        logger.warning("%s 不是整数（%r），回退默认 %d", key, value, default)
        return default
    if int(value) < minimum:
        logger.warning("%s 低于下限 %d（%r），回退默认 %d", key, minimum, value, default)
        return default
    return int(value)


def _read_non_negative_int(raw: dict, key: str, default: int) -> int:
    """通用非负整数字段校验（间距类配置允许 0）：类型不对或为负 → 回退默认值。"""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        logger.warning("%s 不是整数（%r），回退默认 %d", key, value, default)
        return default
    if int(value) < 0:
        logger.warning("%s 为负数（%r），回退默认 %d", key, value, default)
        return default
    return int(value)


def _read_positive_float(raw: dict, key: str, default: float, minimum: float) -> float:
    """通用正数字段校验（行距类配置）：类型不对或低于下限 → 回退默认值。"""
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("%s 不是数字（%r），回退默认 %s", key, value, default)
        return default
    if value < minimum:
        logger.warning("%s 低于下限 %s（%r），回退默认 %s", key, minimum, value, default)
        return default
    return float(value)


def _read_percent(raw: dict, key: str, default: int) -> int:
    """百分数字段校验（1..100）：类型不对或越界 → 回退默认值。"""
    value = raw.get(key, default)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        logger.warning("%s 不是整数（%r），回退默认 %d", key, value, default)
        return default
    if not 1 <= int(value) <= 100:
        logger.warning("%s 超出 1..100（%r），回退默认 %d", key, value, default)
        return default
    return int(value)


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


def _read_hotkey(raw: dict, key: str, default: str) -> str:
    """快捷键字段校验：类型不对或格式解析失败 → 回退默认值。"""
    value = _read_str(raw, key, default)
    if parse_hotkey(value) is None:
        logger.warning("%s 不是合法快捷键（%r），回退默认值 %r", key, value, default)
        return default
    return value


def _read_hotkeys(raw: dict) -> tuple[str, ...]:
    """窗口快捷键组（1-8 依次对应红橙黄绿青蓝紫黑）：逐项校验回退。"""
    return tuple(
        _read_hotkey(raw, f"hotkey_window_{index}", default)
        for index, default in enumerate(DEFAULT_HOTKEY_WINDOWS, start=1)
    )


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
        stream_window_width=_read_positive_int(
            raw, "stream_window_width", DEFAULT_STREAM_WINDOW_WIDTH, MIN_STREAM_WINDOW_WIDTH
        ),
        stream_window_height=_read_positive_int(
            raw, "stream_window_height", DEFAULT_STREAM_WINDOW_HEIGHT, MIN_STREAM_WINDOW_HEIGHT
        ),
        stream_window_font_size=_read_positive_int(
            raw, "stream_window_font_size", DEFAULT_STREAM_WINDOW_FONT_SIZE, MIN_STREAM_FONT_SIZE
        ),
        stream_window_line_spacing=_read_positive_float(
            raw,
            "stream_window_line_spacing",
            DEFAULT_STREAM_WINDOW_LINE_SPACING,
            MIN_STREAM_LINE_SPACING,
        ),
        stream_window_title_font_size=_read_positive_int(
            raw,
            "stream_window_title_font_size",
            DEFAULT_STREAM_WINDOW_TITLE_FONT_SIZE,
            MIN_STREAM_FONT_SIZE,
        ),
        stream_window_title_gap=_read_non_negative_int(
            raw, "stream_window_title_gap", DEFAULT_STREAM_WINDOW_TITLE_GAP
        ),
        screenshot_compress_percent=_read_percent(
            raw, "screenshot_compress_percent", DEFAULT_SCREENSHOT_COMPRESS_PERCENT
        ),
        screenshot_model=_read_str(raw, "screenshot_model", DEFAULT_SCREENSHOT_MODEL),
        recording_duration=_read_positive_int(
            raw, "recording_duration", DEFAULT_RECORDING_DURATION, MIN_RECORDING_DURATION
        ),
        hotkey_main=_read_hotkey(raw, "hotkey_main", DEFAULT_HOTKEY_MAIN),
        hotkey_windows=_read_hotkeys(raw),
        api_base_url_stanby=_read_str(
            raw, "api_base_url_stanby", DEFAULT_API_BASE_URL_STANBY
        ),
        model_stanby=_read_str(raw, "model_stanby", DEFAULT_MODEL_STANBY),
        api_key_stanby=_read_str(raw, "api_key_stanby", DEFAULT_API_KEY_STANBY),
        screenshot_model_stanby=_read_str(
            raw, "screenshot_model_stanby", DEFAULT_SCREENSHOT_MODEL_STANBY
        ),
    )
    logger.info(
        "配置已加载：auto_translate=%s，interval=%.1fs，show_original_text=%s，"
        "cache_size=%dKB，api_base_url=%s，api_timeout=%.1fs，model=%s，"
        "system_prompt=%d 字，api_key=%s，enable_thinking=%s，reasoning_effort=%r，"
        "stream_window（size=%dx%d，font=%d，spacing=%.2f，title_font=%d，gap=%d），"
        "screenshot（compress=%d%%，model=%s），recording_duration=%ds（%s），"
        "hotkeys（main=%r，windows=%r），"
        "stanby（base_url=%s，model=%r，api_key=%s，screenshot_model=%r）",
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
        app_config.stream_window_width,
        app_config.stream_window_height,
        app_config.stream_window_font_size,
        app_config.stream_window_line_spacing,
        app_config.stream_window_title_font_size,
        app_config.stream_window_title_gap,
        app_config.screenshot_compress_percent,
        app_config.screenshot_model or "<回退 model>",
        app_config.recording_duration,
        target,
        app_config.hotkey_main,
        app_config.hotkey_windows,
        app_config.api_base_url_stanby or "<未启用>",
        app_config.model_stanby,
        "已设置" if app_config.api_key_stanby else "未设置",
        app_config.screenshot_model_stanby or "<回退 model_stanby>",
    )
    return app_config
