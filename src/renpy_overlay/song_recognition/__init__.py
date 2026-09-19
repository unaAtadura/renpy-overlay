"""听歌识曲独立包：录制系统音频并经 Shazam 识别歌曲。

设计约束（对应需求文档 .raw_plans/听歌识曲.txt）：

- 纯后台行为：无窗口无弹窗，状态经宿主悬浮窗的标题/正文窗呈现；
- 重依赖（shazamio / soundcard / pyaudiowpatch / pyaudio）全部延迟导入，
  缺库只降级本功能（``*_AVAILABLE`` 标志），不影响主程序启动；
- 录制三重回退：soundcard 环回 → pyaudiowpatch WASAPI 环回 → pyaudio 立体声
  混音（移植自参考项目 screen_translaor_4.0 的实现方式）；
- 与翻译请求共用"同时仅一个 API 请求"的互斥原则，互斥逻辑在宿主
  （stream_window.StreamOverlayWindow）内实现，本包只提供录制与识别。
"""

from .history_window import SongHistoryWindow
from .recognizer import (
    SHAZAMIO_AVAILABLE,
    extract_track_info,
    format_fail_body,
    format_success_body,
    recognize,
)
from .recorder import (
    PYAUDIO_AVAILABLE,
    PYAUDIOWPATCH_AVAILABLE,
    SOUNDCARD_AVAILABLE,
    record_system_audio,
)
from .store import SongStore, open_store

#: 是否存在至少一个可用的系统音频录制方案
RECORDER_AVAILABLE = (
    SOUNDCARD_AVAILABLE or PYAUDIOWPATCH_AVAILABLE or PYAUDIO_AVAILABLE
)

__all__ = [
    "PYAUDIO_AVAILABLE",
    "PYAUDIOWPATCH_AVAILABLE",
    "RECORDER_AVAILABLE",
    "SHAZAMIO_AVAILABLE",
    "SongHistoryWindow",
    "SongStore",
    "SOUNDCARD_AVAILABLE",
    "extract_track_info",
    "format_fail_body",
    "format_success_body",
    "open_store",
    "recognize",
    "record_system_audio",
]
