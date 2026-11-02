# 技术文档索引（doc/）

本目录收录 renpy-overlay 的全部**技术文档**：实现原理、架构设计、注入链路、Hook 机制、IPC 协议、数据存储与缓存结构、并发与性能策略、模块职责、测试与代码级细节。所有内容以**当前代码的实际实现为准**（撰写时逐文件核对过源码），每个文档都标注了对应的源码范围。

用户视角的使用说明（软件介绍、亮点、环境搭建、快速上手、操作与配置的使用层说明）见根目录 [README.md](../README.md)；本文档讲"怎么实现、代码在哪"。

## 文档列表

| 文档 | 一句话简介 | 覆盖主题 | 对应代码 |
| --- | --- | --- | --- |
| [architecture.md](architecture.md) | 双进程架构总览、端到端数据流、线程与互斥模型、日志与退出清理 | 总体设计 | `cli.py`、`logs.py`、`stream/`（装配层） |
| [injection.md](injection.md) | 进程发现打分、PE 导出表解析、引导桩机器码、注入编排与跳过注入 | 发现与注入 | `discovery.py`、`win32api.py`、`pe.py`、`shellcode.py`、`injector.py`、`picker.py`、`folder_dialog.py` |
| [dialogue-capture.md](dialogue-capture.md) | 注入代理的多层 Hook、去重、选项捕获、重连上报与卸载 | 游戏内对话捕获 | `payload/agent.py` |
| [ipc.md](ipc.md) | NDJSON over TCP 通道：拓扑、报文类型表、握手与竞态处理 | 通信协议 | `ipc.py` + agent 发送侧 |
| [overlay-window.md](overlay-window.md) | 全透明双窗口渲染、打字机、几何跟随、鼠标交互、入口条与快捷菜单 | 悬浮窗渲染与交互 | `stream_window.py`、`stream/`、`quick_menu.py`、`bound_window.py` |
| [translation.md](translation.md) | OpenAI 兼容客户端、单击/自动翻译、内存+SQLite 两级缓存、历史窗口 | 翻译链路与缓存 | `translator.py`、`translation_cache.py`、`translation_store.py`、`stream/translate_flow.py`、`translation_history_window.py` |
| [pretranslate.md](pretranslate.md) | 脚本扫描解析、清洗口径对齐、AIMD 自适应并发、三页签窗口、unrpyc/rpaextract | 预构建翻译缓存 | `pretranslate/`、`prebuild_window.py`、`external_tools.py`、`tools_runner.py` |
| [screenshot.md](screenshot.md) | 截图窗口、视觉识别、快捷键模式、锁鼠标、遥控截图与自动化 | 截图翻译与视觉识别 | `screenshot/`、`stream/screenshot_flow.py` |
| [ai-chat.md](ai-chat.md) | AI 对话窗口、OCR 链路、成对落库与历史窗口 | AI 对话 | `screenshot/chat_window.py`、`chat_store.py`、`chat_history_window.py` |
| [song-recognition.md](song-recognition.md) | 系统音频三重回退录制、Shazam 识别、识曲落库与历史 | 听歌识曲 | `song_recognition/`、`stream/song_flow.py` |
| [asr.md](asr.md) | 非实时转写（字幕制作）与 DashScope 实时流式识别 | 语音识别 | `video/asr.py`、`asr_rt.py`、`asr_store.py`、`asr_history_window.py`、`stream/text_logic.py` |
| [video.md](video.md) | gdigrab 双流录制与合并、SDL2 播放引擎、剪辑模型与导出 | 视频录制与剪辑 | `video/`（recorder、av_media、clip/） |
| [history-and-storage.md](history-and-storage.md) | 5 个 SQLite 库的表结构汇总、公共降级模式、各历史窗口交互 | 历史记录与数据库 | 各 `*_store.py`、各 `*history_window.py` |
| [config.md](config.md) | config.json 全键表（默认/下限/回退）、合并写盘、双栏设置与热重载 | 配置系统 | `config.py`、`settings_window.py`、`stream/host_core.py` |
| [build-and-packaging.md](build-and-packaging.md) | 依赖选型、uv 命令、PyInstaller onedir/onefile 打包要点 | 打包与构建 | `pyproject.toml`、`renpy_overlay.spec`、`pyinstaller_entry.py` |
| [testing.md](testing.md) | 离线测试约定、70 个测试文件的覆盖映射、端到端自测清单 | 测试体系 | `tests/` |

## 阅读路径建议

- 初次了解全貌：[architecture.md](architecture.md) → [injection.md](injection.md) → [dialogue-capture.md](dialogue-capture.md) → [ipc.md](ipc.md)；
- 改某个功能前：先看对应功能文档的"对应源码"行，再读 [architecture.md](architecture.md) §3 确认线程约束；
- 排查问题：日志体系见 [architecture.md](architecture.md) §6；降级行为见 [history-and-storage.md](history-and-storage.md) §1 与各功能文档。

## 文档维护约定（本目录与根 README 的分工）

自 2026-11 文档重构起执行：

1. **技术内容一律写进本目录**：以往"同步更新 README"的实现层内容（实现原理、架构、协议、数据结构、并发策略、模块结构、测试细节），今后一律同步更新到上表对应的主题文档；README 不再承载实现级细节。
2. **README 只维护五类内容**：软件介绍、软件亮点、环境搭建、快速上手、使用说明（含操作方式、菜单含义、配置项的使用层说明、常见问题排障的用户侧指引、许可证与合规）。仅当功能变动影响这五类之一时才改 README。
3. **一次变更同时涉及两者时，保证描述一致**：README 讲"怎么用、有什么用"，技术文档讲"怎么实现、代码在哪"；两边如有交叉引用，用相对链接（README → `doc/xxx.md`，doc 内互链用同目录相对路径）。
4. **新增功能**：先在上表登记（标题、一句话简介、对应代码），再建/改主题文档；主题拆分粒度以代码实际结构为准，新功能优先并入既有主题，确属新主题才建新文件。
5. **文档与代码不一致时以代码为准**，并应尽快修订文档；已知 README 旧描述与代码不符的差异需在对应文档中显式说明（先例：[dialogue-capture.md](dialogue-capture.md) §2 的菜单 Hook 点）。
