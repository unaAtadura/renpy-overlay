# 测试体系

对应目录：`tests/`（70 个测试文件）。运行命令：

```bash
uv run pytest          # 单元测试
uv run ruff check .    # 静态检查
```

## 1. 测试约定

- **全部离线**：不依赖真实 Ren'Py 游戏、真实窗口与真实网络。网络客户端用本地 HTTP 桩（`test_translator*.py`、`test_screenshot_vision.py`）；Win32/Qt 依赖以 stub 注入构造函数（`test_mouse_lock.py`、`test_remote_automation.py`、`test_video_recorder.py` 等）；离线测试不构造真实窗口；
- **纯函数优先**：几何（`stream/geometry.py`）、滚动/蒙版（`stream/scroll_logic.py`）、图像处理（`screenshot/processing.py`）、文本清洗（`pretranslate/cleaning.py`）、并发控制（`pretranslate/concurrency.py`）等核心逻辑都抽成纯函数以便离线验证；
- **PE 解析自证**：`RemotePE` 的 RVA 语义 reader 让"解析本进程已加载的 kernel32.dll"与"解析远程进程内存"走同一套代码（`test_pe.py` 用 `GetProcAddress` 交叉验证，并覆盖转发导出、`python3.dll` 转发层排除等分支）；
- **payload 兼容性静态断言**：`test_payload.py` 断言 `agent.py` 无 f-string / 注解 / nonlocal 等 Python 2.7 不支持的语法，并验证引导模板占位符与 base64 嵌入内容的往返一致性。

## 2. 测试文件 → 覆盖范围映射

### 注入与底层（→ [injection.md](injection.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_shellcode.py` | x86/x64 引导桩的字节级布局与 RIP 相对位移断言、桩长度常量一致性 |
| `test_pe.py` | 本进程 kernel32.dll 内存映射当样本、`GetProcAddress` 交叉验证、转发导出与 python3.dll 转发层排除 |
| `test_win32api.py` | Win32 封装冒烟 |
| `test_skip_injection.py` | `resolve_cache_dir` 三规则 |
| `test_cli.py` | 参数解析与主流程编排 |

### 协议与游戏端（→ [ipc.md](ipc.md)、[dialogue-capture.md](dialogue-capture.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_ipc.py` | NDJSON 编解码、token 握手、非法报文容错、心跳、断开回调 |
| `test_payload.py` | agent.py 的 py2.7 语法兼容静态断言、引导模板占位符与 base64 往返校验 |
| `test_logs.py` | 单行 formatter、级别映射、游戏端回灌、旁路输出接管 |

### 悬浮窗（→ [overlay-window.md](overlay-window.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_stream_window.py` | `compute_geometry`/`pair_layout`/`pair_offset_from_body` 几何（停靠、拖动偏移、两窗拆分、松手重定位不跳动）、正文蒙版条带聚合、拖拽滚动速率、text_logic 实时文本 |
| `test_stream_window_style2.py` | 样式 2 的整段上屏路径与非法样式回退 |
| `test_quick_menu_control.py` | 快捷菜单控制类菜单项（截图窗创建/销毁、快捷键模式、锁鼠标等）状态联动 |
| `test_quick_menu_video.py` | 录制视频 / 视频剪辑 / 实时语音识别菜单项状态与文案 |

### 翻译与预构建（→ [translation.md](translation.md)、[pretranslate.md](pretranslate.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_translator.py` | OpenAI 兼容协议往返、空文本拒绝、协议缺字段报错、服务不可达收敛 `TranslationError`（本地 HTTP 桩） |
| `test_translator_stream.py` | SSE 桩：逐块产出与顺序、`stream: true` 请求体、生成器惰性、错误路径 |
| `test_translation_cache.py` | 分桶共存（同哈希多原文）、桶内覆盖与顺序刷新、记录级 FIFO 淘汰（不整桶淘汰）、哈希键稳定性、UTF-8 字节计量、单条超限拒绝 |
| `test_translation_store.py` | 建库、增量写入、同原文覆盖、同哈希多原文共存、旧版单主键自动迁移、重启持久化、目录不可建降级 |
| `test_translation_history.py` | 历史窗口数据装配 |
| `test_pretranslate_parser.py` | say 三形态 / menu / tl strings / python 块跳过 / 三引号跨行 / rpa 索引与解包 |
| `test_pretranslate_cleaning.py` | `clean_text` 与 agent 口径的样本表钉住、`build_choice_key` |
| `test_concurrency.py` | AIMD 升降级、时延闸门、熔断 |
| `test_pretranslate_runner.py` | 两阶段流水、重试、熔断收束、取消语义、单条单事务落库 |
| `test_prebuild_formatting.py` / `test_prebuild_window.py` | 进度文案纯函数、三页签窗口状态机 |
| `test_external_tools.py` / `test_tools_runner.py` / `test_overlay_paths.py` | 子进程执行与超时强杀、反编译/解包任务幂等跳过、unrpa 路径映射 |

