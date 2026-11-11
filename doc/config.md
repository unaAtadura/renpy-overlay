# 配置系统与设置窗口

对应源码：`src/renpy_overlay/config.py`（加载 / 校验 / 合并写盘）、`settings_window.py`（双栏设置窗口）、宿主热重载 `stream/host_core.py`（`reload_config` / `_apply_runtime_config`）。测试：`test_config.py`（959 行：首建默认值、非法 JSON / 类型错误逐项回退、坏文件不覆盖、各键默认/合法/缺失/越界）、`test_settings_window.py`。

## 1. config.json 的位置与生命周期

`default_path()` 决定配置文件位置：

- **PyInstaller 打包后（frozen）**：固定 exe 所在目录（打包后配置跟随 exe，不受启动目录/快捷方式影响）；
- **源码运行**：以包文件位置反推项目根（`src/renpy_overlay/config.py` → 上两级，即 `pyproject.toml` 同级）——从任何工作目录启动都命中同一份配置；反推失败（如装进 site-packages）回退当前工作目录。

生命周期：文件缺失时 `_write_defaults` 写出全部默认键；读取失败 / JSON 非法 / 顶层非对象 → 回退 `AppConfig()` 默认值（**不回写用户的坏文件**，避免覆盖手工编辑）；字段级问题逐项回退默认值并记 WARNING。配置问题绝不导致程序崩溃。

## 2. 全部配置键（AppConfig 字段与校验规则）

校验辅助函数语义：`_read_bool`（非布尔回退）、`_read_str`（非字符串回退；`allow_empty=False` 时空串回退）、`_read_positive_int`（非整数或低于下限回退；**bool 是 int 子类不算合法数字**）、`_read_non_negative_int`、`_read_positive_float`、`_read_int_in_range`（越界回退）、`_read_percent`（1..100）、`_read_hotkey`（`parse_hotkey` 解析失败回退）。

### 翻译 / API

| 键 | 默认 | 下限/范围 | 说明 |
| --- | --- | --- | --- |
| `api_base_url` | `http://127.0.0.1:1234` | 非空 | OpenAI 兼容 API 地址（LM Studio / OpenAI / DeepSeek 等） |
| `api_timeout` | `30.0` | >0 | 请求超时秒数 |
| `model` | `""` | — | 空 = 自动 `GET /v1/models` 取第一个已加载模型 |
| `system_prompt` | 内置翻译提示词 | — | 可整个覆盖 |
| `api_key` | `""` | — | 非空时附 `Authorization: Bearer` |
| `enable_thinking` | `false` | — | 思考模式开关；关闭时三方言同发（见 [translation.md](translation.md) §1） |
| `reasoning_effort` | `"none"` | — | 关闭思考时的取值；置空不发送 |
| `api_base_url_vision` / `model_vision` / `api_key_vision` | `""` | — | 视觉三键；基址空跟随主链路、模型空自动发现（需 vision 能力）、密钥空不带鉴权头 |
| `api_base_url_asr` / `model_asr` / `api_key_asr` | `""` | — | 非实时 ASR 三键；基址空跟随主链路、模型空跟随主链路 model、密钥空跟随主链路 api_key；无备选模型 |
| `api_base_url_asr_rt` / `model_asr_rt` / `api_key_asr_rt` | `""` | — | 实时 ASR 三键；**不回退其它链路，任一为空即不可用** |

### 自动翻译与缓存

| 键 | 默认 | 下限 | 说明 |
| --- | --- | --- | --- |
| `auto_translate` | `true` | — | 自动翻译开关（仅锁定状态生效） |
| `auto_translate_interval` | `1.0` | 0.1 | 轮询间隔秒 |
| `show_original_text` | `false` | — | 正文区是否随对话显示游戏原文（分支选项列表同样受其约束）。分支选项的译文在两种模式下均为「提问译文 + 编号选项译文」整段一次性呈现（提问与选项同范围，来自两级缓存/独立翻译，选项翻译目标与缓存键不变）；true 时额外先显示原文合并段，译文就绪后被整段替换 |
| `translation_cache_size_kb` | `2048` | >0 | 内存缓存上限（KB） |

### 悬浮窗

| 键 | 默认 | 下限 | 说明 |
| --- | --- | --- | --- |
| `stream_window_width` / `stream_window_height` | `1760` / `200` | 240 / 80 | 正文窗尺寸（会被游戏窗口宽度钳制） |
| `stream_window_font_size` | `16` | 6 | 正文字号（像素） |
| `stream_window_line_spacing` | `1.15` | 1.0 | 行距倍数 |
| `stream_window_title_font_size` | `12` | 6 | 标题字号 |
| `stream_window_title_gap` | `2` | ≥0 | 标题窗与正文窗间距 |
| `translate_window_style` | `1` | 仅 1/2 | 悬浮窗样式（非法值回退 1） |
| `body_fill_color` | `"#ADD8E6"` | #RGB/#RRGGBB | 流式文字填充色（样式 1 的标题与正文；非法值回退默认；点设置窗口「确定」后即时生效） |
| `chat_window_width` / `chat_window_height` | 同流式正文窗配置值 | 240 / 80 | AI 对话窗尺寸（键不存在或非法时**回退流式正文窗的配置值**） |
| `fullscreen_mode` | `"sdl"` | 仅 sdl/qt | 视频剪辑全屏渲染路径；非法值回退 **qt**（attach 保底） |

