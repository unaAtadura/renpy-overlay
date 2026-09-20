# renpy-overlay

向**正在运行的 Ren'Py 游戏进程**注入对话采集代理，并把游戏内对话实时显示在一个
**全透明、始终置顶、可鼠标拖动**的悬浮窗里：标题 + 正文两个独立透明窗口（PyQt6），
常规字体逐字渲染、逐行半透明蒙版提升对比度，以打字机方式流式呈现 API 译文。

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
| 主 | `renpy.display_menu` | 链式包装，所有 menu 语句（分支选项）的唯一入口：进入时捕获选项原文与触发对话上下文，返回时捕获玩家所选；游戏自定义 choice screen 也经过它 |
| 备 | `config.periodic_callbacks` | 约 20Hz 轮询，读 `renpy.get_screen("say").scope` |
| 备 | 对话历史 | `renpy.store._history_list[-1].who / .what` |
| 备 | 当前语句 | `renpy.game.context().current` 上的 `who / what` |

注册优先走 `renpy.invoke_in_main_thread`（保证在主线程修改 config），否则直接注册
并有 2 秒看门狗兜底。上报由 `queue.Queue` + 守护发送线程完成，断线按
0.5s→5s 退避重连，每 2 秒心跳。

分支选项捕获（`exports_menu` 层）的实测验证环境：**Ren'Py 8.2.1.24030407**
（脚本版本 `(8, 2, 1)`，构建于 `2025-07-06`，`Python 3.9.10 / x64`），测试游戏
**Sicae-Ep.7**；更早或更晚的引擎版本未实测（若该 Hook 未生效，仍有 choice screen
轮询兜底可捕获选项文本，但拿不到玩家所选）。

### 3. 悬浮窗

工具端自己的进程里用 PyQt6 创建**全透明双窗口**（标题 + 正文），两窗均带
`WA_TranslucentBackground + FramelessWindowHint + WindowStaysOnTopHint + Tool`，
屏幕上只可见文字本身：

- **正文窗**：文字以常规字体（Black 字重、统一淡蓝色）直接绘制，背后逐行绘制
  半透明黑色蒙版条带（行距区域留空隙）提升对比度；打字机节拍逐字弹出（积压越多
  消化越快），带出现动画，静止后位置固定；输出过程始终固定显示第一行（大段
  文字不因自动滚屏滚出可视区，1-2 行短文本观感不变），滚轮可回看当前对话
  全文、滚回顶部恢复固定显示；锁定状态下还可按住文字上下拖拽滚动（防抖 +
  两档速度）；显示策略为单条显示 —— 只保留最新一条对话，
  `show_original_text` 控制是否随对话刷新原文；
- **标题窗**：位于正文窗上方、水平左对齐的独立透明窗口，不做打字机动画 —— 每次
  状态变化一次性整显完整标题；文案随交互状态动态更新（新对话原文前 20 字提醒、
  "翻译中... / 翻译完成 / 翻译失败"、锁定状态提示等），控制台的连接状态也写入
  同一位置；
- **跟随游戏窗口**：每 200ms 读取游戏窗口的 `GetWindowRect`，按 `--dock` 贴靠
  跟随（定位全部走 win32 物理像素）；游戏最小化时自动隐藏；移动 / 显隐 / 跟随 /
  锁定态置顶均两窗成对处理，锁定态还会周期性重申 TOPMOST（应对独占全屏被激活时
  的覆盖）；
- **鼠标拖动**：按住标题或正文任一窗口拖动，两窗整体联动移动，松开后位置以
  **相对游戏窗口的偏移**锁定，不再被自动停靠逻辑拉回（控制台输入 `d` 可恢复
  默认停靠）；
- **双击锁定**：双击切换位置锁定 —— 锁定后不接受拖动、自动跟随也让位，位置固定；
  再双击解锁并恢复拖动/跟随；双击同时会强制中止在途翻译；
- **单击翻译（仅锁定态）**：锁定状态下单击左键，把**最近捕获的游戏原文**发给
  OpenAI 兼容服务（`http://127.0.0.1:1234`），翻译走 SSE 流式接口（`stream: true`），
  译文增量逐块流入正文窗；请求在后台守护线程执行、同一时刻只允许一条在途，
  未锁定时单击不触发；
