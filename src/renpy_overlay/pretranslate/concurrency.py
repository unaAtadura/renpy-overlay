"""自适应并发控制器：AIMD 升降级 + 时延闸门 + 不可达熔断。

根据 OpenAI 兼容服务端（LM Studio 等）的实际处理速度动态调整并发翻译
的在途请求数上限，在保证稳定性的前提下逼近最大吞吐：

- **加性增长**：连续成功达到 ``increase_after`` 条，且近期成功时延的
  中位数低于 ``latency_guard_seconds``（服务端未过载的信号），并发 +1；
- **乘性减少**：任何失败（普通翻译错误 / 端点不可达）把并发减半
  （下限 1）——失败通常意味着服务端过载或模型正在切换；
- **熔断**：连续 ``circuit_break_threshold`` 条端点不可达判定服务离线，
  置位熔断标志，由批量任务收束本轮。

纯逻辑无线程代码：所有方法只被批量任务的调度线程（收割 Future 的一侧）
调用，无需加锁。
"""

from __future__ import annotations

import statistics
from collections import deque

#: 近期成功时延滚动窗口容量（条）
LATENCY_WINDOW = 16
#: 并发下限（再低无法成批，减半到此为止）
MIN_CONCURRENCY = 1

#: 请求结果事件类别
KIND_SUCCESS = "success"  # 翻译成功并已入库
KIND_FAILURE = "failure"  # 普通翻译失败（HTTP 错误 / 空译文 / 协议不符，重试耗尽）
KIND_UNREACHABLE = "unreachable"  # 端点不可达（连接失败/超时）


class AdaptiveLimiter:
    """AIMD 并发上限控制器（见模块文档）。"""

    def __init__(
        self,
        max_concurrency: int,
        initial_concurrency: int = 1,
        increase_after: int = 6,
        latency_guard_seconds: float = 15.0,
        circuit_break_threshold: int = 5,
    ) -> None:
        self._max = max(MIN_CONCURRENCY, int(max_concurrency))
        self._current = min(max(MIN_CONCURRENCY, int(initial_concurrency)), self._max)
        self._increase_after = max(1, int(increase_after))
        self._latency_guard = float(latency_guard_seconds)
        self._cb_threshold = max(1, int(circuit_break_threshold))
        self._success_streak = 0
        self._unreachable_streak = 0
        self._latencies: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._broken = False

    # ---- 状态 ---------------------------------------------------------------

    @property
    def current(self) -> int:
        """当前允许的在途请求数上限。"""
        return self._current

    @property
    def max_concurrency(self) -> int:
        return self._max

    @property
    def broken(self) -> bool:
        """连续不可达达到阈值（服务判定离线）。"""
        return self._broken

    @property
    def unreachable_streak(self) -> int:
        return self._unreachable_streak

    # ---- 事件 ---------------------------------------------------------------

    def record(self, kind: str, latency: float = 0.0) -> None:
        """记录一次请求结果（``kind`` 取 KIND_* 常量）。"""
        if kind == KIND_SUCCESS:
            self.record_success(latency)
        elif kind == KIND_UNREACHABLE:
            self.record_unreachable()
        else:
            self.record_failure()

    def record_success(self, latency: float) -> None:
        """单条成功：连不可达清零；连成功达标且时延未过载 → 并发 +1。"""
        self._unreachable_streak = 0
        self._success_streak += 1
        self._latencies.append(max(0.0, float(latency)))
        if self._success_streak >= self._increase_after and self._current < self._max:
            if self._median_latency() < self._latency_guard:
                self._current += 1
                self._success_streak = 0

    def record_failure(self) -> None:
        """普通失败（重试耗尽）：连成功清零，并发减半。"""
        self._success_streak = 0
        self._current = max(MIN_CONCURRENCY, self._current // 2)

    def record_unreachable(self) -> None:
        """端点不可达：连成功清零，并发减半，并累积熔断计数。"""
        self._success_streak = 0
        self._current = max(MIN_CONCURRENCY, self._current // 2)
        self._unreachable_streak += 1
        if self._unreachable_streak >= self._cb_threshold:
            self._broken = True

    # ---- 内部 ---------------------------------------------------------------

    def _median_latency(self) -> float:
        """近期成功时延中位数（秒）；窗口为空返回 0.0（视为未过载）。"""
        if not self._latencies:
            return 0.0
        return float(statistics.median(self._latencies))
