# renpy-overlay

向**正在运行的 Ren'Py 游戏进程**注入对话采集代理，并把游戏内对话实时显示在一个
**无边框、半透明、始终置顶、可鼠标拖动**的独立悬浮窗里；也可切换为**全透明艺术字
双窗口**（标题 + 正文），以打字机方式流式呈现 API 译文（默认启用）。

- 工具端（本程序）：负责进程发现、注入、接收数据、渲染悬浮窗、退出清理。
- 游戏端（注入的 agent）：跑在游戏进程内部，Hook Ren'Py 的对话回调，把文本经
  `127.0.0.1` 的 NDJSON/TCP 通道推送给工具端。

```
┌─────────────────────────┐         NDJSON over TCP          ┌──────────────────────────┐
│ Ren'Py 游戏进程          │   127.0.0.1:<随机端口>            │ renpy-overlay（本程序）    │
│  ┌───────────────────┐  │  ──────────────────────────►     │  ┌────────────────────┐  │
│  │ renpy_overlay_    │  │   hello / say / hb / log / bye   │  │ DialogueLink(服务端) │  │
│  │ agent（注入的 Hook）│  │  ◄──────────────────────────     │  └─────────┬──────────┘  │
│  └───────────────────┘  │      （重连、token 握手）           │            ▼             │
└──────────▲──────────────┘                                  │  ┌────────────────────┐  │
           │  CreateRemoteThread + pythonXX.dll 导出调用       │  │ 悬浮窗（置顶＋拖动）  │  │
           └─────────────────────────────────────────────────│  └────────────────────┘  │
                       注入由工具端发起                        └──────────────────────────┘
```

---

## 工作原理

### 1. 注入链路（纯 `ctypes`，无需 C 编译器）

Ren'Py 游戏进程内本来就加载了完整的 CPython 运行库（`python27.dll` / `python39.dll`
/ `python310.dll` / `python312.dll`，以及 Ren'Py 8.x 较新构建里改名的
`libpython3.9.dll`），因此不需要再带一个引导 DLL，直接远程调用它的解释器即可：

1. `OpenProcess`（`CREATE_THREAD | QUERY_INFORMATION | VM_OPERATION | VM_READ | VM_WRITE`）；
2. `EnumProcessModulesEx` 枚举模块，按规则挑出 Python 运行库（带版本号的实现优先；
   仅作稳定 ABI 转发的 `python3.dll` 与不含 Python C API 的 `librenpython.dll` 会被
   导出验证自然排除）；
3. 用 `ReadProcessMemory` **解析目标进程内的 PE 导出表**，定位
   `PyGILState_Ensure` / `PyRun_SimpleString` / `PyGILState_Release`
   —— 不依赖本机 Python 版本，同时从 PE `Machine` 字段确定目标架构（x86 / x64）；
4. `VirtualAllocEx` 分配两块内存：**桩区**（RWX，机器码 + 函数指针数据槽 + 源码）
   与**结果区**（RW，8KB）；
5. `CreateRemoteThread` 执行桩：`Ensure GIL → PyRun_SimpleString(源码) → Release GIL`，
   返回线程退出码；引导源码在 `start()` 返回后把 JSON 结果同步写进结果区；
6. 线程结束后读回结果区 → 释放两块远程内存。

因为整个过程仅操作**目标进程的内存**（工具端自身不加载任何 32 位代码），所以
64 位 Python 运行的工具端可以同时注入 **32 位（Ren'Py 7.x / Python 2.7）** 与
**64 位（Ren'Py 8.x / Python 3.9+）** 游戏。

### 2. 游戏内 Hook（多层捕获 + 去重）

注入的 `agent.py` 同时挂多个来源，任一层生效即可，全部包在 `try/except` 中、
绝不向游戏抛异常；按 `(who, what)` 去重：

| 层 | Hook 点 | 说明 |
| --- | --- | --- |
| 主 | `config.all_character_callbacks` | 官方角色回调，`event == "begin"` 时 `kwargs["what"]` 是完整对话文本 |
| 主 | `config.say_arguments_callback` | 链式包装（保留原有值）以取得当前说话角色名 |
| 备 | `config.periodic_callbacks` | 约 20Hz 轮询，读 `renpy.get_screen("say").scope` |
| 备 | 对话历史 | `renpy.store._history_list[-1].who / .what` |
| 备 | 当前语句 | `renpy.game.context().current` 上的 `who / what` |