- **自动翻译（可选，config.json 开关）**：`auto_translate` 开启后，锁定状态下按
  `auto_translate_interval`（默认 3 秒）轮询最新游戏原文并自动翻译 —— 同一原文只翻
  一次、在途请求不重复发起；进入锁定需先经过一个完整轮询间隔才会首次触发；
  两级缓存命中时直接上屏；解锁即停用并中止在途（手动单击路径不受影响）；
- **分支选项捕获**：注入代理同时捕获剧情分支选项（menu choice）—— 选项出现时
  正文整段替换为编号选项列表（打字机呈现，含触发对话的上文），标题显示选项预览；
  玩家做出选择后正文追加「→ 已选择：×××」提示；选项事件不影响翻译链路
  （自动翻译仍只翻对话原文）；控制台模式下同步打印；
- **截图翻译**：标题/正文窗文字处**右键**弹快捷菜单 —— 创建截图窗口（最多 8 个，
  边框按红橙黄绿青蓝紫黑依次分配；可拖动、八向拉伸、双击锁定）、销毁截图窗口
  （堆栈式，优先销毁最新）、查看历史、对话；双击锁定截图窗后单击即截取该区域，
  短暂隐藏全部悬浮窗后用 Pillow 截屏，发送 vision 模型识别翻译 —— 始终走 API
  不检索缓存、同一时刻与单击/自动翻译互斥（存在任意在途翻译则不截图不发请求）；
  双击解锁截图窗不打断截图翻译，双击解锁流式窗则会中止它；译文显示在正文窗，
  成功后原图（JPEG Q90）与 ≤10w 像素缩略图存入游戏目录
  `renpy_overlay_cache/screenshot.db`（不写翻译两级缓存）；失败则清除内存图像
  并提示「翻译失败」；
- **截图历史**：常规窗口浏览 `screenshot.db` —— 左上为年/月/日下拉（年范围取自
  首末记录、日智能平闰年）与首末记录日期标签，左中为缩略图条带（滚轮上滚看
  早期/下滚看后期、按住左键拖动浏览；中垂线扫到的缩略图停留 1 秒自动选中，
  左键点击跳过延迟直接选中），左下为选中记录的译文，右侧为原图；
- **听歌识曲**：标题/正文窗文字处**右键**快捷菜单的「听歌识曲」—— 纯后台行为
  （无新窗口/弹窗）：录制 `recording_duration`（默认 8 秒）系统音频（soundcard
  环回 → pyaudiowpatch WASAPI 环回 → pyaudio 立体声混音三重回退）交 Shazam 识别；
  标题窗显示录制倒计时（建议关闭游戏音效提升识别率）/录制失败/识曲中/识曲成功/
  识曲失败，成功时正文窗显示「歌曲名/艺术家」，失败提示多次失败可能是该曲目
  未被收录；与翻译/截图翻译共用“同时仅一个 API 请求”互斥（在途时点击被忽略
  且不更新提示），双击可打断录制与识曲（识别结果作废）；成功结果存入游戏目录
  `renpy_overlay_cache/game_song.db`（`game_scene`/`remark` 留空）；
- **识曲历史**：常规窗口浏览 `game_song.db` —— 上部为「查找」输入框与上一个/
  下一个/删除按钮，下部为四列表格（曲名/艺术家/游戏场景/备注），按写入顺序
  滚动浏览；查找精确匹配、不区分大小写并整行高亮，Ctrl+C 复制选中内容；
  删除选中行同步删除数据库记录；游戏场景与备注两列可编辑，回车写入数据库；
- **AI 对话**：标题/正文窗文字处**右键**快捷菜单的「对话」—— 常规窗口向 AI
  提问辅助深入学习外语：上部为「保留发送消息」复选框（默认勾选，取消勾选时
  发送成功后清空输入框）、双击锁定截图窗的色块下拉列表、OCR 与发送按钮，
  下部为无滚动条输入框（空时占位「按 Ctrl+Enter 即可发送。」，输入框内
  Ctrl+Enter 发送）；OCR 识别选中颜色窗口的区域原文（复用截图翻译的隐藏-
  截屏-恢复时序，成功后填充到输入框并置光标末尾）；发送时附加对话专用系统
  提示词（令回复为无标签无格式的纯文本），回复显示在流式正文窗；标题窗提示
  对话发送中.../发送失败/发送成功/OCR中，速度会稍慢.../OCR成功/OCR失败；
  对话不保存上下文，成功后用户消息与回复成对存入
  `renpy_overlay_cache/chat.db`；OCR/发送与翻译/识曲共用“同时只允许一个
  API 调用”互斥（冲突时忽略点击且不更新提示）；
