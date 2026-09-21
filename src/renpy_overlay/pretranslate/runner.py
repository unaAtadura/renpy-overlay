"""预构建任务编排：统计 → 翻译 → 落库的可中止流水线（纯逻辑，无线程代码）。

运行模型（设计文档第七节）：由调用方（``stream_window``）在后台守护线程里
执行 :meth:`PrebuildTask.run`，本类只负责条目级流水 —— 两阶段：

1. **统计**（无 API 调用）：解析勾选文件 → 清洗去重 → 逐条 ``store.get``
   判存，得到本轮待翻译总数 ``total``（进度分母）；条目间响应 ``stop_event``；
2. **翻译**：逐条以与运行时一致的参数调用 ``translator.translate_text``，
   成功即 ``store.put``（单条单事务，任意时刻终止不留残缺条目）；单条失败
   重试 ``DEFAULT_RETRIES`` 次仍失败计入失败清单继续；连续
   ``DEFAULT_CIRCUIT_BREAK`` 条 ``EndpointUnavailable`` 熔断收束。

进度与阶段经回调产出（调用方入队回 Qt 主线程）；store 为任务私有连接，
``finally`` 中自行关闭，不与前台共享。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from .. import config, translator
from ..translation_store import TranslationStore
from .parser import SourceText, collect_texts

logger = logging.getLogger("renpy_overlay.pretranslate.runner")

#: 单条翻译失败的最大重试次数（首次 + 重试共 3 次尝试）
DEFAULT_RETRIES = 2
#: 连续不可达（主备链路均 EndpointUnavailable）熔断阈值
DEFAULT_CIRCUIT_BREAK = 5


class PrebuildTask:
    """一轮预构建任务：条目间可取消、进度回调产出、私有 store 自管生命周期。"""

    def __init__(
        self,
        files: list[str],
        game_dir: Path,
        cfg: config.AppConfig,
        store: TranslationStore | None,
        stop_event: threading.Event,
        on_phase: Callable[[str, dict], None],
        on_progress: Callable[[int, int, int], None],
        on_finished: Callable[[str], None],
    ) -> None:
        self._files = list(files)
        self._game_dir = Path(game_dir)
        self._cfg = cfg
        self._store = store
        self._stop = stop_event
        self._on_phase = on_phase
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._finished = False  # on_finished 只发一次（正常/取消/熔断/异常收敛）
        # 与运行时 _start_translation 完全一致的 API 参数集（非流式版）
        self._api_options = {
            "base_url": cfg.api_base_url,
            "timeout": cfg.api_timeout,
            "model": cfg.model,
            "system_prompt": cfg.system_prompt,
            "api_key": cfg.api_key,
            "enable_thinking": cfg.enable_thinking,
            "reasoning_effort": cfg.reasoning_effort,
            "base_url_stanby": cfg.api_base_url_stanby,
            "api_key_stanby": cfg.api_key_stanby,
            "model_stanby": cfg.model_stanby,
        }

    # ---- 对外入口 -----------------------------------------------------------

    def run(self) -> None:
        """执行整轮任务（在后台线程调用）；任何异常收敛为 error 终态。"""
        try:
            self._run_inner()
        except Exception as exc:  # pragma: no cover - 防御：不让线程静默死亡
            logger.exception("预构建任务异常终止")
            self._finish("error", exc)

    # ---- 内部流水 -----------------------------------------------------------

    def _run_inner(self) -> None:
        if self._store is None:
            self._finish("error", RuntimeError("翻译数据库不可用"))
            return
        self._on_phase("counting", {})
        pending = self._count_pending()
        if pending is None:
            return  # 统计阶段取消（_finish 已发）
        self._on_phase("ready", {"total": len(pending)})
        self._translate_pending(pending)

    def _count_pending(self) -> list[SourceText] | None:
        """阶段 1：解析 + 清洗去重 + 判存，返回缺失条目；取消返回 None。"""
        try:
            texts = collect_texts(self._files, self._game_dir)
        except Exception as exc:  # pragma: no cover - collect 内部已按文件兜底
            self._finish("error", exc)
            return None
        self._on_phase("counting", {"discovered": len(texts)})
        pending: list[SourceText] = []
        for item in texts:
            if self._stop.is_set():
                self._finish("cancelled")
                return None
            try:
                missing = self._store.get(item.text) is None
            except Exception as exc:  # pragma: no cover - 库损坏防御
                self._finish("error", exc)
                return None
            if missing:
                pending.append(item)
        return pending

    def _translate_pending(self, pending: list[SourceText]) -> None:
        """阶段 2：逐条翻译落库；单条失败不中断，连续不可达熔断。"""
        done = failed = skipped = 0
        consecutive_unreachable = 0
        for item in pending:
            if self._stop.is_set():
                self._finish("cancelled")
                return
            translated = self._translate_one(item.text)
            if isinstance(translated, EndpointRun):
                consecutive_unreachable += 1
                failed += 1
                if consecutive_unreachable >= DEFAULT_CIRCUIT_BREAK:
                    logger.error(
                        "预构建熔断：连续 %d 条主备链路均不可达，终止本轮",
                        consecutive_unreachable,
                    )
                    self._finish("circuit_break")
                    return
            elif translated:
                consecutive_unreachable = 0
                if self._store.put(item.text, translated):
                    done += 1
                else:  # pragma: no cover - 写库失败（已降级）
                    failed += 1
            else:
                consecutive_unreachable = 0
                failed += 1
            self._on_progress(done, failed, skipped)
        logger.info(
            "预构建完成：%d 成功 / %d 失败（共 %d 条待翻译）", done, failed, len(pending)
        )
        self._finish("completed")

    def _translate_one(self, text: str) -> str | None | EndpointRun:
        """单条翻译（含重试）。返回译文；重试耗尽返回 None；持续不可达返回哨兵。"""
        last_error: Exception | EndpointRun | None = None
        for attempt in range(DEFAULT_RETRIES + 1):
            if self._stop.is_set():
                return None  # 取消按普通失败计，外层下一轮循环前即收束
            try:
                translated = str(
                    translator.translate_text(text, **self._api_options)
                ).strip()
                if translated:
                    return translated
                last_error = translator.TranslationError("模型返回了空译文")
            except translator.EndpointUnavailable as exc:
                last_error = _ENDPOINT_SENTINEL  # 备选链路也失败才累积熔断计数
                logger.debug("预构建链路不可达（第 %d 次尝试）：%s", attempt + 1, exc)
            except translator.TranslationError as exc:
                last_error = exc
                logger.debug("预构建单条失败（第 %d 次尝试）：%s…", attempt + 1, text[:24])
            except ValueError:  # 空文本无重试意义（上游已过滤，防御兜底）
                return None
        if last_error is None:  # pragma: no cover - 循环至少一次，必有错误
            return None
        if isinstance(last_error, EndpointRun):
            return last_error  # 主备链路均不可达：参与外层熔断计数
        logger.warning("预构建单条重试耗尽：%s…（%s）", text[:24], last_error)
        return None

    def _finish(self, status: str, exc: Exception | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        if self._store is not None:
            try:
                self._store.close()
            except Exception:  # pragma: no cover - 关闭失败不影响退出
                pass
        if exc is not None:
            logger.error("预构建任务异常收束（status=%s）：%s", status, exc)
        self._on_finished(status)


class EndpointRun:
    """``_translate_one`` 的哨兵返回：主备链路均不可达（参与熔断计数）。"""


_ENDPOINT_SENTINEL = EndpointRun()
