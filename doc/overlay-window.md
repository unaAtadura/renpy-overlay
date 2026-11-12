# 悬浮窗渲染与交互

对应源码：`src/renpy_overlay/stream_window.py`（门面 + 兼容 re-export）与 `src/renpy_overlay/stream/` 子包；右键菜单 `quick_menu.py`；标题入口条 `bound_window.py`。测试：`tests/test_stream_window.py`、`test_stream_window_style2.py`、`test_quick_menu_control.py`、`test_quick_menu_video.py`。

## 1. 模块拆分（stream/ 子包）

`stream_window.py` 只做 Mixin 组装（`StreamOverlayWindow` 无自有状态与方法），实现按原子文件拆分：

| 文件 | 职责 |
| --- | --- |
| `constants.py` | 运行节拍与交互阈值（下表） |
| `visual.py` | 视觉参数常量（下表） |
| `icons.py` | 入口条自绘图标（预构建 / 定位 / 模式切换） |
| `geometry.py` | 停靠几何纯函数（`compute_geometry` / `pair_layout` / `pair_offset_from_body`） |
| `scroll_logic.py` | 滚动目标计算与正文蒙版条带纯函数 |
| `text_logic.py` | 实时文本纯函数（实时 ASR 拼接 / 句末换行 / 锚点，见 [asr.md](asr.md)） |
| `windows.py` | Qt 渲染层：`_StreamWindowBase` / `TitleWindow` / `BodyWindow`（样式 1） |
| `windows_style2.py` | 样式 2（半透明黑色窗） |
| `choice_log.py` | 选项副作用日志纯逻辑（条目模型 / 去重 / 状态翻转 / 摘要 / 定位钳制，无 Qt 可离线单测） |
| `choice_log_window.py` | 副作用日志弹窗（半透明黑固定尺寸覆盖窗，纯视图） |
| `host_core.py` | `StreamOverlayCore`：装配 / 生命周期 / 队列 / 显示出口 / 配置热重载 |
| `follow.py` | 游戏窗口跟随定位 |
| `interaction.py` | 鼠标交互（拖动 / 双击锁定 / 单击翻译 / 拖拽滚动） |
| `translate_flow.py` | 流式 + 自动翻译（见 [translation.md](translation.md)） |
| `screenshot_flow.py` | 截图翻译 + 遥控自动化（见 [screenshot.md](screenshot.md)） |
| `song_flow.py` | 听歌识曲（见 [song-recognition.md](song-recognition.md)） |
| `video_flow.py` | 视频录制 / 剪辑入口 + 实时 ASR（见 [video.md](video.md)、[asr.md](asr.md)） |
| `history_flow.py` | 历史窗口 + AI 对话入口（见 [history-and-storage.md](history-and-storage.md)、[ai-chat.md](ai-chat.md)） |
| `injection_flow.py` | 注入入口（入口条）+ 翻译模式切换 |

所有 Mixin 经 `self` 共享宿主状态（行为与拆分前单文件实现等价）。

## 2. 关键常量

`stream/constants.py`：

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `DRAIN_INTERVAL_MS` | 80 | 主线程队列消费节拍 |
| `FOLLOW_INTERVAL_MS` | 200 | 游戏窗口跟随轮询 |
| `CLICK_DELAY_MS` | 系统双击间隔 + 60 | 单击/双击延迟判定窗口 |
| `CLICK_MOVE_TOLERANCE` | 4 px | 判定单击的位移容差 |
| `CLICK_SUPPRESS_SECONDS` | 0.5 | 交互抑制窗口 |
| `SCREENSHOT_DWM_SETTLE_SECONDS` | 0.18 | SW_HIDE 后等 DWM 合成移除旧帧（约 10 个 60Hz 周期），截图防残影 |
| `RECORD_TICK_INTERVAL_MS` | 1000 | 识曲录制倒计时刷新 |

`stream/visual.py`：

