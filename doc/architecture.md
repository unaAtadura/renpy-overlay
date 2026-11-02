# 总体架构与线程模型

本文是全部技术文档的总览：先讲清"双进程 + 单通道"的整体形态，再给出端到端数据流、线程模型、互斥模型与模块地图。各专题细节见文末交叉链接。

## 1. 双进程架构

系统由两个协作进程组成：

- **工具端**（本程序，`src/renpy_overlay/`）：负责进程发现、注入、接收数据、渲染悬浮窗、全部辅助功能（翻译 / 截图 / 识曲 / ASR / 视频）与退出清理。PyQt6 为硬依赖，缺失时启动即报清晰 `ImportError`（`cli.py` 顶层导入 `stream_window`）。
- **游戏端**（注入代理，`src/renpy_overlay/payload/agent.py`）：跑在 Ren'Py 游戏进程内部，Hook 引擎回调捕获对话，经 `127.0.0.1` 的 NDJSON/TCP 通道推送给工具端。该文件不参与工具端 import，而是由 `injector.agent_source()` 经 `importlib.resources` 读成字节，随引导代码送进目标进程执行，因此必须兼容 Python 2.7 / 3.x（ruff 对 `payload/*.py` 关闭现代化规则，见 `pyproject.toml`）。

```
┌─────────────────────────┐         NDJSON over TCP          ┌──────────────────────────┐
│ Ren'Py 游戏进程          │   127.0.0.1:<随机端口>            │ renpy-overlay（本程序）    │
│  ┌───────────────────┐  │  ──────────────────────────►     │  ┌────────────────────┐  │
│  │ payload/agent.py   │  │   hello/say/choice/hooks/        │  │ DialogueLink(服务端)│  │
│  │ （注入的 Hook 代理）│  │   hb/log/bye                     │  └─────────┬──────────┘  │
│  └───────────────────┘  │  ◄──────────────────────────      │            ▼             │
└─────────▲───────────────┘      （重连、token 握手）           │  ┌────────────────────┐  │
          │  CreateRemoteThread + pythonXX.dll 导出调用         │  │ 悬浮窗（置顶＋拖动） │  │
          └─────────────────────────────────────────────────── │  └────────────────────┘  │
                        注入由工具端发起                        └──────────────────────────┘
```

另有一个**跳过注入模式**（非 Ren'Py 游戏）：不经注入直接打开悬浮窗，右键菜单中不依赖游戏文本的功能（截图翻译 / 听歌识曲 / AI 对话 / 各历史窗口）全部可用；单击翻译与自动翻译不可用。入口在 `picker.choose_target` 的 `SkipSelection` 结果，实现在 `cli._run_skip_injection_mode`。

## 2. 端到端数据流（以一句对话为例）

1. 游戏推进一句对白 → `agent.py` 的某个 Hook 层捕获（详见 [dialogue-capture.md](dialogue-capture.md)）；
2. agent 端清洗（`_clean_text` 去 `{...}` 标签）、按 `(who, what)` 去重后入 `collections.deque` 队列（上限 `max_queue=512`，满则丢最旧）；
3. agent 发送守护线程 `_sender_loop` 每 0.2s 醒来，把队列刷成 NDJSON 行写入 socket；断线按 0.5s→5s 指数退避重连，每 2s 心跳；
4. 工具端 `ipc.DialogueLink`（服务端）的 reader 线程按行解码、分发（详见 [ipc.md](ipc.md)）；
5. `cli.Session._on_message` 收到 `t=say` → 若悬浮窗未就绪则暂存 `_pending_say`（只留最新一条，就绪后补推）→ 已就绪则调 `overlay.push_say(who, what)`；
6. `push_say` 只是把消息放进 Qt 侧队列 `_queue`（**所有跨线程输入都进队列**）；
7. Qt 主线程的 QTimer（`DRAIN_INTERVAL_MS = 80`）统一消费队列 → 正文窗以打字机动画逐字呈现（详见 [overlay-window.md](overlay-window.md)）；
8. 若处于锁定状态且启用了自动翻译，`auto` QTimer（间隔 `auto_translate_interval`）查两级缓存或发起 SSE 流式翻译，译文增量经同一队列回流上屏（详见 [translation.md](translation.md)）。

