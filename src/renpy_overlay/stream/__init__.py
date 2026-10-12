"""流式悬浮窗原子模块包：由 ``renpy_overlay.stream_window`` 门面组装。

本包是 ``stream_window.py`` 原子化拆分的产物，按职责分层：

- 纯逻辑层（无 Qt、可离线测试）：``constants`` / ``visual`` / ``icons`` /
  ``geometry`` / ``scroll_logic`` / ``text_logic``；
- Qt 渲染层：``windows``（透明双窗口 ``TitleWindow`` / ``BodyWindow``）；
- 门面 Mixin 层：``host_core`` / ``follow`` / ``interaction`` /
  ``translate_flow`` / ``screenshot_flow`` / ``song_flow`` / ``video_flow`` /
  ``history_flow`` / ``prebuild_flow``（经 ``self`` 共享宿主状态，彼此零导入）。

对外入口仍是 ``renpy_overlay.stream_window.StreamOverlayWindow``；本包 ``__init__``
不导入任何子模块，避免连锁导入，符号经门面模块 re-export 兼容。
"""