| 常量 | 值 | 说明 |
| --- | --- | --- |
| `BODY_MARGIN` | 24.0 | 正文文本边距（逻辑像素） |
| `SPAWN_DURATION` | 0 | 已废弃置 0：逐字出现动画已移除，字符直接在最终位置绘制（保留符号维持兼容） |
| `TYPE_INTERVAL_MS` | 16 | 打字机节拍（~60 字/秒，积压越多消化越快） |
| `RENDER_INTERVAL_MS` | 16 | 重绘刷新周期（驱动平滑滚动与滚动插值） |
| `BODY_FILL_COLOR` | `#ADD8E6` | 标题与正文统一淡蓝色（`config.body_fill_color` 的默认值来源，运行时以配置为准） |
| `MASK_COLOR` / `MASK_ALPHA` | `#000000` / 110 | 行蒙版（约 43% 不透明度，仅覆盖有文字的行，行距留空隙） |
| `MASK_PAD_X/Y` / `MASK_RADIUS` | 6.0 / 1.0 / 4.0 | 条带内边距与圆角 |
| `TITLE_MARGIN_X` / `TITLE_GLOW_FACTOR` | 16.0 / 1.7 | 标题左起点 / 标题窗高度余量字号倍数 |

## 3. 双窗口与定位（geometry.py / follow.py）

- 两窗均为独立顶层 QWidget，统一 flags：`WA_TranslucentBackground + FramelessWindowHint + WindowStaysOnTopHint + Tool`——屏幕上只可见文字本身；
- `compute_geometry(game_rect, size, dock, user_offset, margin=12)`：宽度钳制 `max(240, min(配置宽, 游戏宽 - 24))`，高度 `max(80, min(配置高, 游戏高 // 2))`；`user_offset` 存在时位置 = 游戏窗口左上角 + 偏移（拖动后锁定相对位置，不再被停靠拉回；控制台 `d` 恢复）；
- `pair_layout(total, title_h, gap)`：标题窗在上、正文窗在下，水平左对齐，中间空 `gap` 像素（配置 `stream_window_title_gap`）；
- `pair_offset_from_body`：拖动结束时把正文窗坐标换算回"整体几何顶点"参照系的偏移——若直接以正文窗为参照，松手后首次重定位会整体下移「标题高 + 间距」（一次向下跳变）；
- **定位全部走 win32 物理像素**（`move_window`）：Qt 侧不做 Qt 坐标移动，拖动时把 Qt 逻辑坐标乘 `devicePixelRatioF` 换算回物理像素。跟随循环每 200ms 读游戏窗口 `GetWindowRect` 后成对重定位；游戏最小化自动隐藏；移动 / 显隐 / 跟随 / 锁定态置顶均两窗成对处理；锁定态周期性重申 TOPMOST（应对独占全屏被激活时的覆盖）。

## 4. 正文窗渲染（windows.py，样式 1）

- 文字以常规 QFont（Black 字重）、统一淡蓝色直接绘制；背后逐行绘制半透明黑色蒙版条带（`scroll_logic.body_mask_bands` 聚合，行距区域留空隙）提升对比度；
- **打字机**：`_Glyph` 序列按 `TYPE_INTERVAL_MS` 节拍逐字上屏，字符在轮到显示的那一帧直接以最终位置与颜色绘制（无位移/渐显过渡，出现动画已移除）；输出过程**始终固定显示第一行**（大段文字不因自动滚屏滚出可视区，1-2 行短文本观感不变）；
- **滚轮回看**：`body_wheel_target` 计算滚动目标，可回看当前对话全文；滚回顶部恢复固定首行。窗口只显示最新一条对话（单条显示策略，不累积历史）；
- **拖拽滚动**（锁定态）：按住正文文字上下滑动即滚动文本，`scroll_logic.body_drag_scroll_rate`：偏离按下点 12px 内防抖不滚，慢速每秒 1 格滚轮，超过 120px 提速 5 倍；未锁定时按住拖动为移动窗口。

## 5. 标题窗与状态文案

- 标题窗不做打字机动画——每次状态变化一次性整显完整标题；位于正文窗上方、水平左对齐；
- 文案随交互状态动态更新（`_update_title`）：新对话原文前 20 字提醒、「翻译中... / 翻译完成 / 翻译失败」、锁定状态提示、识曲录制倒计时 / 识曲中 / 成功 / 失败、对话发送中 / OCR 状态、实时 ASR「实时语音识别中，阻塞其余API请求」、控制台的连接状态（已连接 / 连接断开 / Hook N 层）也写入同一位置；
- 正文窗尺寸与字号由 `config.json` 的 `stream_window_*` 控制（默认 1760×200，字号 16 / 行距 1.15 / 标题字号 12 / 间距 2），见 [config.md](config.md)。

