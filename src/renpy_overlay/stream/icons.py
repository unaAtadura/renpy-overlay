"""流式悬浮窗的绑定入口图标常量与翻译模式图标纯函数。

图标资产位于 ``renpy_overlay/assets/icons/``（注意本模块在 ``stream/`` 子包内，
路径基准为 ``Path(__file__).parent.parent``，与拆分前 ``stream_window`` 的
``Path(__file__).parent`` 指向同一目录）。
"""

from __future__ import annotations

from pathlib import Path

#: 绑定窗口入口图标（淡蓝色书本，与标题/正文文字同一视觉语言）
BOUND_PREBUILD_ICON = str(
    Path(__file__).parent.parent / "assets" / "icons" / "书籍1_book-one.svg"
)
#: 「定位翻译位置」入口图标（淡蓝色文档搜索，与既有入口同一视觉语言）
BOUND_LOCATE_ICON = str(
    Path(__file__).parent.parent / "assets" / "icons" / "文档搜索_doc-search-two.svg"
)
#: 「单击翻译」模式图标（淡蓝色播放，与既有入口同一视觉语言）
BOUND_MODE_MANUAL_ICON = str(
    Path(__file__).parent.parent / "assets" / "icons" / "播放_play-one.svg"
)
#: 「自动翻译」模式图标（淡蓝色循环重播，与既有入口同一视觉语言）
BOUND_MODE_AUTO_ICON = str(
    Path(__file__).parent.parent / "assets" / "icons" / "重新播放_replay-music.svg"
)


def translation_mode_icon(auto_translate_on: bool) -> str:
    """翻译模式 → 入口图标（纯函数，离线测试用）：与生效模式一一对应。"""
    return BOUND_MODE_AUTO_ICON if auto_translate_on else BOUND_MODE_MANUAL_ICON