- **对话历史**：快捷菜单「查看历史 → 对话历史」—— 常规窗口按写入顺序浏览
  `chat.db` 的对话成对记录（两列表格：用户消息/AI 回复，外观如两列 Excel）；
  查找为部分文字匹配、不区分大小写（两列都参与），上一个/下一个循环跳转到
  匹配行并整行高亮，左键点击选中，Ctrl+C 复制选中格；删除选中行同步删除
  数据库记录并原位回选；
- **翻译历史**：快捷菜单「查看历史 → 翻译历史」—— 常规窗口按写入顺序浏览
  `translations.db` 的分桶翻译记录（两列表格：原文/译文；同一哈希下多条
  不同原文的记录逐行展开）；查找/复制/选中交互与对话历史一致，删除仅移除
  选中的一组 (哈希, 原文) 记录——同桶其它原文的记录保留，桶空则整个条目
  随之消失；
- **跳过注入（非 Ren'Py 游戏）**：选择进程窗口新增「跳过注入」按钮 —— 经系统
  文件夹选择对话框（IFileOpenDialog）选定数据目录（选择期间选择窗保持打开，
  取消则退回选择窗），随后关闭选择窗并打开悬浮窗：目录名即为
  `renpy_overlay_cache` 则直接使用，其下已包含则使用该子目录，否则自动创建；
  已存在的数据库不会被覆盖。悬浮窗保持置顶（请以窗口化运行游戏配合），
  右键菜单全部功能可用（截图翻译/听歌识曲/AI 对话/各历史窗口，数据库落至
  所选目录）；单击翻译与自动翻译依赖注入，本模式不可用；
- **正文窗尺寸与字号**：`stream_window_width`（默认 1760）/ `stream_window_height`
  （默认 200）控制正文窗大小，字号 / 行距 / 标题字号 / 标题间距可经 `config.json`
  调整（见下方配置示例）；宽度超出游戏窗口时仍按既有规则被钳制；
- 显隐走控制台命令（`h`）；退出可经右键快捷菜单「退出」或控制台 `q`，不再有全局热键。

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
- Rust 工具链（**推荐预先安装**，否则 shazamio 模块可能会安装失败 —— 听歌识曲
  功能依赖）：Windows 从 https://rustup.rs 下载安装；未安装 Rust 时若依赖均能
  从镜像获得预编译 wheel 也可正常安装，其余功能不受影响；
- 目标游戏为 Ren'Py 7.x（32 位）或 8.x（64 位），且已经启动到游戏主循环；
- 工具与游戏为**同一用户**运行。若游戏以管理员身份启动，本工具也需以管理员运行。

## 安装

```bash
uv sync            # 创建虚拟环境并安装依赖（psutil / pywin32 / PyQt6 / shazamio 等）
```

默认通过清华 TUNA 镜像下载（写在 `pyproject.toml` 的 `[[tool.uv.index]]` 里，
因为官方源在本机下载大体积 wheel 时不稳定）。如需换回官方源或其它镜像，
修改该配置，或设置环境变量 `UV_DEFAULT_INDEX`（如
`https://mirrors.aliyun.com/pypi/simple/`）。

## 打包为独立 exe（可选）

不想装 Python 环境时，可以把工具端打成单文件 exe：PyInstaller 打包运行必需的
模块（psutil / pywin32 / tkinter / PyQt6 / PIL / 听歌识曲的 shazamio 链路；包含
Qt DLL 后体积明显增大），排除 scipy 等无关库：

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

- 悬浮窗为全透明双窗口（标题在上、正文在下，左对齐），对话以打字机动画逐字弹出，
  随游戏移动、最小化而隐藏；