## 6. 样式 2（windows_style2.py）

`translate_window_style: 2` 切换：整窗半透明黑圆角背景 + 白色文字，无发音按钮 / 蓝色外边框，不支持拖拽改变大小；**无打字机特效**，译文整段汇总后一次性上屏并固定显示在第一行（实时语音识别仍跟随末尾）；鼠标交互与样式 1 完全一致。非法值回退 1。样式 2 下翻译完成走"整段替换"路径（`translate_flow._handle_translation` 中 `style == 2` 分支）。

## 7. 鼠标交互（interaction.py）

- **单击/双击延迟判定**：按下后启动 `CLICK_DELAY_MS` 定时器，位移 ≤ `CLICK_MOVE_TOLERANCE`（4px）时松手才判单击；`CLICK_DELAY_MS` 内二次按下判双击；
- **拖动**：按住标题或正文任一窗口拖动，两窗整体联动；松开后位置以相对游戏窗口的偏移锁定；
- **八向拉伸（仅未锁定）**：按下落在任一窗的边缘/角热区（12px，角优先；标题窗顶缘/上角管北向与上角，正文窗底缘/下角管南向与下角，东西两边两窗皆可拖，总宽同步）即进入拉伸手势——拖动某边只改该方向尺寸、对边固定，拖动角同时改两向、对角固定，涉及左/上时两窗原点同步平移（最小 240×总高下限钳制）。拉伸期间跟随循环经 `_dragging` 短路；结束把新尺寸写回宿主运行时尺寸（跟随以此重摆，被游戏窗口约束的钳制仍生效；不回写 config，重启恢复配置默认）并按拖动同款语义记录相对偏移。悬停热区时对应拉伸光标 + 该边缘亮白反馈线（锁定态无提示、不响应拉伸）；
- **双击锁定**：切换位置锁定——锁定后不接受拖动、自动跟随让位、周期重申 TOPMOST；双击同时强制中止在途翻译（识曲录制也随双击打断）；
- **单击翻译（仅锁定态）**：把最近捕获的游戏原文发给 OpenAI 兼容服务（见 [translation.md](translation.md)）；未锁定时单击不触发。

## 8. 标题入口条（bound_window.py：TitleEntryStrip）

注入成功（`t=hooks` 确认 → `mark_injected()`）后，标题窗标题文本右侧出现自绘图标入口条（**跳过注入模式不出现**；可脱离注入运行的功能仍在右键快捷菜单）。入口随标题文本长度实时移动、同一渲染帧内同步绘制（避免跨窗口定位的滞后/偏移）：

| 入口 | 图标 | 行为 |
| --- | --- | --- |
| 预构建翻译缓存 | `BOUND_PREBUILD_ICON` | 打开三页签预构建主窗口（见 [pretranslate.md](pretranslate.md)） |
| 定位翻译位置 | `BOUND_LOCATE_ICON` | 打开/刷新翻译历史窗口并定位最近一次缓存命中记录，随后清空内存缓存（见 [translation.md](translation.md)） |
| 切换翻译模式（末端） | `translation_mode_icon()`：播放=单击 / 循环重播=自动 | 在两模式间切换并立即同步图标；只改运行时内存状态，不写回 `config.json`，重启后仍以配置文件原值（`auto_translate`）为准 |
| 选项副作用日志（末端） | `CHOICE_LOG_ICON` | 开合副作用日志弹窗（见下文）；label 随开关状态同步（「已开启」） |

入口条动作经 `dispatch_action(BoundAction)` 分发，`BOUND_GAP` 控制图标间距。

### 8.1 选项副作用日志弹窗（choice_log.py / choice_log_window.py）

标题窗图标在「打开 / 关闭」两态间切换（`injection_flow._toggle_choice_log`），**仅人工触发**，默认不弹出、不随选项自动展开；关闭即清空已累积日志，下次打开从空白开始（托盘隐藏/恢复为 hide/show 语义，不清空）。

