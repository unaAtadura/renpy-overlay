"""听歌识曲的门面 Mixin：录制系统音频 + Shazam 识别（与翻译共用互斥）。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``_recognizing`` / ``_chat_busy`` / ``_translating`` /
``_config`` / ``_translation_seq`` / ``_song_stop`` / ``_record_deadline`` /
``_record_timer`` / ``_queue`` / ``_song_store``，以及 ``_update_title`` /
``_display_text``。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path

from ..song_recognition import (
    RECORDER_AVAILABLE,
    SHAZAMIO_AVAILABLE,
    extract_track_info,
    format_fail_body,
    format_success_body,
    recognize,
    record_system_audio,
)

logger = logging.getLogger("renpy_overlay.stream_window")


class SongFlowMixin:
    """听歌识曲入口、录制倒计时、后台识别 worker 与结果收尾。"""

    def request_song_recognition(self) -> None:
        """听歌识曲入口（主线程）：互斥检查 → 标题倒计时 → 后台录制+识别。

        与单击/自动/截图翻译共用"同时仅一个 API 请求"的互斥原则：存在任意在途
        翻译或识曲时静默忽略本次点击（需求：不做提示更新，防止正文被意外覆盖）。
        依赖缺失时给出降级提示，不占用互斥标志。
        """
        if self._closed:
            return
        if self._translating or self._recognizing or self._chat_busy:
            logger.info("已有请求在途（翻译/识曲/对话），忽略本次听歌识曲")
            return
        missing = []
        if not SHAZAMIO_AVAILABLE:
            missing.append("shazamio（pip install shazamio）")
        if not RECORDER_AVAILABLE:
            missing.append("soundcard / pyaudiowpatch / pyaudio 任一录制库")
        if missing:
            logger.warning("听歌识曲依赖缺失：%s", "、".join(missing))
            self._update_title("识曲不可用：缺少依赖")
            self._display_text("听歌识曲依赖未安装：\n" + "\n".join(missing) + "\n")
            return
        duration = self._config.recording_duration
        self._recognizing = True
        self._translation_seq += 1
        seq = self._translation_seq
        stop = threading.Event()
        self._song_stop = stop
        self._record_deadline = time.monotonic() + duration
        self._record_tick()  # 立即显示第一条倒计时，不等 1s 定时器
        self._record_timer.start()
        logger.info("发起听歌识曲（seq=%d，录制 %d 秒）", seq, duration)
        thread = threading.Thread(
            target=self._song_worker,
            args=(seq, stop, duration),
            name=f"song-recognition-{seq}",
            daemon=True,
        )
        thread.start()

    def _record_tick(self) -> None:
        """录制倒计时（主线程 QTimer）：标题窗每秒刷新剩余秒数。"""
        remaining = int(math.ceil(self._record_deadline - time.monotonic()))
        if remaining <= 0:
            self._record_timer.stop()
            return  # 录制应已结束，等待 worker 投递下一状态
        self._update_title(f"正在录制系统音频 {remaining}s，建议关闭游戏音效提升识别率")

    def _song_worker(self, seq: int, stop: threading.Event, duration: int) -> None:
        """后台线程：录制系统音频 → Shazam 识别 → 结果只经队列回主线程。"""
        try:
            audio = record_system_audio(duration=duration, stop_event=stop)
            if audio is None:
                if not stop.is_set():
                    self._queue.put(("song_fail", {"seq": seq, "stage": "record"}))
                return
            if stop.is_set():
                self._cleanup_temp_audio(audio)
                return
            self._queue.put(("song_recognizing", {"seq": seq}))
            track = recognize(audio)
            self._cleanup_temp_audio(audio)
            if stop.is_set():
                return  # 已作废：识别结果不再上屏
            if track is None:
                self._queue.put(("song_fail", {"seq": seq, "stage": "recognize"}))
                return
            title, artist = extract_track_info(track)
            self._queue.put(("song_done", {"seq": seq, "title": title, "artist": artist}))
        except Exception as exc:  # 兑底：录制/识别之外的异常统一收敛为识曲失败
            logger.exception("听歌识曲过程发生错误")
            if not stop.is_set():
                self._queue.put(
                    ("song_fail", {"seq": seq, "stage": "recognize", "error": str(exc)})
                )

    @staticmethod
    def _cleanup_temp_audio(audio: Path) -> None:
        """删除录制产生的临时 wav（失败仅记日志，不影响识曲结果展示）。"""
        try:
            audio.unlink(missing_ok=True)
        except OSError:
            logger.warning("删除临时音频文件失败：%s", audio)

    def _handle_song_recognizing(self, item: dict) -> None:
        """主线程：录制结束、开始识别（停倒计时，标题切换为识别中）。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("识曲状态已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._record_timer.stop()
        self._update_title("正在通过Shazam听歌识曲...")

    def _handle_song_done(self, item: dict) -> None:
        """主线程：识曲成功收尾：标题提示成功，正文显示歌名与艺术家。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("识曲结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._recognizing = False
        self._record_timer.stop()
        title = str(item.get("title") or "未知")
        artist = str(item.get("artist") or "未知")
        self._display_text(format_success_body(title, artist))
        self._update_title("识曲成功")
        logger.info("识曲成功（seq=%d）：%s - %s", seq, title, artist)
        # 成功结果落库（需求：仅成功写入，失败不入库）；写入异常由 store 内部降级
        if self._song_store is not None:
            self._song_store.insert(artist=artist, song_name=title)

    def _handle_song_fail(self, item: dict) -> None:
        """主线程：识曲失败收尾：录制失败只改标题，识别失败额外显示正文提示。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("识曲失败结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._recognizing = False
        self._record_timer.stop()
        if item.get("stage") == "record":
            logger.warning("系统音频录制失败，无法进行识曲")
            self._update_title("录制失败")
            return
        logger.warning("识曲失败：%s", item.get("error") or "未识别到歌曲")
        self._display_text(format_fail_body())
        self._update_title("识曲失败")

    def _abort_song_recognition(self, reason: str) -> None:
        """作废在途识曲：世代号递增使结果不再上屏，stop 让录制循环尽快退出。"""
        if not self._recognizing:
            return
        self._recognizing = False
        self._translation_seq += 1
        self._record_timer.stop()
        stop = self._song_stop
        if stop is not None:
            stop.set()
        logger.info("已中止听歌识曲（%s）：结果作废，界面保持当前状态", reason)
