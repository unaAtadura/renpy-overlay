"""视频录制 / 视频剪辑 / 实时语音识别的门面 Mixin。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``_video_recorder`` / ``_video_clip_window`` / ``_game_dir`` /
``_config`` / ``_asr_rt`` / ``_asr_rt_finals`` / ``_asr_rt_current`` /
``_body_window``，以及 ``_update_title`` / ``hint`` /
``_acquire_chat_slot`` / ``_release_chat_slot``。
"""

from __future__ import annotations

import logging

from .text_logic import compose_live_text

logger = logging.getLogger("renpy_overlay.stream_window")


class VideoFlowMixin:
    """视频录制开关、剪辑窗口与实时 ASR 差分上屏。"""

    def _open_video_clip(self) -> None:
        """打开视频剪辑窗口（关闭即销毁的单例；PyAV/SDL2 缺失时降级提示）。

        集成约定：配置注入宿主 Config（config.json 单一来源）；制作字幕
        的 ASR/翻译请求经 api_acquire/api_release 占用「同时仅一个 API
        请求」互斥槽；资源面板默认目录与录制视频输出目录同源。
        """
        if self._closed:
            return
        if self._video_clip_window is None:
            try:
                from ..video.clip.editor_window import EditorWindow
                from ..video.recorder import default_video_dir
            except ImportError as exc:
                logger.warning("PyAV/SDL2 不可用，视频剪辑无法打开：%s", exc)
                self.hint("视频剪辑不可用：缺少 PyAV/SDL2 组件")
                return
            window = EditorWindow(
                config=self._config,
                api_acquire=self._acquire_chat_slot,
                api_release=self._release_chat_slot,
                video_dir=default_video_dir(self._game_dir or None),
            )
            # 关闭即销毁（WA_DeleteOnClose）：渲染线程已停的旧实例不可
            # 复用，destroyed 后清空单例引用，下次打开重建全新实例
            window.destroyed.connect(self._on_video_clip_destroyed)
            self._video_clip_window = window
        self._video_clip_window.show()
        self._video_clip_window.raise_()
        self._video_clip_window.activateWindow()
        logger.info("已打开视频剪辑窗口")

    def _on_video_clip_destroyed(self, *_args) -> None:
        """剪辑窗口销毁回调（主线程）：清空单例引用（防复用已停引擎实例）。"""
        self._video_clip_window = None

    def _toggle_video_record(self) -> None:
        """录制视频开关（主线程）：状态由 VideoRecorder 自持（开始/停止/提示）。

        录制不经 API、不占翻译/识曲互斥；ffmpeg 缺失等不可录制情形由
        控制器内部降级并提示（标题窗唯一出口）。
        """
        if self._closed:
            return
        self._video_recorder.toggle()

    # -- 实时语音识别（右键菜单开关；占用互斥槽阻塞其余 API 请求；不入 asr.db） --

    def _on_asr_rt_state(self, enabled: bool) -> None:
        """控制器状态回调（主线程）：重置显示状态并切换正文窗锚点。

        开启：清空识别显示状态与正文窗（识别文本从零开始），锚点切为尾部
        跟随（倒数第2字符可见，需求）；关闭：恢复「固定显示第一行」既定
        锚点（P2 决议），已上屏的识别文本保留在正文窗。
        """
        self._asr_rt_finals = []
        self._asr_rt_current = ""
        if enabled:
            self._body_window.clear()
            self._body_window.set_anchor("tail")
        else:
            self._body_window.set_anchor("start")
        logger.debug("实时语音识别状态：%s", "开启" if enabled else "关闭")

    def _handle_asr_rt_delta(self, payload: dict) -> None:
        """实时识别中间结果（主线程）：替换当前句全文并重算上屏。"""
        if self._closed or not self._asr_rt.enabled:
            return
        self._asr_rt_current = str(payload.get("text") or "")
        self._refresh_asr_rt_display()

    def _handle_asr_rt_completed(self, payload: dict) -> None:
        """实时识别句定稿（主线程）：当前句入定稿列表，另起一句。"""
        if self._closed or not self._asr_rt.enabled:
            return
        self._asr_rt_finals.append(str(payload.get("text") or ""))
        self._asr_rt_current = ""
        self._refresh_asr_rt_display()

    def _handle_asr_rt_error(self, payload: dict) -> None:
        """实时识别错误（主线程）：过期错误忽略；在途错误提示并回退为关。"""
        if self._closed or not self._asr_rt.enabled:
            # 关闭开关后服务端的迟到错误 / 停止会话的收尾事件：作废
            logger.debug("实时语音识别错误已过期（开关已关闭），忽略")
            return
        message = str(payload.get("message") or "未知错误")
        logger.warning("实时语音识别错误（code=%s）：%s", payload.get("code"), message)
        self._update_title(f"实时语音识别错误：{message}")
        self._asr_rt.shutdown()  # 回退为关并释放互斥（静默，不覆盖错误提示）

    def _refresh_asr_rt_display(self) -> None:
        """组合实时识别显示全文并差分上屏（主线程；实时结果不入 asr.db）。"""
        text = compose_live_text(self._asr_rt_finals, self._asr_rt_current)
        self._body_window.replace_stream(text)