- **弹窗形态**：独立置顶覆盖窗（Frameless + Tool + 半透明黑圆角底板，与入口条同一视觉语言），初始尺寸 `choice_log_window_width/height`（默认 150×300，需重启键；尺寸不持久化，关闭再打开仍按配置默认打开）；`WA_ShowWithoutActivating` 不抢游戏焦点；顶部拖动把手 + 内容区（除滚动条）全域可拖动——`_LogBrowser` 接管鼠标事件转发窗口拖动，文本交互整体禁用（`NoTextInteraction`：无 I 形光标/选中/双击选词/右键编辑菜单），拖动中光标为移动样式；滚轮与滚动条不受拖动影响（滚轮事件未接管、滚动条是兄弟子控件）；
- **八向拉伸**：未锁定态下边缘/角热区（6px 窄带，角优先）拖拽调整窗口大小——与截图窗口同口径（对边/对角固定、最小 100×150 钳制、光标映射 nw/se → SizeFDiag 等）；滚动条区域不参与东向拉伸（保持原生事件），把手内最靠上的窄带优先作北向拉伸、其余把手区域仍为移动窗口；拉伸仅在未锁定时可用，锁定态不响应尺寸调整；
- **锚定**：打开时经 `TitleWindow.entry_top_right_global("choice-log")` 取图标右上角全局坐标，弹窗左下角贴合并钳在屏内（`anchored_position` 纯函数）；仅打开时计算一次，打开期间不自动跟随标题窗移动，拖动位置保持到关闭；
- **内容与双态区分**：数据来自 `t=choice` 的 `effects` 字段（agent 端 AST 采集，见 [dialogue-capture.md](dialogue-capture.md) §4）与 `choice_pick` 的状态翻转，经 `ChoiceEffectLog` 独立累积（同指纹 0.5s 内合并去重；effects 长度与选项数不齐时整体降级为仅选项文本，绝不错位展示）。每菜单一块：时间 + 上文预览 + 各选项；语句行条件单栏——解析成功（`+=`/`-=`/`= True|False` 三类）仅显示极简摘要，未命中（函数调用/条件表达式等动态行）显示完整原文并标注「动态」语义；「预测」（灰）与「已发生」（淡蓝）用前缀徽标 + 文字色双重标识；
- **与翻译/去重解耦**：该数据是纯旁路——不进 `choice_translation_text` 翻译键、不写 `_last_say` / `_last_translation_input`、不参与 say 去重，正文窗展示完全不受影响（钉子测试 `test_choice_log.py::test_effects_never_enter_choice_translation_key`）。

## 9. 右键快捷菜单（quick_menu.py）

标题/正文窗文字处右键弹出（`QuickMenu` 管理器，截图窗口池 + 各功能菜单扩展点）。菜单项集合：创建截图窗口（`n/8`）/ 销毁截图窗口、快捷键模式开关、锁鼠标区域开关、录制视频（录制中显示「录制视频 mm:ss」）、视频剪辑、实时语音识别开关、遥控器自动化开关与隐藏遥控器（快捷键模式未开启时点击提示先开启）、听歌识曲、对话、预构建翻译缓存、查看历史（翻译 / 截图 / 识曲 / 对话 / 语音识别）、设置（位于退出正上方）、退出。开关型菜单项文案随状态切换（如「实时语音识别: 关/开」）。

## 10. 线程模型

Qt 只能在创建它的线程里操作：**所有跨线程输入**（IPC 回调、控制台命令、翻译 chunk / 结果、识曲结果、ASR 事件）都进宿主队列 `_queue`，由主线程 QTimer（80ms）统一消费。流式翻译在守护线程执行、逐块投递；**世代号（seq）校验**使作废结果不再上屏；stop Event 让中止的请求在 SSE 行之间尽快退出。

## 交叉链接

- 翻译链路：[translation.md](translation.md)
- 截图翻译 / 快捷键模式 / 锁鼠标 / 遥控截图：[screenshot.md](screenshot.md)
- 实时语音识别上屏逻辑：[asr.md](asr.md)
