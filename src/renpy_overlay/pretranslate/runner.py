"""批量任务编排：统计 → 动态并发翻译 → 落库的可中止流水线（无线程创建代码）。

移植自独立工具 renpy-overlay-batch-translator 的 ``runner.py``，替换旧逐条
串行实现。差异（相对旧 ``PrebuildTask``）：阶段 2 由逐条串行改为**动态并发**
——调度循环按 :class:`~renpy_overlay.pretranslate.concurrency.AdaptiveLimiter`
的当前上限派发请求到 ``ThreadPoolExecutor``，随服务端处理速度自适应增减并发
（见 ``concurrency`` 模块）；单条重试 / 熔断 / 终态幂等语义与独立工具一致。

运行模型：由调用方（预构建主窗口）在后台守护线程里执行 :meth:`BatchTask.run`，
本类只负责条目级流水 —— 两阶段：

1. **统计**（无 API 调用）：解析勾选文件 → 清洗去重 → 逐条 ``store.has``
   判存，得到本轮待翻译总数 ``total``（进度分母）；条目间响应 ``stop_event``；
2. **翻译**：动态并发调度，单条以与运行时单击翻译一致的参数调用
   ``translator.translate_text``，成功即 ``store.put``（单条单事务，
   任意时刻终止不留残缺条目；写库经进程级写锁与前台翻译链路串行化）；
   单条失败重试 ``retries`` 次仍失败计入失败清单继续；连续
   ``circuit_break_threshold`` 条 ``EndpointUnavailable`` 熔断收束。

进度与阶段经回调产出（GUI 侧经 Qt Signal 转回主线程）；store 为任务私有
连接，``finally`` 中自行关闭，不与其它调用方共享。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from .. import config, translator
from ..translation_store import TranslationStore
from .concurrency import KIND_FAILURE, KIND_SUCCESS, KIND_UNREACHABLE, AdaptiveLimiter
from .parser import SourceText, collect_texts

logger = logging.getLogger("renpy_overlay.pretranslate.runner")


@dataclass(frozen=True)
class BatchOptions:
    """一轮批量任务的 API 与并发参数（来自 :class:`config.AppConfig`）。"""

    base_url: str
    timeout: float
    model: str
    system_prompt: str
    api_key: str
    enable_thinking: bool
    reasoning_effort: str
    max_concurrency: int
    initial_concurrency: int
    increase_after: int
    latency_guard_seconds: float
    circuit_break_threshold: int
    retries: int

    @classmethod
    def from_config(cls, cfg: config.AppConfig) -> BatchOptions:
        """从配置对象构造（与运行时单击翻译一致的 API 参数集，非流式）。"""
        return cls(
            base_url=cfg.api_base_url,
            timeout=cfg.api_timeout,
            model=cfg.model,
            system_prompt=cfg.system_prompt,
            api_key=cfg.api_key,
            enable_thinking=cfg.enable_thinking,
            reasoning_effort=cfg.reasoning_effort,
            max_concurrency=cfg.max_concurrency,
            initial_concurrency=cfg.initial_concurrency,
            increase_after=cfg.concurrency_increase_after,
            latency_guard_seconds=cfg.latency_guard_seconds,
            circuit_break_threshold=cfg.circuit_break_threshold,
            retries=cfg.retries,
        )


@dataclass
class Outcome:
    """单条翻译结果（工作线程返回给调度线程）。"""

    kind: str  # KIND_SUCCESS / KIND_FAILURE / KIND_UNREACHABLE
    translated: str = ""
    latency: float = 0.0  # 成功请求的耗时（秒），供时延闸门使用


class BatchTask:
    """一轮批量翻译任务：条目间可取消、进度回调产出、私有 store 自管生命周期。"""

    def __init__(
        self,
        files: list[str],
        game_dir: Path,
        options: BatchOptions,
        store: TranslationStore | None,
        stop_event: threading.Event,
        on_counting: Callable[[int], None],
        on_ready: Callable[[int], None],
        on_progress: Callable[[int, int, int], None],
        on_finished: Callable[[str], None],
        source_dir: Path | None = None,
    ) -> None:
        self._files = list(files)
        self._game_dir = Path(game_dir)
        # 源文件根（三页签页 0 扫描根切到 unrpa 时指向 unrpa）；缺省回退 game_dir，
        # 数据库仍由调用方基于 game_dir 定位，两者解耦
        self._source_dir = Path(source_dir) if source_dir is not None else None
        self._options = options
        self._store = store
        self._stop = stop_event
        self._on_counting = on_counting
        self._on_ready = on_ready
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._finished = False  # on_finished 只发一次（正常/取消/熔断/异常收敛）

    # ---- 对外入口 -----------------------------------------------------------

    def run(self) -> None:
        """执行整轮任务（在后台线程调用）；任何异常收敛为 error 终态。"""
        try:
            self._run_inner()
        except Exception as exc:  # pragma: no cover - 防御：不让线程静默死亡
            logger.exception("批量任务异常终止")
            self._finish("error", exc)

    # ---- 内部流水 -----------------------------------------------------------

    def _run_inner(self) -> None:
        if self._store is None:
            self._finish("error", RuntimeError("翻译数据库不可用"))
            return
        pending = self._count_pending()
        if pending is None:
            return  # 统计阶段取消 / 出错（_finish 已发）
        self._on_ready(len(pending))
        if not pending:
            logger.info("批量处理完成：勾选文件无缺失条目，无需翻译")
            self._finish("completed")
            return
        self._translate_pending(pending)

    def _count_pending(self) -> list[SourceText] | None:
        """阶段 1：解析 + 清洗去重 + 判存，返回缺失条目；取消返回 None。

        源文件从 :attr:`_source_dir`（缺省 game_dir）加载：相对路径均相对
        该根（页 0 扫描根为 unrpa 时即 unrpa 内相对路径）。
        """
        try:
            texts = collect_texts(
                self._files,
                self._source_dir or self._game_dir,
                on_file=lambda _done, _total, discovered: self._on_counting(discovered),
            )
        except Exception as exc:  # pragma: no cover - collect 内部已按文件兜底
            self._finish("error", exc)
            return None
        pending: list[SourceText] = []
        for item in texts:
            if self._stop.is_set():
                self._finish("cancelled")
                return None
            try:
                missing = not self._store.has(item.text)
            except Exception as exc:  # pragma: no cover - 库损坏防御
                self._finish("error", exc)
                return None
            if missing:
                pending.append(item)
        return pending

    def _translate_pending(self, pending: list[SourceText]) -> None:
        """阶段 2：动态并发翻译落库；单条失败不中断，连续不可达熔断。"""
        options = self._options
        limiter = AdaptiveLimiter(
            max_concurrency=options.max_concurrency,
            initial_concurrency=options.initial_concurrency,
            increase_after=options.increase_after,
            latency_guard_seconds=options.latency_guard_seconds,
            circuit_break_threshold=options.circuit_break_threshold,
        )
        done = failed = 0
        queue: deque[SourceText] = deque(pending)
        inflight: dict[Future, SourceText] = {}
        status = "completed"
        executor = ThreadPoolExecutor(
            max_workers=max(1, options.max_concurrency), thread_name_prefix="batch"
        )
        try:
            while queue or inflight:
                if self._stop.is_set():
                    # 取消：停止派发，等待在途请求自然完成（已完成的译文照常入库；
                    # 最长等待一个请求超时周期，有界）
                    if inflight:
                        logger.info("批量任务取消：等待 %d 条在途请求收束", len(inflight))
                        wait(list(inflight))
                    status = "cancelled"
                    break
                # 按自适应上限派发：动态并发调度的核心
                while queue and len(inflight) < limiter.current:
                    item = queue.popleft()
                    future = executor.submit(self._translate_worker, item)
                    inflight[future] = item
                completed, _ = wait(set(inflight), timeout=0.2, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = inflight.pop(future)
                    outcome = future.result()  # worker 内部全捕获，不抛异常
                    if outcome.kind == KIND_SUCCESS:
                        limiter.record_success(outcome.latency)
                        done += 1
                    elif outcome.kind == KIND_UNREACHABLE:
                        limiter.record_unreachable()
                        failed += 1
                    else:
                        limiter.record_failure()
                        failed += 1
                    self._on_progress(done, failed, limiter.current)
                    if limiter.broken:
                        logger.error(
                            "批量熔断：连续 %d 条端点不可达，终止本轮",
                            limiter.unreachable_streak,
                        )
                        if inflight:  # 在途请求等待收束（结果照常入库）
                            wait(list(inflight))
                        self._finish("circuit_break")
                        return
            if status == "completed":
                logger.info(
                    "批量处理完成：%d 成功 / %d 失败（共 %d 条待翻译）", done, failed, len(pending)
                )
            self._finish(status)
        except Exception as exc:  # pragma: no cover - 防御：不让线程静默死亡
            logger.exception("批量任务异常终止")
            self._finish("error", exc)
        finally:
            executor.shutdown(wait=True)

    def _translate_worker(self, item: SourceText) -> Outcome:
        """单条翻译（含重试）并落库。永不抛异常（全部收敛为 Outcome）。"""
        options = self._options
        last_error: Exception | None = None
        for attempt in range(options.retries + 1):
            if self._stop.is_set():
                return Outcome(KIND_FAILURE)  # 取消按普通失败计
            try:
                started = time.monotonic()
                translated = str(
                    translator.translate_text(
                        item.text,
                        base_url=options.base_url,
                        timeout=options.timeout,
                        model=options.model,
                        system_prompt=options.system_prompt,
                        api_key=options.api_key,
                        enable_thinking=options.enable_thinking,
                        reasoning_effort=options.reasoning_effort,
                    )
                ).strip()
                latency = time.monotonic() - started
                if translated:
                    if self._store.put(item.text, translated):
                        return Outcome(KIND_SUCCESS, translated, latency)
                    return Outcome(KIND_FAILURE)  # pragma: no cover - 写库失败（已降级）
                last_error = translator.TranslationError("模型返回了空译文")
            except translator.EndpointUnavailable as exc:
                # 端点不可达不做单条重试（备用链路切换在运行时流程内，
                # 预构建重试无意义）→ 直接上报参与熔断计数
                logger.debug("批量链路不可达：%s", exc)
                return Outcome(KIND_UNREACHABLE)
            except translator.TranslationError as exc:
                last_error = exc
                logger.debug("批量单条失败（第 %d 次尝试）：%s…", attempt + 1, item.text[:24])
            except ValueError:  # 空文本无重试意义（上游已过滤，防御兜底）
                return Outcome(KIND_FAILURE)
            except Exception as exc:  # pragma: no cover - 防御：不让工作线程静默死亡
                logger.exception("批量工作线程异常")
                last_error = exc
        logger.warning("批量单条重试耗尽：%s…（%s）", item.text[:24], last_error)
        return Outcome(KIND_FAILURE)

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
            logger.error("批量任务异常收束（status=%s）：%s", status, exc)
        self._on_finished(status)
