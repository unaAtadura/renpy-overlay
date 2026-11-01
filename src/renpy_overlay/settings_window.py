"""设置窗口：双栏布局（左功能栏目列表 + 右该栏目设置项），编辑 config.json。

设计要点（需求）：

- **收归统一**：预构建翻译缓存等全部功能的设置统一在本窗口编辑；
  窗口不自带设置入口（需求：预构建窗口无设置按钮）；
- **同键多栏共用**：多个功能都依赖的键（如 LLM API 主链路四键）在多个
  栏目中复现（同一份 :class:`SettingSpec`），它们共用同一份 config.json；
- **切换栏目重读**：每次切换栏目都重新读取 config.json 刷新右侧控件，
  同步在其他栏目中所做的改动，避免互相覆盖；
- **改动即写盘**：控件值变更经轻量校验后合并写盘（``config.update_config_keys``，
  只改对应键、保留未展示键与手工编辑内容）；下次启动由 ``load_config``
  逐项严格校验兜底；
- **确定按钮**：手动触发宿主 ``on_reload`` 回调（运行时热重载全部已加载
  配置的模块）；回调返回错误字符串即提示失败且不破坏原配置；
- **需重启生效**：无法安全热应用的键（窗口样式 / 全屏渲染路径 / 内存缓存
  容量）在元数据中标注 ``restart=True``，设置名带「（需重启）」后缀。

纯函数层（栏目表 / 键规格 / 值转换）不依赖 Qt，离线单测覆盖（见
``tests/test_settings_window.py``）：元数据完整性、键冲突优先级（同键
多栏同 spec）、切换栏目重读、合并写盘不丢未知键。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import config

logger = logging.getLogger("renpy_overlay.settings_window")

#: 控件类型常量（``SettingSpec.kind``）
KIND_BOOL = "bool"          # QCheckBox 开关
KIND_INT = "int"            # QSpinBox（范围 = config 校验范围）
KIND_FLOAT = "float"        # QDoubleSpinBox
KIND_STR = "str"            # QLineEdit（API 地址 / 模型 / Key / 快捷键）
KIND_TEXT = "text"          # QPlainTextEdit（系统提示词）
KIND_ENUM = "enum"          # QComboBox（choices）
KIND_LANGS = "langs"        # QLineEdit 逗号分隔语言列表


@dataclass(frozen=True)
class SettingSpec:
    """单个配置键的界面元数据（键名 / 中文名 / 控件类型 / 范围 / 说明）。"""

    key: str                    # config.json 键名（写入与读取的唯一定位）
    label: str                  # 中文设置名
    kind: str                   # KIND_* 控件类型
    default: object             # 默认值（config 模块常量，缺键/回退时显示）
    minimum: float | None = None    # 数值下限（与 config 校验范围一致）
    maximum: float | None = None    # 数值上限
    choices: tuple[str, ...] = ()   # KIND_ENUM 的可选项
    note: str = ""                  # 取值说明（界面 tooltip + 测试文档）
    restart: bool = False           # True = 无法安全热应用，需重启生效
    hidden: bool = False            # True = 不在界面展示（记忆型键）


# ------------------------------------------------------------------ 键规格表
# 默认值全部引用 config 模块常量（单一事实来源，改动即时同步）。


def _api_main_specs() -> tuple[SettingSpec, ...]:
    """LLM API 主链路四键 + 思考模式（翻译与预构建栏目共用同一份 spec）。"""
    return (
        SettingSpec(
            "api_base_url", "API 地址", KIND_STR, config.DEFAULT_BASE_URL,
            note="OpenAI 兼容接口地址（翻译 / 预构建 / 备用链路的主入口）",
        ),
        SettingSpec(
            "api_timeout", "超时（秒）", KIND_FLOAT, config.DEFAULT_API_TIMEOUT,
            minimum=1.0, maximum=600.0,
            note="单次请求超时；预构建并发任务同用此值",
        ),
        SettingSpec(
            "model", "模型代号", KIND_STR, config.DEFAULT_MODEL,
            note="留空 = 自动向 /v1/models 发现第一个已加载模型",
        ),
        SettingSpec(
            "api_key", "API Key", KIND_STR, config.DEFAULT_API_KEY,
            note="留空 = 不携带鉴权头",
        ),
        SettingSpec(
            "enable_thinking", "启用模型思考（reasoning）", KIND_BOOL,
            config.DEFAULT_ENABLE_THINKING,
            note="开启后显著变慢，默认关闭",
        ),
        SettingSpec(
            "reasoning_effort", "reasoning_effort", KIND_STR,
            config.DEFAULT_REASONING_EFFORT,
            note="关闭思考时发送的取值（LM Studio 靠它真正关闭思考）",
        ),
    )


#: 跨栏目复用的键组必须为同一实例集合（键冲突优先级测试钉住身份一致）
_API_MAIN_SPECS = _api_main_specs()
#: 流式窗口样式键：运行中的双窗单例持有字体与几何，全部标需重启生效
_STREAM_SIZE_SPECS = (
    SettingSpec(
        "stream_window_width", "正文窗宽度（像素）", KIND_INT,
        config.DEFAULT_STREAM_WINDOW_WIDTH,
        minimum=config.MIN_STREAM_WINDOW_WIDTH, maximum=7680,
        note="实际显示宽度还会被游戏窗口宽度钳制；修改后需重启生效",
        restart=True,
    ),
    SettingSpec(
        "stream_window_height", "正文窗高度（像素）", KIND_INT,
        config.DEFAULT_STREAM_WINDOW_HEIGHT,
        minimum=config.MIN_STREAM_WINDOW_HEIGHT, maximum=2160,
        note="修改后需重启生效",
        restart=True,
    ),
)


#: 全部设置项（按栏目分组；同一 SettingSpec 实例可出现在多个栏目）
_SECTIONS: tuple[tuple[str, tuple[SettingSpec, ...]], ...] = (
    (
        "翻译",
        (
            SettingSpec(
                "auto_translate", "自动翻译", KIND_BOOL, config.DEFAULT_AUTO_TRANSLATE,
                note="锁定状态下按间隔自动翻译最新对话",
            ),
            SettingSpec(
                "auto_translate_interval", "自动翻译间隔（秒）", KIND_FLOAT,
                config.DEFAULT_AUTO_TRANSLATE_INTERVAL,
                minimum=config.MIN_AUTO_TRANSLATE_INTERVAL, maximum=60.0,
                note="过小的值会让轮询空转（下限 0.1 秒）",
            ),
            SettingSpec(
                "show_original_text", "正文区显示游戏原文", KIND_BOOL,
                config.DEFAULT_SHOW_ORIGINAL_TEXT,
                note="开启后正文区随对话显示原文（译文照常）",
            ),
            SettingSpec(
                "translation_cache_size_kb", "内存翻译缓存上限（KB）", KIND_INT,
                config.DEFAULT_TRANSLATION_CACHE_SIZE_KB,
                minimum=1, maximum=1048576,
                note="FIFO 分桶淘汰；修改后需重启生效", restart=True,
            ),
            *_API_MAIN_SPECS,
            SettingSpec(
                "system_prompt", "系统提示词", KIND_TEXT,
                config.DEFAULT_SYSTEM_PROMPT,
                note="只输出译文的指令默认即可满足多数场景",
            ),
        ),
    ),
    (
        "预构建翻译缓存",
        (
            SettingSpec(
                "max_concurrency", "并发上限", KIND_INT, config.DEFAULT_MAX_CONCURRENCY,
                minimum=1, maximum=config.MAX_CONCURRENCY_LIMIT,
                note="自适应并发的在途请求数上限（1..64）",
            ),
            SettingSpec(
                "initial_concurrency", "起步并发", KIND_INT, config.DEFAULT_INITIAL_CONCURRENCY,
                minimum=1, maximum=config.MAX_CONCURRENCY_LIMIT,
                note="本轮保守起步值，按服务端处理速度自动爬坡",
            ),
            SettingSpec(
                "concurrency_increase_after", "连成功多少条后 +1", KIND_INT,
                config.DEFAULT_CONCURRENCY_INCREASE_AFTER, minimum=1, maximum=1000,
            ),
            SettingSpec(
                "latency_guard_seconds", "时延闸门（秒）", KIND_FLOAT,
                config.DEFAULT_LATENCY_GUARD_SECONDS, minimum=1.0, maximum=600.0,
                note="近期成功时延中位数超过此值不再升档",
            ),
            SettingSpec(
                "circuit_break_threshold", "连续不可达熔断阈值", KIND_INT,
                config.DEFAULT_CIRCUIT_BREAK_THRESHOLD, minimum=1, maximum=100,
                note="连续 N 条端点不可达判定服务离线，终止本轮",
            ),
            SettingSpec(
                "retries", "单条失败重试次数", KIND_INT, config.DEFAULT_RETRIES,
                minimum=0, maximum=config.MAX_RETRIES_LIMIT,
            ),
            SettingSpec(
                "include_tl_strings", "扫描 game/tl/ 翻译文件", KIND_BOOL,
                config.DEFAULT_INCLUDE_TL_STRINGS,
                note="把 tl 下的翻译文件纳入扫描（translate strings 块）",
            ),
            SettingSpec(
                "tl_langs", "tl 语言过滤", KIND_LANGS, config.DEFAULT_TL_LANGS,
                note="逗号分隔语言；留空 = 全部语言",
            ),
            *_API_MAIN_SPECS,
        ),
    ),
    (
        "视觉/识图",
        (
            SettingSpec(
                "api_base_url_vision", "视觉 API 地址", KIND_STR,
                config.DEFAULT_API_BASE_URL_VISION,
                note="留空 = 跟随主链路 api_base_url",
            ),
            SettingSpec(
                "model_vision", "视觉模型", KIND_STR, config.DEFAULT_MODEL_VISION,
                note="留空 = 自动向 /v1/models 发现首个可用模型",
            ),
            SettingSpec(
                "api_key_vision", "视觉 API Key", KIND_STR, config.DEFAULT_API_KEY_VISION,
                note="留空 = 不携带鉴权头",
            ),
            SettingSpec(
                "screenshot_compress_percent", "截图压缩百分比", KIND_INT,
                config.DEFAULT_SCREENSHOT_COMPRESS_PERCENT, minimum=1, maximum=100,
                note="发送 API 前的等比压缩（像素面积比，越小越省流量）",
            ),
        ),
    ),
    (
        "语音识别（ASR）",
        (
            SettingSpec(
                "api_base_url_asr", "ASR API 地址", KIND_STR, config.DEFAULT_API_BASE_URL_ASR,
                note="留空 = 跟随主链路 api_base_url",
            ),
            SettingSpec(
                "model_asr", "ASR 模型", KIND_STR, config.DEFAULT_MODEL_ASR,
                note="留空 = 跟随主链路 model",
            ),
            SettingSpec(
                "api_key_asr", "ASR API Key", KIND_STR, config.DEFAULT_API_KEY_ASR,
                note="留空 = 跟随主链路 api_key",
            ),
        ),
    ),
    (
        "实时语音识别",
        (
            SettingSpec(
                "api_base_url_asr_rt", "实时 ASR API 地址", KIND_STR,
                config.DEFAULT_API_BASE_URL_ASR_RT,
                note="不回退其它链路；留空即实时识别不可用",
            ),
            SettingSpec(
                "model_asr_rt", "实时 ASR 模型", KIND_STR, config.DEFAULT_MODEL_ASR_RT,
                note="不回退其它链路；留空即不可用",
            ),
            SettingSpec(
                "api_key_asr_rt", "实时 ASR API Key", KIND_STR, config.DEFAULT_API_KEY_ASR_RT,
                note="不回退其它链路；留空即不可用",
            ),
        ),
    ),
    (
        "截图翻译与快捷键",
        (
            SettingSpec(
                "hotkey_main", "快捷键模式主开关", KIND_STR, config.DEFAULT_HOTKEY_MAIN,
                note="全局热键（如 alt+t）；不含 Ctrl 以规避 Ren'Py 持续快进",
            ),
            *tuple(
                SettingSpec(
                    f"hotkey_window_{index}", f"截图窗口 {index} 热键", KIND_STR, default
                )
                for index, default in enumerate(config.DEFAULT_HOTKEY_WINDOWS, start=1)
            ),
            SettingSpec(
                "hotkey_mouse_escape", "锁鼠标逃脱热键", KIND_STR,
                config.DEFAULT_HOTKEY_MOUSE_ESCAPE,
                note="锁鼠标区域开启期间全程注册的保底退出方式",
            ),
        ),
    ),
    (
        "遥控截图",
        (
            SettingSpec(
                "remote_screenshot_enable", "遥控截图开关", KIND_BOOL,
                config.DEFAULT_REMOTE_SCREENSHOT_ENABLE,
                note="快捷键模式期间生成 A/B 触发/瞄准小圆窗；修改后需重启生效",
                restart=True,
            ),
            SettingSpec(
                "remote_screenshot_interval", "自动化截图间隔（秒）", KIND_FLOAT,
                config.DEFAULT_REMOTE_SCREENSHOT_INTERVAL,
                minimum=config.MIN_REMOTE_SCREENSHOT_INTERVAL, maximum=60.0,
                note="修改后需重启生效", restart=True,
            ),
            SettingSpec(
                "remote_cache_size_count", "自动化缓存条数", KIND_INT,
                config.DEFAULT_REMOTE_CACHE_SIZE_COUNT,
                minimum=config.MIN_REMOTE_CACHE_SIZE_COUNT, maximum=999,
                note="感知签名↔译文的内存循环队列容量；修改后需重启生效",
                restart=True,
            ),
            SettingSpec(
                "remote_screenshot_diameterA", "触发器（A）直径", KIND_INT,
                config.DEFAULT_REMOTE_SCREENSHOT_DIAMETER,
                minimum=config.MIN_REMOTE_SCREENSHOT_DIAMETER, maximum=500,
                note="低于 20 像素无法瞄准，回退默认；修改后需重启生效",
                restart=True,
            ),
            SettingSpec(
                "remote_screenshot_diameterB", "瞄准器（B）直径", KIND_INT,
                config.DEFAULT_REMOTE_SCREENSHOT_DIAMETER,
                minimum=config.MIN_REMOTE_SCREENSHOT_DIAMETER, maximum=500,
                note="修改后需重启生效", restart=True,
            ),
        ),
    ),
    (
        "听歌识曲",
        (
            SettingSpec(
                "recording_duration", "录制时长（秒）", KIND_INT,
                config.DEFAULT_RECORDING_DURATION,
                minimum=config.MIN_RECORDING_DURATION, maximum=60,
                note="低于 3 秒的采样难以命中指纹库",
            ),
        ),
    ),
    (
        "视频录制与剪辑",
        (
            SettingSpec(
                "fullscreen_mode", "全屏渲染路径", KIND_ENUM, config.DEFAULT_FULLSCREEN_MODE,
                choices=("sdl", "qt"),
                note="sdl=SDL 独立窗口（更稳）/ qt=Qt 顶层回退；修改后需重启生效",
                restart=True,
            ),
        ),
    ),
    (
        "流式窗口样式",
        (
            *_STREAM_SIZE_SPECS,
            SettingSpec(
                "stream_window_font_size", "正文字号（像素）", KIND_INT,
                config.DEFAULT_STREAM_WINDOW_FONT_SIZE,
                minimum=config.MIN_STREAM_FONT_SIZE, maximum=96,
                note="修改后需重启生效", restart=True,
            ),
            SettingSpec(
                "stream_window_line_spacing", "正文行距倍数", KIND_FLOAT,
                config.DEFAULT_STREAM_WINDOW_LINE_SPACING,
                minimum=config.MIN_STREAM_LINE_SPACING, maximum=3.0,
                note="小于 1.0 会上下行重叠；修改后需重启生效", restart=True,
            ),
            SettingSpec(
                "stream_window_title_font_size", "标题字号（像素）", KIND_INT,
                config.DEFAULT_STREAM_WINDOW_TITLE_FONT_SIZE,
                minimum=config.MIN_STREAM_FONT_SIZE, maximum=48,
                note="修改后需重启生效", restart=True,
            ),
            SettingSpec(
                "stream_window_title_gap", "标题与正文间距（像素）", KIND_INT,
                config.DEFAULT_STREAM_WINDOW_TITLE_GAP, minimum=0, maximum=100,
                note="修改后需重启生效", restart=True,
            ),
            SettingSpec(
                "translate_window_style", "悬浮窗样式", KIND_ENUM,
                config.DEFAULT_TRANSLATE_WINDOW_STYLE, choices=("1", "2"),
                note="1=透明双窗（默认）；2=半透明黑色悬浮窗；修改后需重启生效",
                restart=True,
            ),
        ),
    ),
    (
        "AI 对话窗",
        (
            SettingSpec(
                "chat_window_width", "对话窗宽度（像素）", KIND_INT,
                config.DEFAULT_CHAT_WINDOW_WIDTH,
                minimum=config.MIN_STREAM_WINDOW_WIDTH, maximum=7680,
                note="缺省时回退正文窗宽度；修改后需重启生效", restart=True,
            ),
            SettingSpec(
                "chat_window_height", "对话窗高度（像素）", KIND_INT,
                config.DEFAULT_CHAT_WINDOW_HEIGHT,
                minimum=config.MIN_STREAM_WINDOW_HEIGHT, maximum=2160,
                note="缺省时回退正文窗高度；修改后需重启生效", restart=True,
            ),
        ),
    ),
)

#: 不在界面展示的键（记忆型 / 系统维护）：键冲突优先级测试钉住其存在
_HIDDEN_KEYS: frozenset[str] = frozenset({"last_game_dir"})


# ------------------------------------------------------------------ 纯函数层


def section_names() -> list[str]:
    """栏目名列表（顺序即界面显示顺序）。"""
    return [name for name, _specs in _SECTIONS]


def specs_for_section(name: str) -> tuple[SettingSpec, ...]:
    """指定栏目的键规格（未知栏目返回空元组）。"""
    for section, specs in _SECTIONS:
        if section == name:
            return specs
    return ()


def all_sections() -> tuple[tuple[str, tuple[SettingSpec, ...]], ...]:
    """完整栏目表（测试遍历用）。"""
    return _SECTIONS


def visible_spec_keys() -> set[str]:
    """全部界面展示的键集合（跨栏目去重）。"""
    return {spec.key for _name, specs in _SECTIONS for spec in specs}


def hidden_spec_keys() -> set[str]:
    """不展示的维护型键集合。"""
    return set(_HIDDEN_KEYS)


def spec_by_key(key: str) -> SettingSpec | None:
    """按键名查规格（同键多栏为同一 spec 实例；未收录返回 None）。"""
    for _name, specs in _SECTIONS:
        for spec in specs:
            if spec.key == key:
                return spec
    return None


def parse_langs(text: str) -> list[str]:
    """逗号分隔语言列表文本 → 列表（剔空白项；纯函数，测试用）。"""
    return [item.strip() for item in text.split(",") if item.strip()]


def format_langs(langs) -> str:
    """语言列表 → 逗号分隔文本（纯函数，测试用）。"""
    return ", ".join(str(item) for item in langs)


def raw_value_for(spec: SettingSpec, raw: dict):
    """从 raw config 字典读取键值（缺键 / 类型不符回退默认；轻量校验）。

    启动时 ``config.load_config`` 才做严格校验，这里只为控件刷新给出
    可显示的值：类型匹配优先，不匹配回退默认。
    """
    value = raw.get(spec.key, spec.default)
    kind = spec.kind
    if kind in (KIND_BOOL,):
        return value if isinstance(value, bool) else spec.default
    if kind in (KIND_INT, KIND_FLOAT):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return spec.default
        number = float(value)
        if spec.minimum is not None and number < spec.minimum:
            return spec.default
        if spec.maximum is not None and number > spec.maximum:
            return spec.default
        return value
    if kind == KIND_ENUM:
        text = str(value)
        return text if text in spec.choices else str(spec.default)
    if kind == KIND_LANGS:
        return tuple(value) if isinstance(value, list) else tuple(spec.default)
    return value if isinstance(value, str) else str(spec.default)


def commit_value(spec: SettingSpec, widget_value):
    """控件提交值 → 写盘值（JSON 可序列化；纯函数）。

    - 数值按 spec 类型收敛（int 取整、float 保留）；布尔 / 字符串原样；
    - langs 文本经 :func:`parse_langs` 变列表。
    """
    if spec.kind == KIND_INT:
        return int(widget_value)
    if spec.kind == KIND_FLOAT:
        return float(widget_value)
    if spec.kind == KIND_LANGS:
        return parse_langs(str(widget_value))
    return widget_value


def restart_keys() -> set[str]:
    """标注为「需重启生效」的键集合（界面提示与测试用）。"""
    return {spec.key for _name, specs in _SECTIONS for spec in specs if spec.restart}


# ------------------------------------------------------------------ 窗口


class SettingsWindow(QWidget):
    """双栏设置窗口：左栏目列表 + 右设置页；改动即写盘，确定触发热重载。

    宿主交互：``on_reload`` 回调（「确定」按钮触发宿主
    ``reload_config()``，返回 None=成功 / 错误字符串=失败）；
    ``on_closed`` 回调（窗口关闭时宿主清引用）。
    """

    def __init__(self, on_reload=None, on_closed=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_reload = on_reload
        self._on_closed = on_closed

        self.setWindowTitle("设置")
        self.resize(720, 520)

        # ---- 左栏：功能栏目列表 -------------------------------------------
        self._section_list = QListWidget(self)
        for name in section_names():
            self._section_list.addItem(name)
        self._section_list.currentRowChanged.connect(self._on_section_changed)

        # ---- 右栏：各栏目设置页（每栏目一页，见 _SectionStack） -------------
        self._stack = _SectionStack(self._apply_spec_value)
        for name in section_names():
            self._stack.add_section(name, specs_for_section(name))

        splitter = QSplitter(self)
        splitter.addWidget(self._section_list)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # ---- 底部按钮行：确定（触发热重载） / 关闭 --------------------------
        self._hint_label = QLabel("", self)
        buttons = QDialogButtonBox(self)
        ok_button = buttons.addButton(
            "确定", QDialogButtonBox.ButtonRole.AcceptRole
        )
        ok_button.setObjectName("reload_button")
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._on_reload_clicked)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._hint_label)
        layout.addWidget(buttons)

        # 首屏选中第一个栏目（触发首栏刷新）
        self._section_list.setCurrentRow(0)

    # ---- 栏目切换 ------------------------------------------------------------

    def _on_section_changed(self, row: int) -> None:
        """切换栏目：重新读取 config.json 刷新右侧控件（同步他栏改动）。"""
        if row < 0:
            return
        self._stack.set_current_section(section_names()[row], self._read_raw())

    # ---- 读写 ------------------------------------------------------------------

    def _read_raw(self) -> dict:
        """读取 config.json 原始字典（缺失 / 损坏回退空 dict → 控件显示默认）。"""
        target = config.default_path()
        if not target.is_file():
            return {}
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("设置窗口读取配置失败（%s）：%s", target, exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _apply_spec_value(self, spec: SettingSpec, widget_value) -> None:
        """控件值变更：校验 → 合并写盘（只改对应键；栏目刷新不触发本回调）。"""
        ok = config.update_config_keys({spec.key: commit_value(spec, widget_value)})
        if not ok:
            self._hint_label.setText(f"「{spec.label}」写入失败（目录只读？），详见日志")

    def refresh_current_section(self) -> None:
        """重读 config.json 并刷新当前栏目控件（公开方法，测试驱动用）。"""
        name = self._stack.current_section_name()
        if name is not None:
            self._stack.set_current_section(name, self._read_raw())

    # ---- 确定（热重载） ----------------------------------------------------------

    def _on_reload_clicked(self) -> None:
        """「确定」：触发宿主 reload_config（运行时立即生效）；失败有明确提示。"""
        if not callable(self._on_reload):
            self._hint_label.setText("配置已保存（下次启动生效）。")
            return
        try:
            error = self._on_reload()
        except Exception as exc:  # 回调异常同样视为重载失败
            logger.exception("设置重载回调异常")
            error = str(exc)
        if error:
            self._hint_label.setText(f"重载失败：{error}（已保存的配置不受影响）")
        else:
            restart = _loaded_restart_labels()
            suffix = f"；需重启生效：{'、'.join(restart)}" if restart else ""
            self._hint_label.setText(f"已重载全部配置并生效{suffix}")

    def closeEvent(self, event) -> None:  # noqa: N802
        if callable(self._on_closed):
            try:
                self._on_closed()
            except Exception:  # pragma: no cover - 回调失败不影响关闭
                logger.exception("设置窗口关闭回调失败")
        super().closeEvent(event)


def _loaded_restart_labels() -> list[str]:
    """当前 config.json 中与默认不同的需重启键中文名（提示文案用）。"""
    labels: list[str] = []
    raw = _read_raw_for_labels()
    for _name, specs in all_sections():
        for spec in specs:
            if spec.restart and spec.key in raw and raw[spec.key] != spec.default:
                labels.append(spec.label)
    return sorted(set(labels))


def _read_raw_for_labels() -> dict:
    """提示文案专用的轻量 raw 读取（失败返回空 dict，不影响主流程）。"""
    target = config.default_path()
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


class _SectionStack(QWidget):
    """右侧设置页栈：每栏目一页（QFormLayout），支持按栏目刷新控件值。"""

    def __init__(self, on_change) -> None:
        super().__init__()
        self._on_change = on_change  # Callable[[SettingSpec, value], None]
        self._pages: dict[str, tuple[dict[str, QWidget], QWidget]] = {}
        self._current: str | None = None

        self._stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

    def add_section(self, name: str, specs: tuple[SettingSpec, ...]) -> None:
        """构建一个栏目的设置页（控件类型与配置项类型匹配）。"""
        widgets: dict[str, QWidget] = {}
        page = QWidget(self)
        form = QFormLayout(page)
        for spec in specs:
            widget = self._make_widget(spec)
            widgets[spec.key] = widget
            label = f"{spec.label}（需重启）" if spec.restart else spec.label
            form.addRow(label, widget)
        scroll_note = ""
        if specs and any(spec.note for spec in specs):
            scroll_note = "  ".join(
                f"{spec.label}：{spec.note}" for spec in specs if spec.note
            )
        note_label = QLabel(scroll_note, page)
        note_label.setWordWrap(True)
        note_label.setStyleSheet("color: gray;")
        form.addRow("", note_label)
        self._stack.addWidget(page)
        self._pages[name] = (widgets, page)

    def _make_widget(self, spec: SettingSpec) -> QWidget:
        """按键规格创建控件（类型匹配；变更信号接合并写盘回调）。"""
        if spec.kind == KIND_BOOL:
            widget = QCheckBox()
            widget.stateChanged.connect(
                lambda _state, s=spec, w=widget: self._on_change(s, w.isChecked())
            )
            return widget
        if spec.kind == KIND_INT:
            widget = QSpinBox()
            widget.setRange(int(spec.minimum or 0), int(spec.maximum or 999999))
            widget.valueChanged.connect(lambda value, s=spec: self._on_change(s, value))
            return widget
        if spec.kind == KIND_FLOAT:
            widget = QDoubleSpinBox()
            widget.setRange(float(spec.minimum or 0.0), float(spec.maximum or 999999.0))
            widget.setDecimals(2)
            widget.setSingleStep(0.5)
            widget.valueChanged.connect(lambda value, s=spec: self._on_change(s, value))
            return widget
        if spec.kind == KIND_TEXT:
            widget = QPlainTextEdit()
            widget.setFixedHeight(widget.fontMetrics().height() * 5)
            widget.textChanged.connect(
                lambda s=spec, w=widget: self._on_change(s, w.toPlainText())
            )
            return widget
        if spec.kind == KIND_ENUM:
            widget = QComboBox()
            widget.addItems(list(spec.choices))
            widget.currentTextChanged.connect(
                lambda text, s=spec: self._on_change(s, text)
            )
            return widget
        if spec.kind == KIND_LANGS:
            widget = QLineEdit()
            widget.editingFinished.connect(
                lambda s=spec, w=widget: self._on_change(s, w.text())
            )
            return widget
        # KIND_STR（含快捷键 / API 地址 / 模型 / Key）
        widget = QLineEdit()
        widget.editingFinished.connect(
            lambda s=spec, w=widget: self._on_change(s, w.text())
        )
        return widget

    def set_current_section(self, name: str, raw: dict) -> None:
        """切换到指定栏目并把 raw config 值刷入控件（重读同步他栏改动）。

        刷新经 ``blockSignals`` 抑制变更信号，不会触发合并写盘回环。
        """
        widgets, page = self._pages[name]
        self._current = name
        for spec in specs_for_section(name):
            widget = widgets.get(spec.key)
            if widget is None:
                continue
            self._set_widget_value(widget, spec, raw_value_for(spec, raw))
        self._stack.setCurrentWidget(page)

    def _set_widget_value(self, widget: QWidget, spec: SettingSpec, value) -> None:
        """按控件类型把值刷入（blockSignals 防变更信号回环）。"""
        if isinstance(widget, QCheckBox):
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)
        elif isinstance(widget, QSpinBox):
            widget.blockSignals(True)
            widget.setValue(int(value))
            widget.blockSignals(False)
        elif isinstance(widget, QDoubleSpinBox):
            widget.blockSignals(True)
            widget.setValue(float(value))
            widget.blockSignals(False)
        elif isinstance(widget, QPlainTextEdit):
            widget.blockSignals(True)
            widget.setPlainText(str(value))
            widget.blockSignals(False)
        elif isinstance(widget, QComboBox):
            widget.blockSignals(True)
            widget.setCurrentText(str(value))
            widget.blockSignals(False)
        elif isinstance(widget, QLineEdit):
            widget.blockSignals(True)
            if spec.kind == KIND_LANGS:
                widget.setText(format_langs(value))
            else:
                widget.setText(str(value))
            widget.blockSignals(False)

    def current_section_name(self) -> str | None:
        return self._current
