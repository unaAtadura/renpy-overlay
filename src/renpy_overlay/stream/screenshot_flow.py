"""截图翻译与遥控器自动化的门面 Mixin：视觉链路（隐藏-抓屏-恢复时序）。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``_translating`` / ``_recognizing`` / ``_chat_busy`` /
``_translation_seq`` / ``_current_stop`` / ``_config`` / ``_queue`` /
``_screenshot_store`` / ``_screenshot_suppress`` / ``_quick_menu`` /
``_remote_screenshot`` / ``_remote_automation`` / ``_visible`` /
``_body_hwnd`` / ``_title_hwnd`` / ``_body_window``，以及 ``_set_visible`` /
``_update_title`` / ``_display_text``。
"""

from __future__ import annotations

import logging
import threading
import time

from PyQt6.QtCore import QTimer

from .. import win32api
from ..screenshot import (
    capture_region,
    constrain_aspect_ratio,
    encode_jpeg_base64,
    encode_jpeg_bytes,
    make_thumbnail,
    scale_to_percent,
)
from ..screenshot.vision import translate_image_verified
from .constants import SCREENSHOT_DWM_SETTLE_SECONDS

logger = logging.getLogger("renpy_overlay.stream_window")


class ScreenshotFlowMixin:
    """截图翻译、AI 对话 OCR 抓屏共用时序、遥控器自动化提交与收尾。"""

    def _capture_locked_region(self, window, on_done) -> None:
        """隐藏窗口抓取双击锁定截图窗口区域的干净画面（主线程，AI 对话 OCR 用）。

        与截图翻译同一时序（需求「逻辑一致性」）：识别画面不能包含截图窗自身
        的半透明遮罩与彩色边框，也须防流式窗与截图窗重叠时的残影混入 —— 先
        隐藏截图窗与流式窗，等 DWM 移除旧帧后抓屏，再按 hide 前几何恢复显示。
        ``on_done(image | None)`` 在主线程回调；过程不占用翻译互斥（调用方已
        持有对话互斥槽），失败同样回调 None 由调用方收尾。
        """
        if self._closed:
            on_done(None)
            return
        try:
            bbox = win32api.window_rect(window.hwnd)
        except Exception:  # pragma: no cover - 窗口销毁竞态
            logger.warning("无法定位截图窗口区域，取消本次抓屏")
            on_done(None)
            return
        self._screenshot_suppress = True
        self._quick_menu.hide_all()
        self._remote_screenshot.hide_windows()  # A/B 随抓屏一并隐藏（与截图翻译同一时序）
        was_visible = self._visible
        saved_rects = None
        if was_visible:
            try:
                saved_rects = (
                    win32api.window_xywh(self._body_hwnd),
                    win32api.window_xywh(self._title_hwnd),
                )
            except Exception:  # pragma: no cover - 窗口销毁竞态
                saved_rects = None
            self._set_visible(False)
        QTimer.singleShot(
            int(SCREENSHOT_DWM_SETTLE_SECONDS * 1000),
            lambda: self._capture_restore_then(bbox, on_done, saved_rects, was_visible),
        )

    def request_screenshot_translation(self, window) -> None:
        """截图窗口锁定态单击（主线程）：互斥检查 → 隐藏 → 截图 → 恢复 → 后台翻译。

        与单击/自动翻译共用 ``_translating`` 互斥：存在任意在途翻译（含 AI
        对话、识曲）则既不调 API 也不截图（防止译文被意外覆盖）。截图始终
        走 API、不检索缓存，成功不写两级缓存（需求）。
        """
        if self._closed:
            return
        if self._translating or self._recognizing or self._chat_busy:
            logger.info("已有请求在途（翻译/识曲/对话），忽略本次截图翻译")
            return
        try:
            bbox = win32api.window_rect(window.hwnd)
        except Exception:  # pragma: no cover - 窗口销毁竞态
            logger.warning("无法定位截图窗口区域，忽略本次截图翻译")
            return
        self._translating = True
        self._translation_seq += 1
        seq = self._translation_seq
        stop = threading.Event()
        self._current_stop = stop
        self._update_title("截图翻译中，翻译速度会更慢...")
        # 截图时短暂隐藏截图窗口与流式窗口，截图后发送 API 前恢复（需求时序）。
        # 记录 hide 前的物理几何（xywh，与 move_window 参数格式一致）：show() 可能
        # 重应用 Qt 逻辑几何，而锁定态下跟随循环不做 move_window 矫正，漂移会永久
        # 残留（曾因把 GetWindowRect 的 ltrb 直接当 xywh 传入导致标题窗高度暴增、
        # 文字绘制到窗口垂直中央而"跑到正文窗下方/不可见"）
        self._screenshot_suppress = True
        self._quick_menu.hide_all()
        self._remote_screenshot.hide_windows()  # 遥控截图 A/B 随截图一并隐藏（需求）
        was_visible = self._visible
        saved_rects = None
        if was_visible:
            try:
                saved_rects = (
                    win32api.window_xywh(self._body_hwnd),
                    win32api.window_xywh(self._title_hwnd),
                )
            except Exception:  # pragma: no cover - 窗口销毁竞态
                saved_rects = None
            self._set_visible(False)
        # SW_HIDE 返回后 DWM 合成树可能尚未移除窗口（屏幕 DC 仍可抓到旧帧），与
        # 流式窗重叠时截图会混入悬浮窗画面。用单发定时器等合成周期后再抓屏：
        # 等待期间主线程照常泵事件（打字机动画与鼠标不冻结），避免整段 sleep 卡 UI
        QTimer.singleShot(
            int(SCREENSHOT_DWM_SETTLE_SECONDS * 1000),
            lambda: self._capture_after_hide(seq, bbox, stop, saved_rects, was_visible),
        )

    def _capture_after_hide(self, seq: int, bbox, stop, saved_rects, was_visible: bool) -> None:
        """延迟抓屏终点（主线程）：抓屏 -> 恢复窗口显示 -> 起后台翻译线程。"""
        self._capture_restore_then(
            bbox,
            lambda image: self._start_screenshot_worker(seq, image, stop),
            saved_rects,
            was_visible,
        )

    def _start_screenshot_worker(self, seq: int, image, stop: threading.Event) -> None:
        """抓屏回调（主线程）：失败收敛为翻译失败，成功则起后台翻译线程。"""
        if image is None:
            self._translating = False
            self._update_title("翻译失败")
            return
        logger.info("发起截图翻译（seq=%d，区域 %dx%d）", seq, image.width, image.height)
        api_options = {  # 视觉链路基址/模型/密钥独立于文本链路（基址空则跟随主链路）
            "base_url": self._config.api_base_url_vision or self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": self._config.model_vision,
            "api_key": self._config.api_key_vision,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
        }
        thread = threading.Thread(
            target=self._screenshot_worker,
            args=(seq, image, stop, api_options),
            name=f"screenshot-translate-{seq}",
            daemon=True,
        )
        thread.start()

    def _capture_restore_then(self, bbox, on_done, saved_rects, was_visible) -> None:
        """抓屏并恢复窗口显示（主线程）：失败回调 None，成功回调 PIL Image。

        截图翻译与 AI 对话 OCR 共用的抓屏时序终点：finally 中的恢复逻辑
        （截图窗 show_all、流式窗按 hide 前物理几何原样还原）与隐藏侧
        一一对应；窗口已关闭时直接返回、不再回调。回调在主线程同步执行。
        """
        if self._closed:
            return
        image = None
        try:
            image = capture_region(bbox)
        except Exception as exc:
            logger.warning("屏幕截图失败：%s", exc)
        finally:
            self._screenshot_suppress = False
            self._quick_menu.show_all()
            self._remote_screenshot.show_windows()  # A/B 无条件恢复（不受布局锁定分支影响）
            if was_visible:
                self._set_visible(True)
                if saved_rects is not None:
                    # 用 hide 前的物理几何原样恢复（锁定态无人矫正几何，必须显式还原）
                    try:
                        win32api.move_window(self._body_hwnd, *saved_rects[0], topmost=True)
                        win32api.move_window(self._title_hwnd, *saved_rects[1], topmost=True)
                    except Exception:  # pragma: no cover - 窗口销毁竞态
                        pass
        on_done(image)

    def _screenshot_worker(self, seq: int, image, stop: threading.Event, api_options: dict) -> None:
        """后台线程：宽高比约束副本 → 压缩至配置百分比 → vision API → 结果入队。

        原始截图与副本均为线程局部变量：翻译失败随 return 自然清除（需求）；
        成功时在回传前生成 ≤10w 像素缩略图，主线程只负责展示与落库。
        """
        try:
            ratio_image = constrain_aspect_ratio(image)
            api_image = scale_to_percent(ratio_image, self._config.screenshot_compress_percent)
            jpeg_base64 = encode_jpeg_base64(api_image)
            if stop.is_set():
                return  # 已作废：不再发起请求
            # 带结果校验：模型偶发"只识别不翻译"（返回英文原文），
            # CJK 占比过低时自动用强化指令重试一次
            text = translate_image_verified(jpeg_base64, abort_check=stop.is_set, **api_options)
            if stop.is_set():
                return
            thumbnail = make_thumbnail(image)
            self._queue.put(
                (
                    "screenshot_done",
                    {
                        "seq": seq,
                        "text": text,
                        "original_jpeg": encode_jpeg_bytes(image),
                        "thumb_jpeg": encode_jpeg_bytes(thumbnail),
                    },
                )
            )
        except Exception as exc:  # 网络/协议/图像错误统一收敛为失败结果
            if stop.is_set():
                return
            logger.debug("截图翻译失败（seq=%d）：%s", seq, exc)
            self._queue.put(("screenshot_fail", {"seq": seq, "error": str(exc)}))

    def _handle_screenshot_done(self, item: dict) -> None:
        """主线程：校验世代号 → 正文显示译文 → 原图/缩略图落库（不写两级缓存）。

        截图翻译成功/失败与单击/自动翻译共用同一组标题文案（「翻译完成」/
        「翻译失败」）；跳过注入模式下文本翻译入口已禁用，该组文案仅由截图
        翻译触发（需求：共用提示需处理——入口禁用后不产生歧义）。
        """
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("截图翻译结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        text = str(item.get("text") or "").strip()
        if not text:
            logger.warning("截图翻译返回空文本，保持当前显示")
            self._update_title("翻译失败")
            return
        self._display_text(text + "\n")
        self._update_title("翻译完成")
        logger.info("截图翻译完成（seq=%d，译文 %d 字），已显示在正文窗", seq, len(text))
        if self._screenshot_store is None:
            return  # 数据库不可用：仅显示不入库
        record_id = self._screenshot_store.insert(
            ts=int(time.time() * 1000),  # 13 位毫秒时间戳（需求）
            ocr_text=text,
            img_original=bytes(item.get("original_jpeg") or b""),
            img_thumbnail=bytes(item.get("thumb_jpeg") or b""),
        )
        if record_id is None:
            logger.warning("截图记录入库失败，仅保留正文显示")

    def _handle_screenshot_fail(self, item: dict) -> None:
        """主线程：截图翻译失败收尾（提示与单击/自动一致；内存图像已随 worker 释放）。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("截图翻译失败结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        logger.warning("截图翻译失败：%s", item.get("error"))
        self._update_title("翻译失败")

    # -- 遥控器自动化（右键菜单开关；提交走视觉链路但不隐藏窗口、不落库） ------

    def _is_translation_busy(self) -> bool:
        """自动化互斥查询（主线程）：存在任意在途翻译/识曲/对话即忙碌。"""
        return self._translating or self._recognizing or self._chat_busy

    def _display_remote_auto_text(self, text: str) -> None:
        """自动化缓存命中直接上屏（主线程，控制器回调；不发 API）。"""
        self._display_text(text + "\n")
        self._update_title("翻译完成")

    def _submit_remote_auto_translation(
        self, window, image, signature: tuple[int, ...]
    ) -> bool:
        """自动化截图翻译提交（主线程，控制器回调）。

        与手动截图翻译共用 ``_translating`` 互斥槽（同时只允许一个在途），
        但不隐藏任何窗口（需求：防爆闪），不写 screenshot.db（需求）；
        受理返回 True，宿主忙碌被拒返回 False（控制器保持变化状态等待恢复）。
        """
        if self._closed:
            return False
        if self._is_translation_busy():
            logger.info("自动化截图翻译提交时发现在途请求，拒绝（控制器稍后重试）")
            return False
        self._translating = True
        self._translation_seq += 1
        seq = self._translation_seq
        stop = threading.Event()
        self._current_stop = stop
        self._update_title("截图翻译中，翻译速度会更慢...")
        api_options = {  # 视觉链路基址/模型/密钥独立于文本链路（与手动同源）
            "base_url": self._config.api_base_url_vision or self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": self._config.model_vision,
            "api_key": self._config.api_key_vision,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
        }
        thread = threading.Thread(
            target=self._remote_auto_worker,
            args=(seq, image, signature, stop, api_options),
            name=f"remote-auto-translate-{seq}",
            daemon=True,
        )
        thread.start()
        return True

    def _remote_auto_worker(
        self,
        seq: int,
        image,
        signature: tuple[int, ...],
        stop: threading.Event,
        api_options: dict,
    ) -> None:
        """后台线程：自动化截图的视觉链路（宽高比约束副本 → 压缩 → 识图翻译）。

        与手动 worker 的区别：不生成缩略图/原图、不落库；中止时也回传
        （携带 aborted 标记），保证控制器的在途标记一定被清除——否则自动
        化会永久停在「在途」态不再比较变化。
        """
        aborted = False
        text = None
        error = None
        try:
            ratio_image = constrain_aspect_ratio(image)
            api_image = scale_to_percent(
                ratio_image, self._config.screenshot_compress_percent
            )
            jpeg_base64 = encode_jpeg_base64(api_image)
            if stop.is_set():
                aborted = True
            else:
                # 带结果校验（与手动同源）：偶发「只识别不翻译」时强化指令重试一次
                text = translate_image_verified(
                    jpeg_base64, abort_check=stop.is_set, **api_options
                )
                if stop.is_set():
                    aborted = True
        except Exception as exc:  # 网络/协议/图像错误统一收敛为失败结果
            if stop.is_set():
                aborted = True
            else:
                error = str(exc)
                logger.debug("自动化截图翻译失败（seq=%d）：%s", seq, exc)
        self._queue.put(
            (
                "remote_auto_done",
                {
                    "seq": seq,
                    "signature": signature,
                    "text": text,
                    "aborted": aborted,
                    "error": error,
                },
            )
        )

    def _handle_remote_auto_done(self, item: dict) -> None:
        """主线程：自动化翻译结果收尾（显示 + 缓存回填；不写 screenshot.db）。

        seq 过期/中止的结果也要回传控制器清除在途标记（否则自动化停在
        「在途」态不再比较变化）；中止/失败不回填缓存。
        """
        seq = item.get("seq")
        current = seq == self._translation_seq
        if current:
            self._translating = False
        else:
            logger.info(
                "自动化截图翻译结果已过期（seq=%s，当前=%s），仅回传控制器",
                seq,
                self._translation_seq,
            )
        aborted = bool(item.get("aborted"))
        text = str(item.get("text") or "").strip()
        if not aborted and current and text:
            self._display_text(text + "\n")
            self._update_title("翻译完成")
            logger.info(
                "自动化截图翻译完成（seq=%d，译文 %d 字），已显示在正文窗", seq, len(text)
            )
        elif not aborted and current:
            logger.warning("自动化截图翻译失败：%s", item.get("error") or "返回空文本")
            self._update_title("翻译失败")
        # 在途回执无条件回传（成功才回填缓存；中止/失败传 None 不污染缓存）
        self._remote_automation.on_translation_result(
            item.get("signature"), text if not aborted and text else None
        )