### 截图 / 快捷键 / 遥控

| 键 | 默认 | 下限 | 说明 |
| --- | --- | --- | --- |
| `screenshot_compress_percent` | `10` | 1..100 | 截图发送 API 前等比压缩百分比 |
| `hotkey_main` | `"alt+t"` | 合法键位 | 快捷键模式主开关（不含 Ctrl 以规避 Ren'Py 持续快进） |
| `hotkey_window_1..8` | `"1".."8"` | 合法键位 | 八色截图窗口热键（红橙黄绿青蓝紫黑） |
| `hotkey_mouse_escape` | `"alt+o"` | 合法键位 | 锁鼠标逃脱键 |
| `remote_screenshot_enable` | `true` | — | 遥控截图 A/B 小圆窗开关 |
| `remote_screenshot_diameterA` / `diameterB` | `100` | 20 | 触发器 / 瞄准器直径（像素） |
| `remote_screenshot_interval` | `0.5` | 0.1 | 遥控器自动化周期截图间隔秒 |
| `remote_cache_size_count` | `50` | 1 | 自动化内存循环队列缓存条数 |

配置文件中的键名为 `remote_screenshot_diameterA/B`（大写结尾），对应 `AppConfig.remote_screenshot_diameter_a/b` 字段。

### 听歌识曲 / 预构建

| 键 | 默认 | 下限/范围 | 说明 |
| --- | --- | --- | --- |
| `recording_duration` | `8` | ≥3 | 录制系统音频时长秒 |
| `max_concurrency` | `8` | 1..64 | 预构建并发上限 |
| `initial_concurrency` | `1` | 1..64 | 起步并发 |
| `concurrency_increase_after` | `6` | 1..1000 | 连成功多少条并发 +1 |
| `latency_guard_seconds` | `15.0` | ≥1.0 | 时延闸门（中位时延超过则不升档） |
| `circuit_break_threshold` | `5` | 1..100 | 连续不可达熔断阈值 |
| `retries` | `2` | 0..10 | 单条普通失败重试次数 |
| `include_tl_strings` | `true` | — | 预构建是否扫描 game/tl/ |
| `tl_langs` | `[]` | 字符串列表 | tl 语言过滤（空 = 全部语言；非列表回退空） |
| `last_game_dir` | `""` | — | 预构建上次选择的游戏目录（自动记忆，无需手改） |

## 3. 合并写盘（update_config_keys）

供设置窗口 / 预构建窗口的单键变更落盘：读 raw → 只改 `updates` 中的键 → 整文件写回（`indent=2`，保留未展示键与手工编辑内容）。文件不存在或读取失败时以空骨架重建（仅写入 updates）；写盘失败记日志返回 False，不抛异常。

## 4. 双栏设置窗口（settings_window.py）

- 左侧功能栏目：翻译 / 预构建翻译缓存 / 视觉识图 / 语音识别 / 实时语音识别 / 截图翻译与快捷键 / 遥控截图 / 听歌识曲 / 视频录制与剪辑 / 流式窗口样式 / AI 对话窗；右侧为该栏目设置项；
- 项规格由 `SettingSpec` 描述，控件与类型匹配：布尔用开关、数值用范围步进框（范围与配置校验一致）、文本用输入框、系统提示词用多行文本、枚举（全屏渲染路径 / 悬浮窗样式）用下拉；
- **同一配置键可出现在多个栏目**（如主链路三键同属「翻译」与「预构建」），共用同一份 config.json；**切换栏目时重新读取配置文件**，同步在其他栏目所做的改动，避免互相覆盖；
- **改动即合并写盘**（`update_config_keys`），下次启动载入；
- 「确定」按钮触发**运行时热重载**（`host_core.reload_config` → `_apply_runtime_config`）：API 参数、自动翻译间隔、全局热键组、锁鼠标逃脱键等立即生效；无法安全热应用的键（悬浮窗样式与尺寸、对话窗尺寸、全屏渲染路径、内存缓存容量、遥控截图参数）在设置名中标注「（需重启）」，确定后也会提示；
- 重载失败（如热键被占用）时明确提示且运行时整体回退原配置，已写盘的新配置不受影响。

## 交叉链接

- 各键的消费方：翻译三链路 [translation.md](translation.md) / [screenshot.md](screenshot.md) / [asr.md](asr.md)；预构建并发 [pretranslate.md](pretranslate.md)；悬浮窗 [overlay-window.md](overlay-window.md)
- 请求体方言（enable_thinking / reasoning_effort）：[translation.md](translation.md) §1
