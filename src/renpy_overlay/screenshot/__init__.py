"""截图翻译功能包：截图、图像处理、vision 识别翻译、入库与历史浏览。

各窗口相互独立、每个文件一个原子功能（需求备注）：

- :mod:`capture`    —— Pillow 屏幕截图（物理像素 bbox，支持多显示器）
- :mod:`processing` —— 纯函数图像处理（宽高比约束 / 压缩 / 缩略图 / 编码）
- :mod:`vision`     —— OpenAI 兼容 vision 接口（识图翻译 / 只识别原文）
- :mod:`store`      —— screenshot.db 的 SQLite 存取（降级安全）
- :mod:`chat_store` —— chat.db 的 SQLite 存取（AI 对话成对记录，降级安全）
- :mod:`window`     —— 单个截图窗口（8 色边框识别框，PyQt6）
- :mod:`frame_overlay` —— 快捷键模式框选提示覆盖层（整窗穿透线框，PyQt6）
- :mod:`hotkeys`    —— 快捷键模式（全局热键主开关 + 8 窗口键，联动布局锁定）
- :mod:`mouse_lock_window` —— 锁鼠标区域的范围框选窗口（灰边框八向拉伸）
- :mod:`mouse_lock_indicator` —— 锁鼠标区域锁定态提示窗口（等大小穿透）
- :mod:`mouse_lock` —— 锁鼠标区域控制器（ClipCursor 限制 + 逃脱热键）
- :mod:`history_window` —— 截图历史浏览窗口（缩略图条带 + 原图 + 译文）
- :mod:`chat_history_window` —— 对话历史浏览窗口（两列表格：查找/删除/复制）
- :mod:`viewer_window`  —— 原图全尺寸查看弹窗（拖拽移动，单击关闭）
- :mod:`chat_window`    —— AI 对话窗口（复选框 + 锁定窗口下拉 + OCR / 发送）

窗口池与右键菜单管理在包根 :mod:`renpy_overlay.quick_menu`（后续新功能的
菜单入口统一扩展点），不在本包内。
"""

from .capture import capture_region
from .chat_history_window import ChatHistoryWindow
from .chat_store import ChatStore
from .chat_store import open_store as open_chat_store
from .chat_window import AIChatWindow, color_label, color_swatch_pixmap
from .frame_overlay import FrameOverlayLayer
from .history_window import ScreenshotHistoryWindow
from .hotkeys import HotkeyMode
from .mouse_lock import MouseLockController
from .mouse_lock_indicator import MouseLockIndicatorWindow
from .mouse_lock_window import MouseLockRegionWindow
from .processing import (
    constrain_aspect_ratio,
    encode_jpeg_base64,
    encode_jpeg_bytes,
    make_thumbnail,
    scale_to_percent,
)
from .store import ScreenshotStore, open_store
from .viewer_window import ImageViewerWindow
from .vision import looks_untranslated, recognize_image, translate_image_verified
from .window import ScreenshotWindow

__all__ = [
    "AIChatWindow",
    "ChatHistoryWindow",
    "ChatStore",
    "FrameOverlayLayer",
    "ImageViewerWindow",
    "HotkeyMode",
    "MouseLockController",
    "MouseLockIndicatorWindow",
    "MouseLockRegionWindow",
    "ScreenshotHistoryWindow",
    "ScreenshotStore",
    "ScreenshotWindow",
    "capture_region",
    "color_label",
    "color_swatch_pixmap",
    "constrain_aspect_ratio",
    "encode_jpeg_base64",
    "encode_jpeg_bytes",
    "looks_untranslated",
    "make_thumbnail",
    "open_chat_store",
    "open_store",
    "recognize_image",
    "scale_to_percent",
    "translate_image_verified",
]
