"""视频功能独立包：全屏录制（含系统音频）/ 视频编辑播放器 / 语音识别（ASR）。

需求（.raw_plans/future_视频录制.txt）：为难以暂停截图的场景提供兜底方案，
并额外支持 ASR。设计约束（与 :mod:`renpy_overlay.song_recognition` 同风格）：

- 纯后台能力（录制/截取/ASR 客户端/存储）与窗口（编辑器/历史）分离，
  各文件原子级功能，窗口之间互不依赖；
- ffmpeg.exe 由用户自备：探测顺序 = 程序目录 → PATH，缺失时录制/语音
  识别降级提示，不影响主程序与其他功能（:mod:`.ffmpeg`）；
- 录制为双流方案：ffmpeg gdigrab 纯视频 + Python 环回录音（复用识曲的
  soundcard / pyaudiowpatch / pyaudio 三重回退，:mod:`.audio_capture`），
  停止时合并为带 aac 音轨的 mp4（:mod:`.recorder`）；
- ASR 走 OpenAI 兼容 ``chat/completions`` + ``input_audio``（:mod:`.asr`），
  与翻译/识曲/对话共用「同时仅一个 API 请求」互斥（互斥在宿主实现）；
- 识别成功结果落 ``renpy_overlay_cache/asr.db``（:mod:`.asr_store`）。
"""

from .asr_history_window import AsrHistoryWindow
from .asr_store import AsrStore
from .asr_store import open_store as open_asr_store
from .audio_capture import RECORD_AVAILABLE
from .ffmpeg import find_ffmpeg
from .recorder import VideoRecorder

__all__ = [
    "AsrHistoryWindow",
    "AsrStore",
    "RECORD_AVAILABLE",
    "VideoRecorder",
    "find_ffmpeg",
    "open_asr_store",
]
