"""历史窗口与 AI 对话窗口的门面 Mixin：单例窗口打开 + 对话互斥槽。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_screenshot_store`` / ``_history_window`` / ``_song_store`` /
``_song_history_window`` / ``_chat_store`` / ``_chat_history_window`` /
``_translation_store`` / ``_translation_history_window`` / ``_chat_window`` /
``_quick_menu`` / ``_config`` / ``_translation_cache`` /
``_last_cache_hit_original``，以及 ``hint`` / ``_update_title`` /
``_display_text`` / ``_body_window`` / ``_resolve_target``。
"""

from __future__ import annotations

import logging

from .. import win32api
from ..screenshot import (
    AIChatWindow,
    ChatHistoryWindow,
    ScreenshotHistoryWindow,
)
from ..song_recognition import SongHistoryWindow
from ..translation_cache import hash_original
from ..translation_history_window import TranslationHistoryWindow
from ..video import AsrHistoryWindow

logger = logging.getLogger("renpy_overlay.stream_window")


class HistoryFlowMixin:
    """各类历史窗口 / AI 对话窗口的单例管理与对话 API 互斥槽。"""

    # -- 历史窗口（单例复用，每次刷新数据） -----------------------------------

    def _open_history(self) -> None:
        """打开截图历史窗口（单例复用，每次刷新数据）。"""
        if self._screenshot_store is None:
            self.hint("截图历史不可用：未定位到游戏目录或数据库初始化失败。")
            return
        if self._history_window is None:
            self._history_window = ScreenshotHistoryWindow(self._screenshot_store)
        self._history_window.refresh()
        self._history_window.show()
        self._history_window.raise_()
        self._history_window.activateWindow()
        logger.info("已打开截图历史窗口")

    def _open_asr_history(self) -> None:
        """打开语音识别历史窗口（单例复用，每次刷新数据）。"""
        if self._asr_store is None:
            self.hint("语音识别历史不可用：未定位到游戏目录或数据库初始化失败。")
            return
        if self._asr_history_window is None:
            self._asr_history_window = AsrHistoryWindow(self._asr_store)
        self._asr_history_window.refresh()
        self._asr_history_window.show()
        self._asr_history_window.raise_()
        self._asr_history_window.activateWindow()
        logger.info("已打开语音识别历史窗口")

    def _open_song_history(self) -> None:
        """打开识曲历史窗口（单例复用，每次刷新数据）。"""
        if self._song_store is None:
            self.hint("识曲历史不可用：未定位到游戏目录或数据库初始化失败。")
            return
        if self._song_history_window is None:
            self._song_history_window = SongHistoryWindow(self._song_store)
        self._song_history_window.refresh()
        self._song_history_window.show()
        self._song_history_window.raise_()
        self._song_history_window.activateWindow()
        logger.info("已打开识曲历史窗口")

    def _open_chat_history(self) -> None:
        """打开对话历史窗口（单例复用，每次刷新数据）。"""
        if self._chat_store is None:
            self.hint("对话历史不可用：未定位到游戏目录或数据库初始化失败。")
            return
        if self._chat_history_window is None:
            self._chat_history_window = ChatHistoryWindow(self._chat_store)
        self._chat_history_window.refresh()
        self._chat_history_window.show()
        self._chat_history_window.raise_()
        self._chat_history_window.activateWindow()
        logger.info("已打开对话历史窗口")

    def _open_translation_history(
        self,
        locate_original: str | None = None,
        locate_hash: str | None = None,
    ) -> None:
        """打开翻译历史窗口（单例复用，每次刷新数据；可携带定位入参）。"""
        if self._translation_store is None:
            self.hint("翻译历史不可用：未定位到游戏目录或数据库初始化失败。")
            return
        if self._translation_history_window is None:
            self._translation_history_window = TranslationHistoryWindow(
                self._translation_store
            )
        self._translation_history_window.refresh(
            locate_original=locate_original, locate_hash=locate_hash
        )
        self._translation_history_window.show()
        self._translation_history_window.raise_()
        self._translation_history_window.activateWindow()
        logger.info("已打开翻译历史窗口")

    def _locate_last_translation(self) -> None:
        """「定位翻译位置」入口：先定位最近缓存命中条目，再清空内存缓存。

        顺序（需求给定）：先在翻译历史窗口定位「刚刚缓存命中的那条翻译记录」
        （哈希 + 原文作定位入参，直接滚动选中；无命中记录则不定位），随后
        清空内存缓存 —— 人工在历史窗口改译后，下一次自动翻译命中即回源
        数据库取到最新译文，无需重启工具。
        """
        what = self._last_cache_hit_original
        if not what:
            logger.info("定位翻译位置：尚无缓存命中记录，仅打开翻译历史窗口（不定位）")
        self._open_translation_history(
            locate_original=what,
            locate_hash=hash_original(what) if what else None,
        )
        released = self._translation_cache.size()
        entries = self._translation_cache.count()
        self._translation_cache.clear()
        logger.info(
            "已清空内存翻译缓存（释放 %d 字节 / %d 条），下一次命中将回源数据库",
            released,
            entries,
        )

    # -- AI 对话（右键菜单打开窗口；OCR/发送与翻译/识曲共用同一互斥原则） ------

    def _open_chat(self) -> None:
        """打开 AI 对话窗口（单例复用，每次打开重置交互状态并重建下拉列表）。"""
        if self._chat_window is None:
            self._chat_window = AIChatWindow(
                store=self._chat_store,
                controller=self._quick_menu,
                app_config=self._config,
                notify=self._update_title,
                display_reply=self._display_chat_reply,
                api_available=self._chat_api_available,
                api_acquire=self._acquire_chat_slot,
                api_release=self._release_chat_slot,
                capture_locked=self._capture_locked_region,
                game_rect_provider=self._game_window_rect,
                capture_stream_text=lambda: self._body_window.stream_text,
            )
        self._chat_window.refresh()
        self._chat_window.show()
        self._chat_window.raise_()
        self._chat_window.activateWindow()
        logger.info("已打开 AI 对话窗口")

    def _chat_api_available(self) -> bool:
        """互斥出口：翻译/识曲/对话任一在途即不可发起新的 API 请求。"""
        return not (self._translating or self._recognizing or self._chat_busy)

    def _acquire_chat_slot(self) -> None:
        """对话侧占用互斥槽（占用后翻译/识曲的既有入口同样被拦截）。"""
        self._chat_busy = True

    def _release_chat_slot(self) -> None:
        self._chat_busy = False

    def _game_window_rect(self) -> tuple[int, int, int, int] | None:
        """游戏窗口物理像素 rect（ltrb；未定位到游戏窗口时返回 None）。

        供对话窗口按流式窗同一钳制规则计算尺寸（chat_physical_size）。
        """
        try:
            hwnd = self._resolve_target()
        except Exception:  # pragma: no cover - 窗口枚举异常
            return None
        if not hwnd:
            return None
        try:
            return win32api.window_rect(hwnd)
        except Exception:  # pragma: no cover - 窗口销毁竞态
            return None

    def _display_chat_reply(self, text: str) -> None:
        """AI 回复上屏：整段替换正文窗内容（打字机呈现，与识曲结果同路径）。"""
        self._display_text(text + "\n")
