"""流式输出悬浮窗：全透明双窗口（标题 + 正文），常规字体流式呈现 API 译文。

唯一的悬浮窗实现（PyQt6 为硬依赖，缺失时启动即报清晰 ImportError）。关键设计：

1. **双窗口**。标题窗与正文窗是两个独立的顶层 QWidget，均使用
   ``WA_TranslucentBackground + FramelessWindowHint + WindowStaysOnTopHint
   + Tool`` 实现屏幕上只可见文字本身；文字以常规 QFont（Black 字重）+ 统一
   淡蓝色直接绘制，背后逐行绘制半透明黑色蒙版条带提升对比度。标题窗在正文窗
   上方、水平左对齐，一次性整显完整标题（无打字机动画）；正文窗以打字机节拍
   逐字弹出（积压越多消化越快），带出现动画，输出过程始终固定显示第一行
   （大段文字不因自动滚屏滚出可视区），滚轮可回看当前对话全文、滚回顶部
   恢复固定显示。

2. **定位全部走 win32 物理像素**。跟随游戏窗口复用 :func:`compute_geometry`
   （整体高度 = 标题高 + 间距 + 正文高），经 :func:`pair_layout` 拆成两窗后用
   ``move_window`` 成对定位；锁定态周期性对两窗重申 TOPMOST。Qt 侧不做 Qt 坐标
   移动，拖动时把 Qt 鼠标逻辑坐标乘 ``devicePixelRatioF`` 换算回物理像素。

3. **交互**。左键按住标题或正文任一窗口拖动即整体联动；双击切换位置锁定
   （中止在途翻译）；锁定态按住正文文字上下滑动即拖拽滚动文本（两档速度，
   防抖区内不滚动）；锁定态单击把最近捕获的对话原文经 SSE 流式接口发给
   OpenAI 兼容服务，译文逐块流入正文窗；``auto_translate`` 开启时锁定状态
   按间隔自动翻译（两级缓存命中直接上屏）。单击/双击用"延迟判定"区分。

4. **线程模型**。Qt 只能在创建它的线程里操作，所有跨线程输入（IPC 回调、
   控制台命令、翻译 chunk / 结果）都进队列，由主线程 QTimer（80ms）统一消费；
   流式翻译在守护线程中执行，逐块投递，世代号（seq）校验使作废结果不再上屏，
   stop Event 让中止的请求在 SSE 行之间尽快退出。

模块地图（原子化拆分，实现位于 ``stream/`` 子包，本模块为组装门面 + 兼容
re-export：所有历史公开符号保持从本模块可导入）：

- 纯逻辑层：``stream.constants``（运行节拍）/ ``stream.visual``（视觉参数）/
  ``stream.icons``（入口图标）/ ``stream.geometry``（停靠几何）/
  ``stream.scroll_logic``（滚动与蒙版）/ ``stream.text_logic``（实时文本）；
- Qt 渲染层：``stream.windows``（_StreamWindowBase/TitleWindow/BodyWindow）；
- 门面 Mixin 层：``stream.host_core``（装配/生命周期/队列/显示出口）/
  ``stream.follow``（跟随定位）/ ``stream.interaction``（鼠标交互）/
  ``stream.translate_flow``（流式+自动翻译）/ ``stream.screenshot_flow``
  （截图翻译+遥控自动化）/ ``stream.song_flow``（听歌识曲）/
  ``stream.video_flow``（视频线+实时ASR）/ ``stream.history_flow``
  （历史窗口+AI对话）/ ``stream.prebuild_flow``（预构建+注入入口）。
"""

from __future__ import annotations

import logging
import math  # noqa: F401
import queue  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
from pathlib import Path  # noqa: F401

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer  # noqa: F401
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QPainter  # noqa: F401
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: F401

from . import config, translation_store, translator, win32api  # noqa: F401