### 截图与视觉（→ [screenshot.md](screenshot.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_screenshot_processing.py` | 宽高比约束（0.10~10.00 白填充）、百分比压缩、≤10w 像素缩略图、JPEG/base64 往返 |
| `test_screen_capture.py` | 虚拟屏原点与区域裁剪、无效区域拒绝 |
| `test_screenshot_vision.py` | vision 请求体（image_url base64、思考模式方言）、译文解析、CJK 占比校验与强化重试、错误路径（HTTP 桩） |
| `test_screenshot_store.py` | screenshot.db 建库/插入/首末时间戳/升序列表/按位读取与降级 |
| `test_screenshot_history.py` | 历史窗口数据装配与导出魔数判定 |
| `test_screenshot_window_visual.py` | 截图窗绘制与边缘检测纯函数 |
| `test_screenshot_hotkeys.py` | 快捷键模式状态机（开关联动热键注册、数字键命中锁定窗口、模式退出注销） |
| `test_mouse_lock.py` | 锁鼠标状态机（双击锁定/逃脱热键解锁、ClipCursor 施加与解除、提示文案，全 stub） |
| `test_remote_screenshot.py` | A/B 圆窗、装填变形与驻留、触发分发 |
| `test_remote_automation.py` | 32x16 感知签名、变化检测、循环队列缓存命中、周期调度、写库规则 |

### 识曲 / ASR / 对话（→ [song-recognition.md](song-recognition.md)、[asr.md](asr.md)、[ai-chat.md](ai-chat.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_song_recognition.py` | 录制三重回退与中断、识别封装、结果提取与展示纯函数 |
| `test_song_recognition_store.py` / `test_song_recognition_history.py` | music_master 建库/写入/删除/备注更新、历史窗口装配 |
| `test_video_asr.py` | 通用/专用 ASR 模型两种请求形态、Data URL 载荷、三键回退链 |
| `test_video_asr_rt.py` | 事件映射（delta/completed/error）、WS 端点转换、重采样、开启校验链、互斥槽占用与释放 |
| `test_video_asr_store.py` / `test_video_asr_history.py` | asr.db 建库/写入/大字段按需读取、历史窗口装配 |
| `test_chat_store.py` / `test_chat_window.py` / `test_chat_history.py` | chat.db 成对写入/删除、对话窗口状态、历史装配 |

### 视频（→ [video.md](video.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_video_recorder.py` | 录制状态机（开始/停止/意外退出中止收尾）、降级链、临时文件清理 |
| `test_video_audio_capture.py` | 持续环回三案回退与停止 |
| `test_video_av_media.py` | extract_wav 区间整理与样本数推进、merge_streams 时间戳归零与 -shortest 截断、布局归一化 |
| `test_video_clip_flow.py` | 剪辑窗口与宿主接线 |
| `test_clip_*.py`（18 个） | 命令流/导出器/几何/日志面板/切片标记/解码器/引擎/关键帧索引/队列/流选择/同步时钟/资源面板/字幕/时间轴模型/音量同步/波形（模型与视图） |

### 配置与设置（→ [config.md](config.md)）

| 测试 | 覆盖点 |
| --- | --- |
| `test_config.py` | 本地配置首建默认值、非法 JSON/类型错误逐项回退、坏文件不覆盖、全部键的默认/合法读取/缺失与越界回退 |
| `test_settings_window.py` | 双栏切换重读、合并写盘、热重载反馈 |

## 3. 端到端自测（需真实游戏）

单元测试无法覆盖真实注入链路，需用 Ren'Py SDK 的 *The Question* 或任一发行版游戏人工确认：

1. 注入后对话实时上屏且只保留最新一条（旧对话不累积）；
2. 拖动悬浮窗后位置保持、不被跟随拉回；
3. 大段译文输出时首行保持可见；滚轮可回看全文、回到顶部恢复固定显示；
4. 锁定态按住正文文字上下拖拽可两档速度滚动文本；
5. `--unload` 后不再有新对话；关闭游戏后工具自动退出；
6. 日志无残留线程报错。

## 交叉链接

- 各测试对应的行为实现：见各功能文档
- 离线单元测试的工程约定（依赖注入与安全构造）：开发规范层面约定，见仓库开发规范文档