注入确认（`t=hooks`）走同样的队列路径：`Session` 收到后调 `overlay.mark_injected()`，标题窗并入入口条（预构建 / 定位翻译位置 / 切换翻译模式图标）。hooks 消息可能早于悬浮窗创建，`Session._flush_injected` 负责幂等补发。

## 3. 线程模型汇总

| 线程 | 创建处 | 职责 | 约束 |
| --- | --- | --- | --- |
| Qt 主线程 | `Session._run_overlay_mode` | 全部界面绘制、队列消费（80ms）、跟随定位（200ms）、自动翻译轮询、录制定时器、所有 store 读写 | **唯一允许触碰 Qt 对象与 SQLite 存储的线程** |
| IPC accept 线程 | `DialogueLink.start` | `accept()` 新连接（0.5s 超时轮询） | daemon |
| IPC reader 线程 | `DialogueLink._accept_loop` | 每连接一个，按行解码 + 首包 token 认证 + 分发回调 | daemon；回调异常只记日志不断线 |
| 游戏进程监控线程 | `Session._start_monitor` | 每 2s 检查游戏 PID 存活与最后消息时间；游戏退出后 `--idle-exit`（默认 20s）触发清理 | daemon |
| 控制台命令线程 | `Session._start_console` | 读 stdin：`h` 显隐 / `d` 恢复停靠 / `u` 卸载 / `s` 状态 / `q` 退出 | daemon；stdin 非终端时跳过 |
| 流式翻译线程 | `translate_flow._start_translation` | 执行 SSE 请求，逐块投递 `("chunk", ...)`，结束投递 `("translation", ...)` | daemon；同一时刻只允许一条在途 |
| 批量预构建线程池 | `pretranslate.runner.BatchTask` | `ThreadPoolExecutor`，动态并发上限由 `AdaptiveLimiter` 控制 | 后台守护线程驱动 `run()` |
| 截图/识图/对话请求线程 | 各 flow Mixin | 图像处理 + 网络请求，结果经队列回主线程 | daemon |
| 听歌识曲线程 | `song_flow` | 录音（阻塞式）→ Shazam 识别 → 结果入队 | daemon；双击可打断 |
| 录屏编码线程 | `video.recorder` | gdigrab 抓屏 + libx264 编码到 mkv，逐帧检查 stop_event（≤1 帧间隔退出） | daemon；结束经 `pyqtSignal` queued 回主线程 |
| 环回录音线程 | `video.recorder` | 持续录系统音频到临时 wav，块间（~23ms）检查 stop_event | daemon |
| 实时 ASR 采集线程 | `video.asr_rt` | 100ms 一片 loopback 采集 → 16kHz PCM16 → 推流 | daemon；dashscope 回调线程只入队 |
| agent 发送线程（游戏端） | `agent.start` | 重连、刷队列、心跳 | daemon |

## 4. 互斥模型（"同一时刻只允许一个 API 请求"）

工具端用三个布尔标志维护单一 API 互斥槽：`_translating`（流式翻译）、`_recognizing`（听歌识曲）、`_chat_busy`（AI 对话）。判定集中在 `translate_flow._start_translation` 与 `_auto_translate_check`：

- 任一标志为真时，新的翻译 / 截图翻译 / 对话 / 识曲请求**直接忽略**（不更新提示）；
- 实时语音识别开启期间经 `acquire`/`release` 注入的互斥槽把上述全部入口阻塞（双向：实时识别开启本身也要求槽空闲），标题提示「实时语音识别中，阻塞其余API请求」；
- **预构建翻译缓存不受互斥约束**（后台独立链路，写库经进程级写锁与前台串行化）；但实时语音识别开启期间预构建入口同样被阻塞（双向互斥）；
- 视频录制、遥控器自动化的截图动作不占翻译互斥槽（自动化在提交 API 前仍检查在途状态）。

