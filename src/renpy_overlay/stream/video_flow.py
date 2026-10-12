"""视频录制 / 视频编辑 / 语音识别 / 实时语音识别的门面 Mixin。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``_translating`` / ``_recognizing`` / ``_chat_busy`` /
``_video_recorder`` / ``_video_editor_window`` / ``_video_av_ready`` /
``_game_dir`` / ``_config`` / ``_translation_seq`` / ``_queue`` /
``_asr_store`` / ``_asr_history_window`` / ``_asr_rt`` / ``_asr_rt_finals`` /
``_asr_rt_current`` / ``_body_window`` / ``_quick_menu``，以及
``_acquire_chat_slot`` / ``_release_chat_slot`` / ``_display_text``。
"""

from __future__ import annotations

import logging
import threading

from ..translator import TranslationError
from ..video import asr as video_asr
from ..video.av_media import av_available, extract_wav
from ..video.editor_window import ASR_FAIL_TEXT, ASR_SUCCESS_TEXT
from .text_logic import compose_live_text

logger = logging.getLogger("renpy_overlay.stream_window")


class VideoFlowMixin:
    """视频录制开关、编辑窗、片段 ASR 与实时 ASR 差分上屏。"""

    def _toggle_video_record(self) -> None:
        """录制视频开关（主线程）：状态由 VideoRecorder 自持（开始/停止/提示）。

        录制不经 API、不占翻译/识曲互斥；ffmpeg 缺失等不可录制情形由
        控制器内部降级并提示（标题窗唯一出口）。
        """
        if self._closed:
            return
        self._video_recorder.toggle()

    def _open_video_editor(self) -> None:
        """打开视频编辑窗口（单例复用；QtMultimedia 缺失时降级提示）。"""
        if self._video_editor_window is None:
            try:
                from ..video.editor_window import VideoEditorWindow
                from ..video.recorder import default_video_dir
            except ImportError as exc:
                logger.warning("Qt Multimedia 不可用，视频编辑无法打开：%s", exc)
                self.hint("视频编辑不可用：缺少 QtMultimedia 组件")
                return
            window = VideoEditorWindow(
                video_dir=default_video_dir(self._game_dir or None),
                on_asr_request=self._request_video_asr,
            )
            # 帧放大窗压低层级的参照：截图窗口池中双击锁定窗口的句柄
            window.set_locked_hwnd_provider(
                lambda: [w.hwnd for w in self._quick_menu.locked_windows()]
            )
            self._video_editor_window = window
        self._video_editor_window.show()
        self._video_editor_window.raise_()
        self._video_editor_window.activateWindow()
        logger.info("已打开视频编辑窗口")

    def _request_video_asr(self, start_s: float, end_s: float) -> None:
        """语音识别入口（主线程）：互斥检查 → 后台线程截取音轨并请求。

        与翻译/识曲/对话共用「同时仅一个 API 请求」的互斥原则（需求）：
        存在任意在途请求时静默忽略本次点击。片段由 PyAV 截取音轨转
        wav（终点早于起点已在编辑窗口整理），识别成功经 asr_done 队列
        事件回主线程落库与上屏。
        """
        if self._closed:
            return
        if self._translating or self._recognizing or self._chat_busy:
            logger.info("已有请求在途（翻译/识曲/对话），忽略本次语音识别")
            return
        video_path = (
            self._video_editor_window.current_video_path()
            if self._video_editor_window is not None
            else None
        )
        if not video_path:
            if self._video_editor_window is not None:
                self._video_editor_window.set_status(ASR_FAIL_TEXT)
            return
        av_ready = av_available() if self._video_av_ready is None else self._video_av_ready
        self._video_av_ready = av_ready
        if not av_ready:
            logger.warning("视频组件不可用，语音识别不可用")
            if self._video_editor_window is not None:
                self._video_editor_window.set_status(ASR_FAIL_TEXT)
            return
        self._acquire_chat_slot()
        self._translation_seq += 1
        seq = self._translation_seq
        duration = max(1, int(round(abs(end_s - start_s))))
        thread = threading.Thread(
            target=self._asr_worker,
            args=(seq, video_path, start_s, end_s, duration),
            name=f"video-asr-{seq}",
            daemon=True,
        )
        thread.start()

    def _asr_worker(
        self,
        seq: int,
        video_path: str,
        start_s: float,
        end_s: float,
        duration: int,
    ) -> None:
        """后台线程：截取音频轨转 wav → ASR 请求 → 结果只经队列回主线程。"""
        try:
            wav = extract_wav(video_path=video_path, start_s=start_s, end_s=end_s)
            if wav is None:
                self._queue.put(("asr_fail", {"seq": seq, "stage": "extract"}))
                return
            base_url, model, api_key = video_asr.resolve_asr_options(self._config)
            text = video_asr.recognize_audio(
                wav,
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=self._config.api_timeout,
                enable_thinking=self._config.enable_thinking,
                reasoning_effort=self._config.reasoning_effort,
            )
            self._queue.put(
                ("asr_done", {"seq": seq, "text": text, "audio": wav, "duration": duration})
            )
        except TranslationError as exc:
            logger.warning("语音识别失败：%s", exc)
            self._queue.put(
                ("asr_fail", {"seq": seq, "stage": "recognize", "error": str(exc)})
            )
        except Exception as exc:  # 兑底：截取/请求之外的异常统一收敛为识别失败
            logger.exception("语音识别过程发生错误")
            self._queue.put(
                ("asr_fail", {"seq": seq, "stage": "recognize", "error": str(exc)})
            )

    def _handle_asr_done(self, payload: dict) -> None:
        """识别成功（主线程）：释放互斥 → 落库 → 上屏 → 编辑窗状态与历史刷新。"""
        self._release_chat_slot()
        if self._closed:
            return
        text = str(payload["text"])
        if self._asr_store is not None:
            self._asr_store.insert(payload["audio"], text, int(payload["duration"]))
            if self._asr_history_window is not None:
                self._asr_history_window.refresh()
        if self._video_editor_window is not None:
            self._video_editor_window.set_status(ASR_SUCCESS_TEXT)
        self._display_text(text + "\n")  # 需求：识别结果上屏到流式输出窗口
        logger.info("语音识别成功（%d 字），已入库并上屏", len(text))

    def _handle_asr_fail(self, payload: dict) -> None:
        """识别失败（主线程）：释放互斥 + 编辑窗状态提示。"""
        self._release_chat_slot()
        logger.warning(
            "语音识别失败（stage=%s，error=%s）",
            payload.get("stage"),
            payload.get("error"),
        )
        if self._closed:
            return
        if self._video_editor_window is not None:
            self._video_editor_window.set_status(ASR_FAIL_TEXT)

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
