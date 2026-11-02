# 语音识别（非实时 / 实时）

对应源码：`src/renpy_overlay/video/asr.py`（非实时客户端）、`asr_rt.py`（实时客户端与开关控制器）、`asr_store.py`（asr.db）、`asr_history_window.py`（历史窗口）、`av_media.py`（音轨截取）、`stream/text_logic.py`（实时上屏纯函数）、`stream/video_flow.py`（宿主接线）。测试：`test_video_asr.py`、`test_video_asr_rt.py`、`test_video_asr_store.py`、`test_video_asr_history.py`、`test_video_av_media.py`、`test_stream_window.py`（text_logic 部分）。

## 1. 非实时链路（asr.py）

OpenAI 兼容 `chat/completions` + `input_audio`（**qwen-omni 等全模态 chat 模型形态**，而非独立 `/audio/transcriptions` 端点）。主要使用场景：视频剪辑「制作字幕」（逐切片转写 + 翻译，见 [video.md](video.md)）。

- **三键回退链** `resolve_asr_options`：`api_base_url_asr`/`model_asr`/`api_key_asr` 空值分别回退主链路 `api_base_url`/`model`/`api_key`（与 vision 同构）；**无备选模型自动发现**（留空即直接用主链路模型）；
- **音频载荷** `audio_content`：wav 字节 → `input_audio` 段，`data` 必须用 **Data URL**（`data:audio/wav;base64,...`）而非纯 base64——DashScope compatible-mode 把 data 当音频 URL 解析，纯 base64 会被判 "The provided URL does not appear to be valid"（HTTP 400 实测）；
- **专用 ASR 模型形态** `is_dedicated_asr_model`：模型名含 "asr"（不区分大小写，如 qwen3-asr-flash / qwen-audio-3.x-asr-flash）时，只发**单条 user 消息且 content 仅含 input_audio 段**——text 指令段与思考方言字段会被其判 "The dedicated task `asr` does not support this input"（HTTP 400 实测）；通用全模态模型则按标准多模态形态附指令文本（`DEFAULT_INSTRUCTION`："请把这段音频中的语音内容逐字转写为文本……"）与 `temperature: 0`；
- 音频来源：`av_media.extract_wav` 截取视频音轨为 **16k 单声道 s16le** wav（约 32KB/s，见 [video.md](video.md) §4）；
- 成功结果写入 asr.db（字幕制作场景**不落库**——落库的是"语音识别历史"功能的独立识别条目）。

## 2. asr.db（asr_store.py）

```sql
CREATE TABLE IF NOT EXISTS asr (
    asr_id INTEGER PRIMARY KEY AUTOINCREMENT,
    audio BLOB NOT NULL,    -- 截取的 wav 音频片段（16k 单声道）
    text TEXT NOT NULL,     -- 语音识别文本
    duration INTEGER NOT NULL  -- 音频时长（秒）
)
```

- `entries()` 不拉取 audio blob（列表只需时长与文本，大字段按需经 `audio(asr_id)` 单条读取，避免全表搬运）；
- 降级 / 主线程约束同全局口径；**实时识别结果从不调用本 store**（落库规避在宿主侧实现，见 §4）。

## 3. 语音识别历史窗口（asr_history_window.py）

自上而下：

1. **查找行**：输入框与上一个/下一个/删除按钮；部分文字匹配、不区分大小写（时长与语音文本两列参与）并整行高亮，回车即向下查找；
2. **音量控制条**：拖拽/滚轮调节，所有播放条目共用同一音量，每次打开重置为**静音 0**；
3. **三列表格**：第一列播放/停止按钮（**同时只播一条**，切换先停前一条）；第二列音频时长（播放中每秒倒计时剩余时长，停止或播完恢复）；第三列语音文本。

支持逐条删除（同步删库、原位回选）、选中条目 Ctrl+C 直接复制语音文本。

## 4. 实时链路（asr_rt.py）

移植自参考项目 `.reference_projects/asr_rt`，适配本项目的注入回调与互斥模式。数据通路：