注册优先走 `renpy.invoke_in_main_thread`（保证在主线程修改 config），否则直接注册
并有 2 秒看门狗兜底。上报由 `queue.Queue` + 守护发送线程完成，断线按
0.5s→5s 退避重连，每 2 秒心跳。

### 3. 悬浮窗

工具端自己的进程里用 Tkinter 创建窗口：

- `overrideredirect(True)` 无边框 + `-topmost` 置顶 + `-alpha` 半透明；
- 窗口不设任何 `WS_EX_TRANSPARENT` 穿透样式，鼠标事件由 Tk 正常接收：
  **可拖动、可滚动**；代价是落在悬浮窗上的点击会被它接住、不再透传给游戏；
- 每 200ms 读取游戏窗口的 `GetWindowRect`，按 `--dock` 贴靠跟随；游戏最小化时
  自动隐藏；
- **鼠标拖动**：按住窗口任意区域（对话文本 / 状态栏）用左键拖动即可移动，松开后
  位置以**相对游戏窗口的偏移**锁定，不再被自动停靠逻辑拉回（控制台输入 `d`
  可恢复默认停靠）；
- **双击锁定**：双击切换位置锁定 —— 锁定后不接受拖动、自动跟随也让位，位置固定；
  再双击解锁并恢复拖动/跟随；双击同时会强制中止在途翻译；
- **单击翻译（仅锁定态）**：锁定状态下单击左键，把**最近捕获的游戏原文**发给本地
  LM Studio（`http://127.0.0.1:1234`，OpenAI 兼容协议）翻译成中文并替换显示；
  请求在后台守护线程执行、同一时刻只允许一条在途，未锁定时单击不触发；
- **自动翻译（可选，config.json 开关）**：`auto_translate` 开启后，锁定状态下按
  `auto_translate_interval`（默认 3 秒）轮询最新游戏原文并自动翻译 —— 同一原文只翻
  一次、在途请求不重复发起；进入锁定需先经过一个完整轮询间隔才会首次触发；
  解锁即停用并中止在途（手动单击路径不受影响）；
- **单条显示**：窗口只展示当前最新一条对话，新对话直接替换上一条（不累积历史、
  不做行数裁剪）；长对话可用滚轮 / 右侧滚动条查看全文；
- 显隐与退出走控制台命令（`h` / `q`），不再有全局热键。

#### 流式悬浮窗（默认启用）

`config.json` 的 `use_stream_window: true`（默认）时改用**全透明艺术字双窗口**，
与传统 Tk 悬浮窗互斥显示（同一时刻只存在其一；PyQt6 缺失或初始化失败时自动回退
传统悬浮窗并记日志）：

- **正文窗**：完全透明（屏幕上只有文字），每个字符预烘焙为辉光 + 描边的精灵图
  （正文统一淡蓝色填充，标题保持霓虹渐变），打字机节拍逐字弹出（积压越多消化
  越快），带出现动画，静止后位置固定；平滑滚动自动跟随最新内容，滚轮上翻可
  回看当前对话全文、回到底部恢复跟随；显示策略与传统浮窗一致 —— 只保留最新
  一条对话，`show_original_text` 控制是否随对话刷新原文；
- **标题窗**：位于正文窗上方、水平左对齐的独立透明窗口，同样使用艺术字渲染，但
  不做打字机动画 —— 每次状态变化一次性整显完整标题；标题文案沿用传统浮窗的动态
  状态策略（新对话原文前 20 字、"翻译中... / 翻译完成 / 翻译失败"、锁定状态提示等）；
- **联动**：拖动任一窗口整体移动；移动 / 显隐 / 跟随 / 锁定态置顶均两窗成对处理；
- **流式翻译**：锁定态单击 / 自动翻译改走 SSE 流式接口（`stream: true`），译文
  增量逐块流入正文窗；两级缓存命中时整段喂入（观感一致）；缓存、去重、双击中止
  等逻辑与传统浮窗完全相同；
- **正文窗尺寸**：`stream_window_width`（默认 1760）/ `stream_window_height`（默认 200）
  控制正文窗大小（`--width/--height` 仅作用于传统 Tk 浮窗）；宽度超出游戏窗口时
  仍按既有规则被钳制；
