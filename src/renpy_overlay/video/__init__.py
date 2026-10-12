"""视频功能独立包：全屏录制（含系统音频）/ 视频编辑播放器 / 语音识别（ASR）。

需求（.raw_plans/future_视频录制.txt）：为难以暂停截图的场景提供兜底方案，
并额外支持 ASR。设计约束（与 :mod:`renpy_overlay.song_recognition` 同风格）：

- 纯后台能力（录制/截取/ASR 客户端/存储）与窗口（编辑器/历史）分离，
  各文件原子级功能，窗口之间互不依赖；
- PyAV 由项目自带（进程内 FFmpeg 库，无需用户自备 ffmpeg.exe）：缺失时
  录制/语音识别降级提示，不影响主程序与其他功能（:mod:`.av_media`）；
- 录制为双流方案：PyAV gdigrab 纯视频（:mod:`.screen_capture`）+ Python
  环回录音（复用识曲的 soundcard / pyaudiowpatch / pyaudio 三重回退，
  :mod:`.audio_capture`），停止时合并为带 aac 音轨的 mp4（:mod:`.recorder`）；
- ASR 走 OpenAI 兼容 ``chat/completions`` + ``input_audio``（:mod:`.asr`），
  与翻译/识曲/对话共用「同时仅一个 API 请求」互斥（互斥在宿主实现）；
- 实时语音识别（ASR-RT，:mod:`.asr_rt`）：DashScope 实时流式识别（开关式
  后台功能，AsrRtController 自持状态），开启时经宿主互斥槽阻塞其余 API
  请求；识别结果实时上屏、不入 asr.db（需求：防止数据库过度膨胀）；
- 识别成功结果落 ``renpy_overlay_cache/asr.db``（:mod:`.asr_store`）。
"""

from .asr_history_window import AsrHistoryWindow
from .asr_rt import (
    ASR_RT_OFF_HINT,
    ASR_RT_OFF_TEXT,
    ASR_RT_ON_HINT,
    ASR_RT_ON_TEXT,
    AsrRtController,
    OpenAICompatRealtimeASR,
    openai_base_url_to_ws_url,
    resolve_asr_rt_options,
)
from .asr_store import AsrStore
from .asr_store import open_store as open_asr_store
from .audio_capture import RECORD_AVAILABLE
from .av_media import av_available
from .recorder import VideoRecorder

__all__ = [
    "ASR_RT_OFF_HINT",
    "ASR_RT_OFF_TEXT",
    "ASR_RT_ON_HINT",
    "ASR_RT_ON_TEXT",
    "AsrHistoryWindow",
    "AsrRtController",
    "AsrStore",
    "OpenAICompatRealtimeASR",
    "RECORD_AVAILABLE",
    "VideoRecorder",
    "av_available",
    "open_asr_store",
    "openai_base_url_to_ws_url",
    "resolve_asr_rt_options",
]
