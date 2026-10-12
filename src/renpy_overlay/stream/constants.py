"""流式悬浮窗的运行节拍与交互阈值常量。

均为模块级求值：``CLICK_DELAY_MS`` 依赖系统双击间隔（导入时求值一次，
与拆分前 ``stream_window`` 模块导入时求值语义等价）。
"""

from __future__ import annotations

from .. import win32api

DRAIN_INTERVAL_MS = 80
FOLLOW_INTERVAL_MS = 200
CLICK_DELAY_MS = win32api.double_click_time_ms() + 60
CLICK_MOVE_TOLERANCE = 4
CLICK_SUPPRESS_SECONDS = 0.5
#: SW_HIDE 返回后等待 DWM 合成移除旧帧的时间（秒）：约 10 个 60Hz 合成周期，
#  保证截图不会抓到刚隐藏的悬浮窗残影（否则与流式窗重叠时会把译文截进图里）
SCREENSHOT_DWM_SETTLE_SECONDS = 0.18
#: 听歌识曲录制倒计时的标题刷新周期（毫秒）
RECORD_TICK_INTERVAL_MS = 1000