- 字号 / 行距 / 标题字号 / 标题间距可经 `config.json` 调整（见下方配置示例）；
- **字体特效开关**：`disable_text_effects` 默认 `false`（保持辉光 / 描边 / 渐变
  艺术字）；设为 `true` 时正文与标题字符以基础纯色直接渲染，其余行为不变。

### 4. 安全与清理

- 通信只监听 `127.0.0.1`，首个报文必须携带工具端每次运行随机生成的 token；
- 工具退出（含异常路径、`atexit`）时：先远程卸载游戏内代理 → 停接收端 →
  销毁界面 → 释放全部远程分配与句柄；
- 游戏正常退出时，agent 通过 `config.python_exit_callbacks` 自动反注册全部 Hook；
- 唯一不清内存的场合：远程线程等待超时（说明游戏可能卡在执行中），此时
  **宁可少量泄漏也不去动可能仍在执行的代码页**，日志会明确提示。

---

## 环境要求

- Windows 10 / 11（仅桌面版 Ren'Py，不支持网页版与 Android 构建）；
- 本机安装 [uv](https://docs.astral.sh/uv/)；工具端 Python ≥ 3.10（`uv sync` 会自动准备）；
- 目标游戏为 Ren'Py 7.x（32 位）或 8.x（64 位），且已经启动到游戏主循环；
- 工具与游戏为**同一用户**运行。若游戏以管理员身份启动，本工具也需以管理员运行。

## 安装

```bash
uv sync            # 创建虚拟环境并安装依赖（psutil / pywin32 / PyQt6）
```

默认通过清华 TUNA 镜像下载（写在 `pyproject.toml` 的 `[[tool.uv.index]]` 里，
因为官方源在本机下载大体积 wheel 时不稳定）。如需换回官方源或其它镜像，
修改该配置，或设置环境变量 `UV_DEFAULT_INDEX`（如
`https://mirrors.aliyun.com/pypi/simple/`）。

## 打包为独立 exe（可选）

不想装 Python 环境时，可以把工具端打成单文件 exe：PyInstaller 打包运行必需的
模块（psutil / pywin32 / tkinter / PyQt6；包含 Qt DLL 后体积明显增大），排除
numpy 等无关库：

```bash
uv run pyinstaller renpy_overlay.spec --noconfirm
```

产物为 `dist/renpy-overlay.exe`，可复制到任意 Windows 机器直接运行（无需 Python）。

- 首次运行会在 **exe 同目录**自动生成 `config.json`（打包后配置固定跟随 exe，
  不受启动目录 / 快捷方式影响）；日志默认写入当前工作目录的 `logs/`
  （双击运行时即 exe 同目录）；
- 注入、悬浮窗、翻译等行为与 `uv run renpy-overlay` 完全一致，命令行参数相同；
- 打包配置见 `renpy_overlay.spec`：`payload/agent.py`（注入到游戏进程内执行的
  源码）作为数据文件随包分发，运行时经 `importlib.resources` 读取；未启用 UPX
  （避免放大杀软对远程注入工具的误报）；想要启动更快的目录版（onedir），按
  spec 顶部注释切换即可。

## 快速开始

```bash
# 1. 列出候选进程（按“像 Ren'Py 的程度”打分排序）
uv run renpy-overlay --list

# 2. 注入并显示悬浮窗（弹出选择窗，选好 PID 双击或点“注入”）
uv run renpy-overlay

# 或直接指定 PID
uv run renpy-overlay --pid 12345
```

注入成功后：

- 默认启用流式悬浮窗：全透明艺术字双窗口（标题在上、正文在下，左对齐），对话以
  打字机动画逐字弹出，随游戏移动、最小化而隐藏；设 `use_stream_window: false`
  可回到传统半透明浮窗；
- 游戏内每推进一句对话，正文窗实时刷新为最新一条（不累积历史）；
- 按住任一窗口拖动即可整体调整位置；滚轮可回看当前对话全文；
- 双击锁定位置后，单击会把对话原文流式翻译成中文（未锁定不触发）；
- 在 `config.json` 中开启 `auto_translate` 后，锁定状态下会自动翻译最新对话。

### 鼠标操作

| 操作 | 作用 |
| --- | --- |
| 按住窗口 + 左键拖动 | 移动悬浮窗；松开后位置锁定（相对游戏窗口保持） |
| 左键双击 | 锁定 / 解锁窗口位置（锁定后不响应拖动；双击同时中止在途翻译） |
| 锁定状态下单击 | 用本地 LM Studio 翻译当前对话原文并替换显示（未锁定不触发） |
| 鼠标滚轮（悬浮窗内任意位置） | 查看长对话的其余部分（窗口只显示最新一条） |
| 拖动右侧滚动条 | 同上，细粒度查看全文 |
| 按住标题/正文任一窗拖动（流式窗） | 两窗整体联动移动；其余操作同上（流式窗无滚动条，滚轮回看全文） |

启动工具的终端里另有控制台命令：`h`（显隐）、`d`（恢复自动停靠）、`u`（卸载代理）、`s`（状态）、`q`（退出）。

### 翻译（可选，OpenAI 兼容服务）

- 默认面向本地 [LM Studio](https://lmstudio.ai/)（启动并加载一个模型，开启本地服务
  即可）；也可通过 `config.json` 切换到 OpenAI / DeepSeek 等任意 OpenAI 兼容平台；
- 锁定悬浮窗（双击）后单击，会把**注入代理捕获的游戏原文**（而非窗口当前显示的文本）
  发给 `api_base_url`（默认 `http://127.0.0.1:1234`），使用 OpenAI 兼容协议（`model`
  留空时自动读取 `/v1/models` 的第一个已加载模型），译文返回后按现有规则替换显示；
- **翻译 API 可外置配置**（`config.json`）：

  | 字段 | 默认值 | 说明 |
  | --- | --- | --- |
  | `api_base_url` | `http://127.0.0.1:1234` | API 地址（OpenAI / LM Studio / DeepSeek 等兼容平台） |
  | `api_timeout` | `20.0` | 请求超时（秒，非正数回退默认） |
  | `model` | `""` | 模型代号；留空自动读取 `/v1/models` 的第一个已加载模型 |
  | `system_prompt` | 内置游戏翻译提示词 | 系统提示词，可整个覆盖 |
  | `api_key` | `""` | 配置后请求附带 `Authorization: Bearer <key>`；留空不带鉴权头 |
  | `enable_thinking` | `false` | 模型思考（reasoning）模式开关，**默认关闭**。关闭时一并声明 `enable_thinking: false` + `chat_template_kwargs` + `reasoning_effort`：LM Studio 会忽略前两者、需 `reasoning_effort` 才能真正关闭（实测：关闭后单条翻译约 0.5s，开启思考则需 30s+）；vLLM / SGLang 识别 `chat_template_kwargs`、Qwen Cloud 识别顶层 `enable_thinking` |
  | `reasoning_effort` | `"none"` | 关闭思考时使用的 `reasoning_effort` 取值（LM Studio 等本地服务靠它真正关闭思考）；置空则不发送该字段 |

- 请求在后台线程执行，不阻塞窗口；同一时刻只允许一条在途请求；双击可随时中止；
- 流式悬浮窗启用时翻译走 SSE 流式接口，译文增量实时逐块流入正文窗（打字机呈现）；
  传统浮窗仍为一次性返回后替换显示；
- **自动翻译**：项目根目录的 `config.json`（首次运行自动创建、已被 `.gitignore`
  排除）控制自动翻译 ——

  ```json
  {
    "auto_translate": false,          // 改为 true 开启自动翻译
    "auto_translate_interval": 3.0,   // 轮询间隔（秒）
    "show_original_text": true,       // 正文区是否随对话显示游戏原文（默认开启）
    "translation_cache_size_kb": 256, // 内存翻译缓存上限（KB，默认 256）
    "use_stream_window": true,        // 流式悬浮窗开关（默认开启；false 用传统 Tk 浮窗）
    "stream_window_width": 1760,      // 流式正文窗宽度（像素，最小 240；会被游戏窗口宽度钳制）
    "stream_window_height": 200,      // 流式正文窗高度（像素，最小 80）
    "stream_window_font_size": 14,    // 流式正文窗字号（像素，最小 6）
    "stream_window_line_spacing": 1.45, // 流式正文行距倍数（最小 1.0）
    "stream_window_title_font_size": 8, // 流式标题窗字号（像素，最小 6）
    "stream_window_title_gap": 4,     // 标题窗与正文窗间距（像素，非负）
    "disable_text_effects": false,    // 关闭字体特效：true 时正文/标题以基础纯色渲染（无辉光/描边/渐变）
    "api_base_url": "http://127.0.0.1:1234",  // OpenAI 兼容 API 地址
    "api_timeout": 20.0,              // 请求超时（秒）
    "model": "",                      // 模型代号；空则自动取 /v1/models 的第一个
    "api_key": "",                     // API Key；空则不携带鉴权头（本地服务通常不需要）
    "enable_thinking": false,         // 模型思考模式开关，默认关闭
    "reasoning_effort": "none",       // 关闭思考时的 reasoning_effort 取值
    // system_prompt 默认使用内置的游戏对话翻译提示词，可按需覆盖（见下表）
  }
  ```

  开启后：悬浮窗**锁定**状态下按间隔轮询最新捕获的对话原文并自动翻译（同一原文
  只自动翻一次；刚进入锁定需等一个完整间隔；解锁即停用并中止在途；手动单击不受影响）。
  配置读取失败或内容非法时回退默认值并记日志，不会导致程序崩溃；
- **原文显示开关**：`show_original_text` 为 `false` 时，正文区不再随对话刷新原文
  （上一条内容保持不动）；对话仍照常捕获、写入日志，并作为翻译请求的唯一输入，
  标题栏的原文前 20 字提示与翻译替换显示均不受影响。
- **翻译缓存（两级，自动翻译专用）**：内存缓存以**原文哈希**（sha256 前 128 位，
  跨重启稳定）为键分桶存储 —— 每个哈希对应一个「(原文, 译文) 记录列表」，哈希
  相同但原文不同的记录可同桶共存，查询时按原文逐一精确匹配；容量由
  `translation_cache_size_kb` 控制（默认 256KB，哈希+原文+译文的 UTF-8 字节合计，
  超限按记录粒度的最旧优先 FIFO 淘汰）。SQLite 持久化在**游戏目录**下的
  `renpy_overlay_cache/translations.db`，以 (哈希, 原文) 为复合主键增量保存记录
  （与内存分桶语义一致；旧版单主键数据库打开时自动迁移并保留数据；重启后仍可查询；
  目录/库不可写时自动降级为仅内存缓存）。自动翻译依次查内存 → 数据库，任一级命中
  直接上屏、不发起请求（数据库命中会回填内存缓存）；两级均未命中才调 API，成功后
  两级同时写入；单击翻译固定调用 API（不读任何缓存），成功后覆盖写入两级。
- 未启动 LM Studio 时请求会失败并记录日志，界面保持原样，不影响其它功能。

### 卸载与查询

```bash
uv run renpy-overlay --pid 12345 --unload   # 卸载游戏内代理（恢复全部 Hook）
uv run renpy-overlay --pid 12345 --status   # 查询代理运行状态
```

游戏先退出也没有关系：连接断开后工具端会提示，并在 `--idle-exit`（默认 20 秒）
后自动清理退出。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--pid PID` | — | 直接指定目标进程，跳过选择界面 |
| `--list` | — | 只列出候选进程后退出 |
| `--all` | — | 连低分候选一起列出 |
| `--no-gui` | — | 不用图形选择窗，走命令行菜单 |
| `--unload` | — | 卸载目标进程内已注入的代理后退出 |
| `--status` | — | 查询目标进程内代理状态后退出 |
| `--no-overlay` | — | 不显示悬浮窗，对话打印到控制台 |
| `--dock POS` | `top-center` | 停靠位置：`top-center/top-left/top-right/bottom-center/bottom-left/bottom-right` |
| `--width / --height` | `880 / 200` | 传统 Tk 悬浮窗尺寸（像素）；流式窗尺寸由 `config.json` 的 `stream_window_width/height`（默认 1760/200）控制 |
| `--alpha` | `0.85` | 不透明度 `0.1~1.0` |
| `--font-size` | `11` | 对话字号 |
| `--port` | `0` | IPC 监听端口（默认由系统分配空闲端口） |
| `--timeout` | `15.0` | 等待远程线程结束的秒数 |
| `--idle-exit` | `20.0` | 游戏进程消失后自动退出的秒数 |
| `--poll-hz` | `7.0` | 游戏内兜底轮询频率 |
| `--log-level / --log-dir / --log-file` | `INFO / logs` | 日志配置 |

## 日志

- 控制台输出 INFO 及以上；`logs/renpy_overlay_<时间戳>.log` 记录 DEBUG 全量细节
  （含每次注入的导出地址、线程退出码、Hook 层注册结果）；
- 游戏进程内 agent 的日志经 IPC 回灌到同一份日志（logger 前缀 `renpy_overlay.game`），
  注入端与游戏端的时序在一条时间线上。

## 测试

```bash
uv run pytest          # 单元测试
uv run ruff check .    # 静态检查
```

测试不依赖真实的 Ren'Py 游戏：

- `tests/test_shellcode.py`：x86 / x64 引导桩的字节级布局与 RIP 相对位移断言；
- `tests/test_pe.py`：拿本进程里的 `kernel32.dll` 内存映射当样本，用 `GetProcAddress`
  交叉验证 PE 导出解析器，并覆盖转发导出、`python3.dll` 转发层排除等分支；
- `tests/test_ipc.py`：NDJSON 编解码、token 握手、非法报文容错、心跳、断开回调；
- `tests/test_payload.py`：`agent.py` 的 Python 2.7 语法兼容性静态断言（f-string /
  注解 / nonlocal 等一律禁止）、引导模板占位符与 base64 嵌入内容往返校验；
- `tests/test_translator.py`：用本地 HTTP 桩验证翻译客户端（OpenAI 兼容协议往返、
  空文本拒绝、协议缺字段报错、服务不可达收敛为 TranslationError）；
- `tests/test_translator_stream.py`：流式翻译客户端（SSE 桩：逐块产出与顺序、
  `stream: true` 请求体、生成器惰性、错误路径收敛为 TranslationError）；
- `tests/test_stream_window.py`：流式双窗口的 `pair_layout` 几何（左对齐、上下
  布局、间距、与停靠 / 拖动偏移几何的组合）；
- `tests/test_config.py`：本地配置首建默认值、非法 JSON / 类型错误逐项回退、坏文件不覆盖、
  `show_original_text` 与流式窗各配置项的默认 / 合法读取 / 缺失与越界回退；
- `tests/test_translation_cache.py`：分桶共存（同哈希多原文）、桶内覆盖与顺序刷新、
  记录级 FIFO 淘汰（不整桶淘汰）、哈希键稳定性、UTF-8 字节计量、单条超限拒绝；
- `tests/test_translation_store.py`：数据库创建、增量写入、同原文覆盖、同哈希多原文共存、
  旧版单主键数据库自动迁移、重启持久化、目录不可建时的降级。

端到端自测（需要真实游戏）：用 Ren'Py SDK 的 *The Question* 或任一发行版游戏，
按"快速开始"注入后确认：对话实时上屏且只保留最新一条（旧对话不累积）、拖动悬浮窗
后位置保持不被跟随拉回、长对话可用滚轮/滚动条查看全文、`--unload` 后不再有新对话、
关闭游戏后工具自动退出、日志无残留线程报错。

## 目录结构

```
renpygameread/
├─ pyproject.toml              # uv 项目配置 / 依赖 / 入口脚本
├─ renpy_overlay.spec          # PyInstaller 打包配置（生成 dist/renpy-overlay.exe）
├─ pyinstaller_entry.py        # PyInstaller 专用入口（绝对导入）
├─ .python-version             # 工具端 Python 版本
├─ README.md
├─ src/renpy_overlay/
│  ├─ __init__.py
│  ├─ __main__.py              # python -m renpy_overlay
│  ├─ cli.py                   # 参数解析 + 主流程编排 + 退出清理
│  ├─ logs.py                  # logging（控制台 INFO / 文件 DEBUG / 游戏端回灌）
│  ├─ win32api.py              # ctypes：进程/内存/线程/模块/窗口 封装与常量
│  ├─ pe.py                    # 远程 PE 导出表解析（RVA 语义 reader）
│  ├─ shellcode.py             # x86 / x64 引导桩机器码生成
│  ├─ discovery.py             # 进程与窗口枚举、Ren'Py 打分识别
│  ├─ picker.py                # Tkinter 选择窗（失败回退命令行菜单）
│  ├─ injector.py              # 注入 / 卸载 / 状态查询编排
│  ├─ ipc.py                   # NDJSON over TCP 服务端 + 协议编解码
│  ├─ overlay.py               # 悬浮窗（Tk：单条显示、可拖动、双击锁定、单击翻译）
│  ├─ stream_window.py         # 流式悬浮窗（PyQt6 全透明艺术字双窗口，与 overlay 互斥）
│  ├─ translator.py            # LM Studio 翻译客户端（OpenAI 兼容，标准库 urllib，支持 SSE 流式）
│  ├─ translation_cache.py     # 翻译内存缓存（哈希键→(原文,译文)，FIFO，仅主线程读写）
│  ├─ translation_store.py     # 翻译 SQLite 持久化（游戏目录 renpy_overlay_cache/，可降级）
│  ├─ config.py                # 本地配置加载（config.json：窗口开关 / 自动翻译 / API）
│  └─ payload/                 # 注入到游戏进程内执行的源码（py2/py3 兼容）
│     └─ agent.py              # Hook 安装、上报线程、心跳、shutdown
└─ tests/
   ├─ test_shellcode.py
   ├─ test_pe.py
   ├─ test_ipc.py
   ├─ test_payload.py
   ├─ test_translator.py
   ├─ test_translator_stream.py
   ├─ test_overlay.py
   ├─ test_stream_window.py
   ├─ test_translation_cache.py
   ├─ test_translation_store.py
   └─ test_config.py
```

`payload/` 下的模块**不会**被工具端 import —— 它们经 `importlib.resources` 读成
文本，随引导代码一起送进目标进程执行，因此禁止使用 Python 2.7 不支持的语法。

## 排障

| 现象 | 处理 |
| --- | --- |
| `--list` 里看不到游戏 | 用 `--all` 看全部进程；确认游戏已进入主循环；游戏是管理员启动时本工具也要管理员 |
| 注入报 `OpenProcess` 失败（error=5） | 权限不足：以管理员身份运行；并确认工具与游戏是同一用户 |
| 注入报"未找到 Python 运行库" | 先看日志 DEBUG 里的完整模块清单：新版 Ren'Py 的 `libpython3.9.dll` 命名已支持；若清单中确实没有任何 python 相关 dll，说明是静态链接 Python 的自定义构建（暂不支持）；否则核对选中的 PID 是否为游戏本体 |
| `CreateRemoteThread` 失败或被拦 | 杀软 / EDR 拦截了远程线程注入，见下方"合规与安全"；可将项目目录加入白名单（仅限自有环境） |
| 注入成功但收不到对话 | 看日志里的"游戏端确认 Hook 层"；游戏若停在主菜单尚未有对话属正常；可调 `--poll-hz` |
| 悬浮窗被游戏画面盖住 | 游戏处于全屏独占模式时其它窗口无法置顶，请改用窗口化 / 无边框窗口化 |
| 悬浮窗会接收鼠标点击（不再穿透） | 拖到不遮挡操作的位置即可（位置会锁定）；控制台输入 `d` 恢复默认停靠 |
| 退出后游戏里有残留 | 运行 `--unload`；查看日志中 `shutdown` 结果与 `python_exit_callbacks` 注册情况 |
| 注入超时（>15s） | 日志会列出未释放的两块内存地址；游戏若已卡死请直接结束游戏进程，工具不会去动可能仍在执行的代码页 |

## 合规与安全

本项目使用 `OpenProcess` + `WriteProcessMemory` + `CreateRemoteThread` 这一典型的
**远程线程注入**技术特征，可能被杀毒软件 / EDR 标记为可疑行为。

- 请仅用于**你自己的游戏、自研项目或已获得授权**的本机调试与学习；
- 请勿用于绕过游戏保护、破解、作弊或任何违反游戏服务条款与当地法律的用途；
- 通信全程只绑定 `127.0.0.1` 并带随机 token 握手，不对局域网/公网暴露任何端口。

## 已知限制

- 仅支持 Windows 桌面版 Ren'Py（7.x / 8.x）；不支持网页版、Android。
- 目标进程必须已经初始化 Python 运行时（游戏刚启动、Ren'Py 尚未初始化时注入会失败，
  稍等片刻重试即可）。
- 不引入 C 编译器后端（如后续需要可另加引导 DLL 模式）。
- 对把 CPython 静态链接进主程序（进程模块里没有 `python*.dll` / `libpython*.dll`）
  的自定义构建无法注入；工具会把完整模块清单写入日志便于确认。