# 渲染层搬移后的属性面兼容 re-export
from .bound_window import (  # noqa: F401
    BOUND_GAP,
    BoundAction,
    TitleEntryStrip,
    dispatch_action,
)
from .pretranslate import PrebuildTask, scan_rpy_files  # noqa: F401
from .pretranslate_dialog import PretranslateDialog  # noqa: F401
from .quick_menu import QuickMenu  # noqa: F401
from .screenshot import (  # noqa: F401
    AIChatWindow,
    ChatHistoryWindow,
    HotkeyMode,
    MouseLockController,
    RemoteAutomationController,
    RemoteScreenshotController,
    ScreenshotHistoryWindow,
    capture_region,
    constrain_aspect_ratio,
    encode_jpeg_base64,
    encode_jpeg_bytes,
    make_thumbnail,
    open_chat_store,
    open_store,
    scale_to_percent,
)
from .screenshot.vision import translate_image_verified  # noqa: F401
from .song_recognition import (  # noqa: F401
    RECORDER_AVAILABLE,
    SHAZAMIO_AVAILABLE,
    SongHistoryWindow,
    extract_track_info,
    format_fail_body,
    format_success_body,
    recognize,
    record_system_audio,
)
from .song_recognition.store import open_store as open_song_store  # noqa: F401
from .stream.constants import (  # noqa: F401
    CLICK_DELAY_MS,
    CLICK_MOVE_TOLERANCE,
    CLICK_SUPPRESS_SECONDS,
    DRAIN_INTERVAL_MS,
    FOLLOW_INTERVAL_MS,
    RECORD_TICK_INTERVAL_MS,
    SCREENSHOT_DWM_SETTLE_SECONDS,
)
from .stream.follow import WindowFollowMixin
from .stream.geometry import (  # noqa: F401
    DOCK_CHOICES,
    MARGIN,
    compute_geometry,
    pair_layout,
    pair_offset_from_body,
)
from .stream.history_flow import HistoryFlowMixin
from .stream.host_core import StreamOverlayCore
from .stream.icons import (  # noqa: F401
    BOUND_LOCATE_ICON,
    BOUND_MODE_AUTO_ICON,
    BOUND_MODE_MANUAL_ICON,
    BOUND_PREBUILD_ICON,
    translation_mode_icon,
)
from .stream.interaction import MouseInteractionMixin
from .stream.prebuild_flow import PrebuildFlowMixin
from .stream.screenshot_flow import ScreenshotFlowMixin
from .stream.scroll_logic import (  # noqa: F401
    DRAG_SCROLL_DEBOUNCE_PX,
    DRAG_SCROLL_FAST_FACTOR,
    DRAG_SCROLL_FAST_PX,
    DRAG_SCROLL_INTERVAL_MS,
    DRAG_SCROLL_SLOW_NOTCHES_PER_SEC,
    WHEEL_LINES_PER_NOTCH,
    body_drag_scroll_rate,
    body_mask_bands,
    body_scroll_target,
    body_wheel_target,
)
from .stream.song_flow import SongFlowMixin
from .stream.text_logic import (  # noqa: F401
    SENTENCE_END_PUNCTUATION,
    compose_live_text,
    has_sentence_end,
    plan_stream_replace,
    strip_asr_line_breaks,
)
from .stream.translate_flow import StreamTranslateMixin
from .stream.video_flow import VideoFlowMixin
from .stream.visual import (  # noqa: F401
    BODY_FILL_COLOR,
    BODY_MARGIN,
    MASK_ALPHA,
    MASK_COLOR,
    MASK_PAD_X,
    MASK_PAD_Y,
    MASK_RADIUS,
    RENDER_INTERVAL_MS,
    SPAWN_DURATION,
    TITLE_GLOW_FACTOR,
    TITLE_MARGIN_X,
    TYPE_INTERVAL_MS,
)
from .stream.windows import (  # noqa: F401
    BodyWindow,
    TitleWindow,
    _Glyph,
    _stream_font,
    _StreamWindowBase,
)
from .translation_cache import (  # noqa: F401 (choice_translation_text re-export)
    TranslationCache,
    choice_translation_text,
    hash_original,
)
from .translation_history_window import TranslationHistoryWindow  # noqa: F401
from .video import (  # noqa: F401
    AsrHistoryWindow,
    AsrRtController,
    VideoRecorder,
    open_asr_store,
    resolve_asr_rt_options,
)
from .video import (
    asr as video_asr,  # noqa: F401
)
from .video.av_media import av_available, extract_wav  # noqa: F401
from .video.editor_window import ASR_FAIL_TEXT, ASR_SUCCESS_TEXT  # noqa: F401

logger = logging.getLogger("renpy_overlay.stream_window")