- 游戏内每推进一句对话，正文窗实时刷新为最新一条（不累积历史）；
- 按住任一窗口拖动即可整体调整位置；滚轮可回看当前对话全文；双击锁定后
  按住正文文字上下滑动可拖拽滚动文本；
- 双击锁定位置后，单击会把对话原文流式翻译成中文（未锁定不触发）；
- 在 `config.json` 中开启 `auto_translate` 后，锁定状态下会自动翻译最新对话。

### 鼠标操作

| 操作 | 作用 |
| --- | --- |
| 按住标题/正文任一窗 + 左键拖动 | 两窗整体联动移动；松开后位置锁定（相对游戏窗口保持） |
| 左键双击 | 锁定 / 解锁窗口位置（锁定后不响应拖动；双击同时中止在途翻译与听歌识曲） |
| 锁定状态下单击 | 翻译当前对话原文，译文增量流入正文窗（未锁定不触发） |
| 锁定状态下按住正文文字上下滑动 | 拖拽滚动文本：偏离按下点 12px 内防抖不滚，慢速每秒 1 格滚轮，超过 120px 提速 5 倍（未锁定时按住拖动为移动窗口） |
| 右键（标题/正文文字处） | 快捷菜单：创建/销毁截图窗口、听歌识曲、对话、查看历史（翻译历史/截图历史/识曲历史/对话历史）、退出 |
| 截图窗双击 | 锁定/解锁截图窗口（锁定后单击即截图翻译） |
| 鼠标滚轮（悬浮窗内任意位置） | 回看当前对话全文，滚回顶部恢复固定显示第一行（输出过程固定首行；窗口只显示最新一条） |

启动工具的终端里另有控制台命令：`h`（显隐）、`d`（恢复自动停靠）、`u`（卸载代理）、`s`（状态）、`q`（退出）。

### 翻译（可选，OpenAI 兼容服务）

