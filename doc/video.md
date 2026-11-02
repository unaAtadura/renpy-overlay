# 视频录制与剪辑

对应源码：`src/renpy_overlay/video/`（recorder / screen_capture / audio_capture / av_media）与 `video/clip/` 子包（剪辑窗口、SDL2 播放引擎、剪辑模型）；宿主入口 `stream/video_flow.py`。测试：`test_video_recorder.py`、`test_video_audio_capture.py`、`test_video_av_media.py`、`test_screen_capture.py`、`test_clip_*.py` 系列（18 个文件）、`test_video_clip_flow.py`。

## 1. 视频录制（recorder.py / screen_capture.py / audio_capture.py）

快捷菜单「录制视频」开关式录制全屏（含系统音频），保存为 `renpy_overlay_cache/video/yyyyMMdd-hhmmss.mp4`（`default_video_dir`，目录不存在自动创建）。录制中菜单项显示「录制视频 mm:ss」；开始 / 每 10 分钟（`PROGRESS_INTERVAL_MS = 10*60*1000`）/ 结束提示走标题窗。录制不经 API、不占翻译/识曲互斥。

**双流方案**：

- **视频流**：PyAV（进程内 FFmpeg 库，pip 依赖自带动态库，无需自备 ffmpeg.exe）gdigrab 抓屏 + libx264 编码，统一录到 **mkv 中间容器**（`<时间戳>.tmp.mkv`）——无 moov 依赖、簇式容错封装：即使异常未 finalize，已写数据仍可被 remux；
- **音频流**：`audio_capture.record_loopback` 后台线程持续环回录音到临时 wav（三重回退与识曲同源，可用性标志直接复用识曲模块单一事实来源；区别在时长语义——持续录制直到 stop_event 置位，wav 边录边写、停止后 `wave.close` 补全 RIFF 头）；无可用录音方案时降级仅视频。

**编码参数**（`screen_capture.record_screen`，daemon 线程）：gdigrab options `framerate/offset_x/offset_y/video_size`（**物理像素几何**——`physical_geometry` 把 Qt 逻辑像素乘 `devicePixelRatio` 换算，DPR≠1 屏幕上直接用逻辑值会只录到左上一块）；编码器 `libx264`、`pix_fmt yuv420p`、`preset ultrafast`、fps `DEFAULT_RECORD_FPS = 30`；**尺寸必须显式设置**（PyAV 19 的 `add_stream` 预置 640x480 且不随输入帧更新）。逐帧检查 stop_event——协作式停止，置位后 ≤1 帧间隔退出并正常 finalize。

**停止状态机**（`VideoRecorder`，Qt 主线程 + 信号）：`_recording/_stopping` 双标志防重入；停止顺序固定：置停止事件 → 编码线程 `_capture_done` 信号（queued 回主线程）→ stop_event 停录音并 join（等 wav 头写完）→ `merge_streams` 封装 → 清理临时文件 → 提示结束。编码线程意外退出或失败按**中止收尾**（不保留产物）。封装失败回退：mkv 中间文件改名为 `.mkv` 最终产物（不丢录像）。

**双流合并**（`av_media.merge_streams`）：视频流 packet copy（`add_stream_from_template`）+ aac 音轨 → mp4；仅视频时退化为单流 remux（mkv → mp4，秒级）。关键细节：

- **时间戳归零**：gdigrab 用系统启动起的墙钟 pts，与音频流（零起点）不同空间，交错 mux 会 EINVAL——首个视频包 pts 记为基点后逐包平移到零；
- **`-shortest` 语义**：mkv 容器级 duration 不可靠，逐包累计视频终点时长，音频解码到 `frame.time >= video_end_s` 截断（截短不补齐）；
- **aac 布局归一化** `_native_layout`：wav 读回的布局常为非命名形式（`'2 channels'`），FFmpeg 8 系编码器只接受 native 命名布局（否则 `avcodec_open2` 返回 EINVAL）——按声道数映射 mono/stereo/5.1 等；音频流须在首个包 mux 前添加并配置（含显式 `time_base`，否则 mux rebase 遇 0/1 时基报错）。

`av_available()`：惰性 import 探测 PyAV（结果进程内缓存）；PyAV 不可用时录制降级提示、字幕制作/剪辑不可用，不影响其它功能。