__all__ = [
    "AIChatWindow",
    "ASR_FAIL_TEXT",
    "ASR_SUCCESS_TEXT",
    "AsrHistoryWindow",
    "AsrRtController",
    "BODY_FILL_COLOR",
    "BODY_MARGIN",
    "BOUND_GAP",
    "BOUND_LOCATE_ICON",
    "BOUND_MODE_AUTO_ICON",
    "BOUND_MODE_MANUAL_ICON",
    "BOUND_PREBUILD_ICON",
    "BodyWindow",
    "BoundAction",
    "CLICK_DELAY_MS",
    "CLICK_MOVE_TOLERANCE",
    "CLICK_SUPPRESS_SECONDS",
    "ChatHistoryWindow",
    "DOCK_CHOICES",
    "DRAG_SCROLL_DEBOUNCE_PX",
    "DRAG_SCROLL_FAST_FACTOR",
    "DRAG_SCROLL_FAST_PX",
    "DRAG_SCROLL_INTERVAL_MS",
    "DRAG_SCROLL_SLOW_NOTCHES_PER_SEC",
    "DRAIN_INTERVAL_MS",
    "FOLLOW_INTERVAL_MS",
    "HotkeyMode",
    "MARGIN",
    "MASK_ALPHA",
    "MASK_COLOR",
    "MASK_PAD_X",
    "MASK_PAD_Y",
    "MASK_RADIUS",
    "MouseLockController",
    "PrebuildTask",
    "PretranslateDialog",
    "QApplication",
    "QColor",
    "QFont",
    "QFontMetricsF",
    "QPainter",
    "QPointF",
    "QRect",
    "QRectF",
    "QTimer",
    "QWidget",
    "Qt",
    "QuickMenu",
    "RECORDER_AVAILABLE",
    "RECORD_TICK_INTERVAL_MS",
    "RENDER_INTERVAL_MS",
    "RemoteAutomationController",
    "RemoteScreenshotController",
    "SCREENSHOT_DWM_SETTLE_SECONDS",
    "SENTENCE_END_PUNCTUATION",
    "SHAZAMIO_AVAILABLE",
    "SPAWN_DURATION",
    "ScreenshotHistoryWindow",
    "SongHistoryWindow",
    "StreamOverlayWindow",
    "TITLE_GLOW_FACTOR",
    "TITLE_MARGIN_X",
    "TYPE_INTERVAL_MS",
    "TitleEntryStrip",
    "TitleWindow",
    "TranslationCache",
    "TranslationHistoryWindow",
    "VideoRecorder",
    "WHEEL_LINES_PER_NOTCH",
    "av_available",
    "body_drag_scroll_rate",
    "body_mask_bands",
    "body_scroll_target",
    "body_wheel_target",
    "capture_region",
    "choice_translation_text",
    "compose_live_text",
    "compute_geometry",
    "config",
    "constrain_aspect_ratio",
    "dispatch_action",
    "encode_jpeg_base64",
    "encode_jpeg_bytes",
    "extract_track_info",
    "extract_wav",
    "format_fail_body",
    "format_success_body",
    "has_sentence_end",
    "hash_original",
    "logger",
    "make_thumbnail",
    "open_asr_store",
    "open_chat_store",
    "open_song_store",
    "open_store",
    "pair_layout",
    "pair_offset_from_body",
    "plan_stream_replace",
    "recognize",
    "record_system_audio",
    "resolve_asr_rt_options",
    "scale_to_percent",
    "scan_rpy_files",
    "strip_asr_line_breaks",
    "translation_mode_icon",
    "translation_store",
    "translator",
    "video_asr",
    "win32api",
]


class StreamOverlayWindow(
    StreamOverlayCore,
    HistoryFlowMixin,
    SongFlowMixin,
    VideoFlowMixin,
    ScreenshotFlowMixin,
    StreamTranslateMixin,
    PrebuildFlowMixin,
    MouseInteractionMixin,
    WindowFollowMixin,
):
    """流式悬浮窗门面：按功能链路组装的 ``StreamOverlayCore`` 终态类。

    仅做 Mixin 组装，无自有状态与方法：``__init__`` 装配与队列/定时器/
    显示出口在 :class:`StreamOverlayCore`，各功能链路在对应 Mixin 中，
    全部经 ``self`` 共享宿主状态（行为与拆分前单文件实现逐字等价）。
    """