## 5. 模块地图

```
src/renpy_overlay/
├─ cli.py                  主流程编排 + 退出清理           → architecture.md
├─ logs.py                 日志基础设施（见 §6）            → architecture.md
├─ win32api.py             ctypes Win32 封装               → injection.md
├─ pe.py / shellcode.py / injector.py / discovery.py       → injection.md
├─ picker.py / folder_dialog.py  选择窗 / 目录对话框        → injection.md
├─ ipc.py                  NDJSON/TCP 服务端               → ipc.md
├─ payload/agent.py        游戏端代理                      → dialogue-capture.md
├─ stream_window.py + stream/   悬浮窗                     → overlay-window.md
├─ translator.py / translation_cache.py / translation_store.py
├─ translation_history_window.py                           → translation.md
├─ pretranslate/ + prebuild_window.py + external_tools.py + tools_runner.py
│                                                          → pretranslate.md
├─ screenshot/             截图翻译功能包                  → screenshot.md
├─ song_recognition/       听歌识曲包                      → song-recognition.md
├─ video/                  录制 / ASR / 剪辑               → video.md, asr.md
├─ config.py / settings_window.py                          → config.md
├─ quick_menu.py           右键快捷菜单管理器               → overlay-window.md
├─ bound_window.py         绑定窗口 / 标题入口条            → overlay-window.md
└─ 5 个 SQLite store       数据库                          → history-and-storage.md
```

## 6. 日志系统（logs.py）

- 双通道：控制台输出 INFO 及以上；文件 `logs/renpy_overlay_<时间戳>.log` 记录 DEBUG 全量（含注入导出地址、线程退出码、Hook 层注册结果），格式含 pid 与线程名；
- **单行保证**：`_SingleLineFormatter` 把消息内嵌换行转义为字面 `\n`，一条记录严格占一行（Qt 两行 DPI 警告裸奔问题的修复）；
- **三类旁路输出接管**：Qt 诊断消息（`qInstallMessageHandler`，按 `QtMsgType` 枚举对象映射级别）、被 Python 忽略的异常（`sys.unraisablehook`）、warnings（`logging.captureWarnings`），全部转为标准日志；
- **游戏端回灌**：agent 的 `_log()` 经 IPC 发 `t=log`，工具端 `log_from_game` 写入同一 logger（`renpy_overlay.game` 前缀），注入端与游戏端时序在一条时间线上；
- `force_utf8_stdio()` 在参数解析前执行，保证 GBK 控制台下中文与 `--help` 正常输出；
- 日志目录不可写时降级为仅控制台，不影响启动。

## 7. 退出与清理（cli.Session._cleanup）

清理顺序刻意固定，任何一步失败只记日志不中断后续：

1. 先远程卸载游戏内代理（`injector.unload_agent()`，注入引导执行 `agent.shutdown()`——停止产生新消息）；
2. 停止接收端（`DialogueLink.stop()`：关客户端、关服务端、join 线程）；
3. 销毁界面（`overlay.close()`）；
4. 释放远程内存与句柄（`Injector.close()`）。

兜底：`atexit.register(session._cleanup, False)` 保证异常退出路径也不留句柄与远程内存。游戏正常退出时 agent 通过 `config.python_exit_callbacks` 自动执行 `shutdown()` 反注册全部 Hook。远程线程等待超时（默认 15s）是唯一不清内存的场合——宁可少量泄漏也不动可能仍在执行的代码页。

## 交叉链接

- 注入链路细节：[injection.md](injection.md)
- 游戏端 Hook：[dialogue-capture.md](dialogue-capture.md)
- 通信协议：[ipc.md](ipc.md)
- 悬浮窗：[overlay-window.md](overlay-window.md)
- 翻译与缓存：[translation.md](translation.md)
- 存储：[history-and-storage.md](history-and-storage.md)
- 配置：[config.md](config.md)