- 默认面向本地 [LM Studio](https://lmstudio.ai/)（启动并加载一个模型，开启本地服务
  即可）；也可通过 `config.json` 切换到 OpenAI / DeepSeek 等任意 OpenAI 兼容平台；
- 锁定悬浮窗（双击）后单击，会把**注入代理捕获的游戏原文**（而非窗口当前显示的文本）
  发给 `api_base_url`（默认 `http://127.0.0.1:1234`），使用 OpenAI 兼容协议（`model`
  留空时自动读取 `/v1/models` 的第一个已加载模型），译文经 SSE 流式接口增量流入正文窗；
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
  | `api_base_url_stanby` | `""` | 备选 API 链路地址：主链路 `api_base_url` 不可达（连接失败/超时）时自动切换到该端点重试；**留空即不启用备选链路** |
  | `model_stanby` | `""` | 备选链路的模型代号；留空自动读取备选端点 `/v1/models` 的第一个已加载模型 |
  | `api_key_stanby` | `""` | 备选链路的 API Key；留空不带鉴权头 |
  | `screenshot_model_stanby` | `""` | 备选链路的识图模型；留空回退 `model_stanby`（与 `screenshot_model` 回退 `model` 的规则一致） |

- 请求在后台线程执行，不阻塞窗口；同一时刻只允许一条在途请求；双击可随时中止；
- 翻译走 SSE 流式接口（`stream: true`），译文增量实时逐块流入正文窗（打字机呈现）；
- **自动翻译**：项目根目录的 `config.json`（首次运行自动创建、已被 `.gitignore`
  排除）控制自动翻译 ——

  ```json
  {
    "auto_translate": false,          // 改为 true 开启自动翻译
    "auto_translate_interval": 3.0,   // 轮询间隔（秒）
    "show_original_text": true,       // 正文区是否随对话显示游戏原文（默认开启）
    "translation_cache_size_kb": 256, // 内存翻译缓存上限（KB，默认 256）
    "stream_window_width": 1760,      // 流式正文窗宽度（像素，最小 240；会被游戏窗口宽度钳制）
    "stream_window_height": 200,      // 流式正文窗高度（像素，最小 80）
    "stream_window_font_size": 14,    // 流式正文窗字号（像素，最小 6）
    "stream_window_line_spacing": 1.45, // 流式正文行距倍数（最小 1.0）
    "stream_window_title_font_size": 8, // 流式标题窗字号（像素，最小 6）
    "stream_window_title_gap": 4,     // 标题窗与正文窗间距（像素，非负）
    "chat_window_width": 1760,        // AI 对话窗宽度（像素；缺省回退流式正文窗宽度）
    "chat_window_height": 200,        // AI 对话窗高度（像素；缺省回退流式正文窗高度）
    "screenshot_compress_percent": 10, // 截图翻译发送 API 前的等比压缩百分比（1~100）
    "screenshot_model": "",           // 识图模型；空则回退 model（需 vision 多模态模型）
    "recording_duration": 8,          // 听歌识曲录制系统音频时长（秒，最小 3）
    "api_base_url": "http://127.0.0.1:1234",  // OpenAI 兼容 API 地址
    "api_timeout": 20.0,              // 请求超时（秒）
    "model": "",                      // 模型代号；空则自动取 /v1/models 的第一个
    "api_key": "",                     // API Key；空则不携带鉴权头（本地服务通常不需要）
    "enable_thinking": false,         // 模型思考模式开关，默认关闭
    "reasoning_effort": "none",       // 关闭思考时的 reasoning_effort 取值
    "api_base_url_stanby": "",        // 备选 API 链路地址；主链路不可达时自动切换，留空即不启用
    "model_stanby": "",               // 备选链路模型；空则自动取备选端点 /v1/models 的第一个
    "api_key_stanby": "",             // 备选链路 API Key；空则不携带鉴权头
    "screenshot_model_stanby": "",    // 备选链路识图模型；空则回退 model_stanby
    // system_prompt 默认使用内置的游戏对话翻译提示词，可按需覆盖（见下表）
  }
  ```

  开启后：悬浮窗**锁定**状态下按间隔轮询最新捕获的对话原文并自动翻译（同一原文
  只自动翻一次；刚进入锁定需等一个完整间隔；解锁即停用并中止在途；手动单击不受影响）。
  配置读取失败或内容非法时回退默认值并记日志，不会导致程序崩溃；
- **原文显示开关**：`show_original_text` 为 `false` 时，正文区不再随对话刷新原文
  （上一条内容保持不动）；对话仍照常捕获、写入日志，并作为翻译请求的唯一输入，
  标题栏的原文前 20 字提示与翻译替换显示均不受影响。分支选项列表同样受其约束 ——
  `false` 时正文不随选项刷新，但标题预览、选项的自动翻译与「→ 已选择：…」提示
  照常工作。
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
- `tests/test_stream_window.py`：悬浮窗定位的 `compute_geometry` / `pair_layout`
  几何（停靠、拖动偏移、两窗拆分布局、松手重定位不跳动）与正文蒙版条带聚合；
- `tests/test_config.py`：本地配置首建默认值、非法 JSON / 类型错误逐项回退、坏文件不覆盖、
  `show_original_text` 与流式窗各配置项的默认 / 合法读取 / 缺失与越界回退；
- `tests/test_translation_cache.py`：分桶共存（同哈希多原文）、桶内覆盖与顺序刷新、
  记录级 FIFO 淘汰（不整桶淘汰）、哈希键稳定性、UTF-8 字节计量、单条超限拒绝；
- `tests/test_translation_store.py`：数据库创建、增量写入、同原文覆盖、同哈希多原文共存、
  旧版单主键数据库自动迁移、重启持久化、目录不可建时的降级；
- `tests/test_screenshot_processing.py`：宽高比约束（0.10~10.00 白填充）、百分比压缩、
  ≤10w 像素缩略图、JPEG/base64 往返；
- `tests/test_screenshot_store.py`：screenshot.db 建库/插入/首末时间戳/升序列表/
  按位读取与降级；
- `tests/test_screenshot_vision.py`：vision 请求体（image_url base64、思考模式方言）、
  译文解析与错误路径（本地 HTTP 桩）；

端到端自测（需要真实游戏）：用 Ren'Py SDK 的 *The Question* 或任一发行版游戏，
按"快速开始"注入后确认：对话实时上屏且只保留最新一条（旧对话不累积）、拖动悬浮窗
后位置保持不被跟随拉回、大段译文输出时首行保持可见、滚轮可回看全文且回到顶部恢复
固定显示、锁定态按住正文文字上下拖拽可两档速度滚动文本、`--unload` 后不再有新对话、
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
│  ├─ picker.py                # Tkinter 选择窗（失败回退命令行菜单；含跳过注入入口）
│  ├─ folder_dialog.py         # IFileOpenDialog 文件夹选择对话框（COM/ctypes，跳过注入用）
│  ├─ injector.py              # 注入 / 卸载 / 状态查询编排
│  ├─ ipc.py                   # NDJSON over TCP 服务端 + 协议编解码
│  ├─ stream_window.py         # 悬浮窗（PyQt6 全透明双窗口：标题+正文、打字机、拖动/锁定/翻译）
│  ├─ quick_menu.py            # 右键快捷菜单管理器（截图窗口池 + 后续新功能菜单扩展点）
│  ├─ screenshot/              # 截图翻译功能包（各窗口相互独立、原子级文件）
│  │  ├─ capture.py            # Pillow 屏幕截图（物理像素，支持多显示器负坐标）
│  │  ├─ processing.py         # 纯函数图像处理（宽高比约束/压缩/缩略图/编码）
│  │  ├─ vision.py             # vision 识别翻译客户端（OpenAI 兼容，urllib）
│  │  ├─ store.py              # screenshot.db 存取（可降级，仅主线程读写）
│  │  ├─ chat_store.py         # chat.db 存取（AI 对话成对记录，可降级，仅主线程读写）
│  │  ├─ chat_window.py        # AI 对话窗口（复选框+锁定窗口色块下拉+OCR/发送）
│  │  ├─ chat_history_window.py # 对话历史浏览窗口（两列表格：查找/删除/复制）
│  │  ├─ window.py             # 单个截图窗口（8 色边框、八向拉伸、双击锁定）
│  │  ├─ window_visual.py      # 截图窗绘制与边缘检测纯函数
│  │  └─ history_window.py     # 截图历史浏览窗口（缩略图条带+原图+译文）
│  ├─ translator.py            # LM Studio 翻译客户端（OpenAI 兼容，标准库 urllib，支持 SSE 流式）
│  ├─ translation_cache.py     # 翻译内存缓存（哈希键→(原文,译文)，FIFO，仅主线程读写）
│  ├─ translation_store.py     # 翻译 SQLite 持久化（游戏目录 renpy_overlay_cache/，可降级）
│  ├─ translation_history_window.py # 翻译历史浏览窗口（分桶展开两列表格：查找/删除/复制）
│  ├─ config.py                # 本地配置加载（config.json：窗口尺寸 / 自动翻译 / API / 截图）
│  ├─ song_recognition/        # 听歌识曲功能包（纯后台识别 + 历史浏览）
│  │  ├─ recorder.py           # 系统音频录制（三重回退：环回/立体声混音，可中断）
│  │  ├─ recognizer.py         # Shazam 识别封装 + 结果提取/展示纯函数
│  │  ├─ store.py              # game_song.db 存取（可降级，仅主线程读写）
│  │  └─ history_window.py     # 识曲历史浏览窗口（四列表格：查找/删除/备注编辑）
│  └─ payload/                 # 注入到游戏进程内执行的源码（py2/py3 兼容）
│     └─ agent.py              # Hook 安装、上报线程、心跳、shutdown
└─ tests/
   ├─ test_shellcode.py
   ├─ test_pe.py
   ├─ test_ipc.py
   ├─ test_payload.py
   ├─ test_translator.py
   ├─ test_translator_stream.py
   ├─ test_stream_window.py
   ├─ test_translation_cache.py
   ├─ test_translation_store.py
   ├─ test_screenshot_processing.py
   ├─ test_screenshot_store.py
   ├─ test_screenshot_vision.py
   ├─ test_chat_store.py
   ├─ test_chat_window.py
   ├─ test_chat_history.py
   ├─ test_translation_history.py
   ├─ test_skip_injection.py
   ├─ test_song_recognition.py
   ├─ test_song_recognition_store.py
   ├─ test_song_recognition_history.py
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