## 2. 剪辑主窗口（clip/editor_window.py）

快捷菜单「视频剪辑」打开。三区布局：**资源窗口 | 视频播放器 | 日志窗口**；下部快捷按钮行 → 时间轴 → 声波图。

- **资源面板**（resource_panel.py）：导入视频/文件夹（双击载入，默认打开 video 目录）、排序；
- **日志区域**（log_panel.py）：内部进度通道——导出/字幕/载入等任务进度与状态**显式上报**（区别于 root logger 桥接的诊断通道），默认简洁模式恒可见、切换显示等级不丢失；「调试」按钮切换 DEBUG 诊断通道显示（root logger 桥接）；
- **快捷键**（窗口激活时生效，见 §4 内存时序）：space 暂停/播放、←/→ 步退/步进 1 秒、Ctrl+←/→ 步进 1 帧（均支持长按 2 秒后 0.5 秒间隔连发）、x 分割 / Del 删除 / c 合并 / Ctrl+z 退回 / f 标记切片（选中声波图才生效的分割/删除除外）；
- **时间轴**（timeline_view.py）与**声波图**（waveform_view.py）可缩放、点击 seek；声波图支持切片蒙版预览（点击蒙版跳到切片起点播放至终点自动暂停）与片段拖拽重排；共用视图状态 `view_state.py`（缩放/窗口）；波形数据由 `waveform.py` 提取并落 **sidecar 缓存**；声波图绘制按可视窗口裁剪（整段逐列绘制会因 GIL 饥饿实时线程）。

## 3. SDL2 播放引擎（clip/player/）

- `engine.py` 编排 + `decoder.py`（PyAV 解码）+ `queues.py`（双缓冲包/帧队列）+ `audio_output.py`（SDL2 音频输出）+ `renderer_sdl2.py`（SDL2 视频渲染）+ `sync_clock.py`（**音频主时钟**）+ `keyframe_index.py`（关键帧索引）+ `source_filter.py`（流选择）+ `stats.py`；
- 特性：异步 seek **合并节流**（连续拖拽不花屏不卡死）、关键帧索引加速定位、音频主时钟驱动同步；`probe.py` 轻量探测媒体元数据（不启引擎）；
- **全屏**（fullscreen_player.py）：「全屏」按钮进入 SDL 独立顶层窗口（`fullscreen_mode: "sdl"`，默认；Esc 退出，悬浮面板控制播放）；`"qt"` 为 Qt 顶层画布重绑保底回退路径（存在窗口不显示风险，故非默认；非法值回退 qt）。

## 4. 剪辑模型（clip/editing/）

- `model.py`：时间轴 / 切片标记数据模型；`clip_marker.py`：f 键切片标记（**重叠分步时序**：一次按键只做一步变更）；`commands.py`：命令流（Ctrl+z 退回、c 合并、Del 删除）；
- `exporter.py`：**导出两模式**——重编码（帧精确，CRF 可调默认 18）或关键帧吸附（无损流拷贝，边界对齐关键帧）；导出前**预估大小**（三级回退估算）、可中断（`_ExportWorker` QThread，半成品自动清理）；
- `subtitle.py`：「制作字幕」逐切片截取音轨转 16k wav（`av_media.extract_wav`）发 ASR 模型转写 + 翻译成中文，生成**双语 ASS 字幕**（与视频同名）；制作期间占用 API 互斥槽（与实时语音识别同模式阻塞其余请求）；再次点击停止并保留已落盘内容；「加载字幕」解析同名 ASS 映射为切片标记。

## 5. 与宿主的降级关系

- PyAV 缺失：录制降级提示「视频录制不可用：视频组件初始化失败」；剪辑窗口内解码/导出/字幕功能不可用并提示；
- SDL2（pysdl2-dll）缺失：播放引擎不可用提示；全屏可走 qt 回退；
- 无录音库：录制降级仅视频。

## 交叉链接

- 音轨截取与合并实现细节：本文 §1 / [asr.md](asr.md) §1
- 制作字幕的 ASR 客户端：[asr.md](asr.md)
- 全屏渲染路径配置：[config.md](config.md)