```
扬声器播放的声音 → soundcard WASAPI loopback 采集（100ms 分片）
                → 混缩单声道 + 重采样 16kHz PCM16（整数倍降采样 box filter，其余线性插值）
                → DashScope Recognition（经 OpenAI Realtime 事件语义转换层 OpenAICompatRealtimeASR）
                → 事件仅经 submit 回调入宿主队列，Qt 主线程消费上屏
```

- **采集常量**：`CHUNK_MS = 100`（阻塞式 `record` 天然形成实时节奏）、`CAPTURE_SR = 48000`（按设备原生采样率采集避免内部重采样丢帧）、`TARGET_SR = 16000`、采集线程 join 超时 2s；
- **协议转换层** `OpenAICompatRealtimeASR`：请求映射 `connect → Recognition.start`、`append_input_audio → send_audio_frame`、`close → Recognition.stop`、`session_update` 声明 pcm16/16kHz 并构造识别实例；响应映射：中间增量（从句首累积的全量文本）→ `response.audio_transcript.delta`、句结束 → `conversation.item.input_audio_transcription.completed`、错误 → `error`。`dashscope.base_websocket_api_url` 由 `openai_base_url_to_ws_url` 转换（`https://{host}/compatible-mode/v1 → wss://{host}/api-ws/v1/inference`；已是 ws/wss 形态则原样用）；dashscope 延迟导入，依赖缺失时构造即抛 RuntimeError；
- **开关控制器** `AsrRtController`：构造注入 `submit`（事件入宿主队列）/ `notify`（标题窗提示）/ `is_busy`+`acquire`+`release`（互斥槽）/ `options`（三键提供者）/ `on_state`（宿主重置显示状态与锚点）。`toggle`/`shutdown` 仅 Qt 主线程调用；
- **开启校验链**（任一失败回退为关并提示原因，**不占用互斥槽**）：已有请求在途 → 缺 dashscope 依赖 → 三键任一为空（`api_base_url_asr_rt`/`model_asr_rt`/`api_key_asr_rt` **独立不回退其它链路，留空即不可用**）→ 无可用环回设备 → 连接失败；
- **关闭**：立即停止（不等尾部 flush）并保留已上屏文本，释放互斥槽，恢复允许新的请求；
- **不落库**：识别结果不入 asr.db（防止数据库过度膨胀）；
- **上屏逻辑**（`stream/text_logic.py` 纯函数 + 宿主消费）：
  - `compose_live_text`：内容随识别结果**增加、修正**（整段连续文字，不将每次修正结果直接追加）；
  - 返回文本**自带的换行先删除**（`strip_asr_line_breaks`，防止双换行），结尾为中英文句号/问号/感叹号等句末符号（`SENTENCE_END_PUNCTUATION`）时主动换行（`has_sentence_end`）；
  - 显示锚点由固定首行改为**跟随内容末尾（倒数第 2 个字符）**：滚轮/拖拽回看暂停跟随、滚回底部恢复（样式 2 同样跟随末尾）；
- **互斥语义**：开启期间**阻塞其余 API 请求**（翻译/截图翻译/非实时语音识别/听歌识曲/AI 对话/预构建，在途时点击被忽略），标题提示「实时语音识别中，阻塞其余API请求」；关闭提示「实时语音识别关闭，允许新的API请求」；
- AI 对话窗的 OCR /「捕获」可把流式窗当前内容填入输入框（识别结束后配合 AI 翻译外文）。

## 5. 配置要点

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `api_base_url_asr` / `model_asr` / `api_key_asr` | `""` | 非实时三键；空值回退主链路（模型空 = 跟随主链路 model，需全模态 audio 能力如 qwen-omni 系列） |
| `api_base_url_asr_rt` / `model_asr_rt` / `api_key_asr_rt` | `""` | 实时三键；**不回退其它链路，任一为空即实时识别不可用**（模型需支持 DashScope 实时流式识别，如 qwen3-asr-flash 系列） |

## 交叉链接

- 音轨截取（16k wav）：[video.md](video.md)
- asr.db 汇总：[history-and-storage.md](history-and-storage.md)
- 互斥槽注入：[architecture.md](architecture.md) §4
