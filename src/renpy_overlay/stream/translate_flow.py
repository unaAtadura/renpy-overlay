"""流式翻译与自动翻译的门面 Mixin：SSE 流式链路 + 轮询与两级缓存。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``skip_injection`` / ``_translating`` / ``_recognizing`` /
``_chat_busy`` / ``_last_say`` / ``_translation_seq`` / ``_stream_seq`` /
``_current_stop`` / ``_last_translation_input`` / ``_config`` / ``_queue`` /
``_translation_cache`` / ``_translation_store`` / ``_body_window`` /
``_auto_translate_on`` / ``_auto_timer`` / ``_locked`` / ``_lock_started_at`` /
``_auto_skip_reason``，以及 ``_update_title`` / ``_display_text``。
"""

from __future__ import annotations

import logging
import threading
import time

from .. import translator

logger = logging.getLogger("renpy_overlay.stream_window")


class StreamTranslateMixin:
    """流式翻译请求/worker/收尾与自动翻译轮询、两级缓存命中。"""

    # ------------------------------------------------------------ 翻译（流式）

    def _start_translation(self, origin: str = "manual") -> None:
        """发起流式翻译请求（仅主线程调用）。同一时刻只允许一条在途请求。"""
        label = "自动触发" if origin == "auto" else "单击触发"
        if self.skip_injection:
            # 跳过注入模式：无游戏文本捕获（需求：单击/自动翻译不可用）
            logger.info("跳过注入模式：%s翻译不可用（需注入捕获游戏文本）", label)
            return
        if self._translating or self._recognizing or self._chat_busy:
            logger.info("已有请求在途（翻译/识曲/对话），忽略本次%s翻译", label)
            return
        say = self._last_say or {}
        what = str(say.get("what") or "").strip()
        if not what:
            logger.info("暂无可翻译的捕获文本，跳过翻译请求")
            return
        who = str(say.get("who") or "").strip()
        self._translating = True
        self._translation_seq += 1
        seq = self._translation_seq
        self._last_translation_input = what  # 记录已触发的原文，自动翻译据此去重
        logger.info("发起流式翻译请求（%s，seq=%d，原文 %d 字）：%s", label, seq, len(what), what[:40])
        self._update_title("翻译中...")
        api_options = {  # 连接参数来自 config.json（未配置时为与历史一致的默认值）
            "base_url": self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": self._config.model,
            "system_prompt": self._config.system_prompt,
            "api_key": self._config.api_key,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
        }
        thread = threading.Thread(
            target=self._translate_worker,
            args=(seq, who, what, origin, api_options),
            name=f"stream-translate-{seq}",
            daemon=True,
        )
        thread.start()

    def _translate_worker(
        self, seq: int, who: str, what: str, origin: str, api_options: dict
    ) -> None:
        """后台线程：SSE 流式翻译，增量逐块入队，结果只经队列回主线程。"""
        stop = threading.Event()
        self._current_stop = stop
        chunks: list[str] = []
        try:
            for piece in translator.translate_text_stream(what, **api_options):
                if stop.is_set():
                    return  # 已作废：停止喂块，也不投递结束消息
                chunks.append(piece)
                self._queue.put(("chunk", {"seq": seq, "text": piece}))
            message = {
                "seq": seq,
                "ok": True,
                "who": who,
                "what": "".join(chunks),
                "input": what,  # 原文随结果回传，供主线程写缓存
                "origin": origin,
            }
        except Exception as exc:  # 网络/协议错误统一收敛为失败结果
            if stop.is_set():
                return
            logger.debug("翻译请求失败（seq=%d）：%s", seq, exc)
            message = {"seq": seq, "ok": False, "who": who, "error": str(exc), "origin": origin}
        self._queue.put(("translation", message))

    def _handle_translation(self, item: dict) -> None:
        """主线程：校验世代号后收尾（正文已由增量流式呈现完整译文）。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("翻译结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        if not item.get("ok"):
            logger.warning("翻译失败：%s", item.get("error"))
            self._update_title("翻译失败")  # 部分流内容保留在正文区，便于排查
            return
        who = str(item.get("who") or "").strip()
        text = str(item.get("what") or "").strip()
        if not text:
            logger.warning("翻译返回空文本，保持当前显示")
            self._update_title("翻译失败")
            return
        logger.info("翻译完成（seq=%s），译文已流式呈现（%d 字）", seq, len(text))
        if self._stream_seq != seq:  # 兜底：全程无有效增量时整段补显
            self._display_text((who + "\n" if who else "") + text + "\n")
        self._update_title("翻译完成")
        # 成功后同步写入两级缓存（手动 / 自动一致）：内存覆盖视为新写入，
        # 数据库 UPSERT 覆盖同一原文的旧译文；失败由各自模块内部降级并记日志
        input_text = str(item.get("input") or "").strip()
        if input_text:
            self._translation_cache.put(input_text, text)
            if self._translation_store is not None:
                self._translation_store.put(input_text, text)
            logger.debug(
                "翻译结果已写入缓存（%s触发，内存占用 %d/%d 字节）",
                "自动" if item.get("origin") == "auto" else "手动",
                self._translation_cache.size(),
                self._translation_cache.max_bytes,
            )
        else:  # pragma: no cover - 正常流程必然带原文
            logger.warning("翻译结果缺少原文，跳过缓存写入")

    def _abort_translation(self, reason: str) -> None:
        """作废在途翻译：世代号递增使结果不再上屏，stop 让网络等待尽快结束。"""
        if not self._translating:
            return
        self._translation_seq += 1
        self._translating = False
        stop = self._current_stop
        if stop is not None:
            stop.set()
        logger.info("已中止在途翻译（%s）：结果作废，界面保持当前状态", reason)

    # -- 自动翻译（config.json: auto_translate / auto_translate_interval） ----

    def _start_auto_translate(self) -> None:
        """启动自动翻译轮询（仅在当前模式为自动翻译时；初值来自 config.json）。"""
        if self.skip_injection:
            # 跳过注入模式：无游戏文本捕获（需求：自动翻译不可用）
            logger.info("跳过注入模式：自动翻译不可用（需注入捕获游戏文本）")
            return
        if not self._auto_translate_on:
            logger.info("自动翻译未启用（config.json: auto_translate=false）")
            return
        interval_ms = self._auto_interval_ms()
        logger.info("自动翻译已启用：每 %.1f 秒轮询一次（仅锁定状态生效）", interval_ms / 1000.0)
        self._auto_timer.setInterval(interval_ms)
        self._auto_timer.start()

    def _auto_interval_ms(self) -> int:
        return max(200, int(self._config.auto_translate_interval * 1000))

    def _auto_translate_tick(self) -> None:
        """自动翻译轮询（主线程 QTimer，与 drain/follow 同模式）。"""
        if self._closed:
            return
        try:
            self._auto_translate_check()
        except Exception:  # pragma: no cover - 轮询异常不应终止循环
            logger.exception("自动翻译轮询出错")

    def _auto_translate_check(self, immediate: bool = False) -> None:
        """条件全部满足时自动发起一次翻译；任一不满足则跳过（原因去重记日志）。

        ``immediate=True``（事件驱动路径：新 say 刚到达时调用）跳过「刚进入
        锁定」间隔门——新捕获的文本是刚显示的台词，不存在锁定时立即翻译陈旧
        文本的问题；轮询路径（``immediate=False``）保持原间隔门不变。
        """
        if not self._auto_translate_on:
            return
        if not self._locked:
            self._auto_note_skip("未锁定")
            return
        if (
            not immediate
            and time.time() - self._lock_started_at < self._config.auto_translate_interval
        ):
            self._auto_note_skip("刚进入锁定，等待一个完整轮询间隔")
            return
        say = self._last_say or {}
        who = str(say.get("who") or "").strip()
        what = str(say.get("what") or "").strip()
        if not what:
            self._auto_note_skip("暂无可翻译的捕获文本")
            return
        if what == self._last_translation_input:
            self._auto_note_skip("该条原文已翻译过")
            return
        if self._recognizing:
            # 听歌识曲在途：识曲结果即将整屏替换正文，缓存命中也不上屏
            self._auto_note_skip("听歌识曲进行中")
            return
        if self._chat_busy:
            self._auto_note_skip("AI 对话请求在途")
            return

        # 缓存命中优先：依次查内存与数据库，任一级命中则直接上屏并记录"已翻译"
        cached = self._translation_cache.get(what)
        if cached is not None:
            self._auto_use_cached(what, who, cached, "缓存")
            return
        logger.debug("自动翻译内存缓存未命中，继续查数据库（原文 %d 字）", len(what))
        stored = self._translation_store.get(what) if self._translation_store is not None else None
        if stored is not None:
            if self._translation_cache.put(what, stored):
                logger.info("已将数据库译文回填内存缓存（原文 %d 字）", len(what))
            self._auto_use_cached(what, who, stored, "数据库")
            return
        logger.debug("自动翻译两级缓存均未命中，改走流式 API 请求（原文 %d 字）", len(what))

        if self._translating:
            self._auto_note_skip("已有翻译请求在途")
            return
        self._auto_skip_reason = None
        self._start_translation(origin="auto")

    def _auto_use_cached(self, what: str, who: str, translated: str, source: str) -> None:
        """自动翻译缓存命中（内存或数据库）：直接上屏，不发起网络请求。"""
        self._auto_skip_reason = None
        self._last_translation_input = what
        # 记录最近命中记录（原文）：供「定位翻译位置」入口生成定位入参
        self._last_cache_hit_original = what
        if self._translating:
            # 在途请求针对的是旧原文：作废它，避免其结果稍后覆盖本次命中内容
            self._abort_translation(f"自动翻译{source}命中")
        logger.info("自动翻译%s命中（原文 %d 字）：直接上屏，未发起网络请求", source, len(what))
        self._update_title("翻译完成")
        self._display_text((who + "\n" if who else "") + translated + "\n")

    def _auto_note_skip(self, reason: str) -> None:
        if reason == self._auto_skip_reason:
            return
        self._auto_skip_reason = reason
        logger.debug("自动翻译跳过：%s", reason)
