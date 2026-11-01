"""自适应并发控制器验证：AIMD 升降级、时延闸门、熔断与边界钳制。"""

from __future__ import annotations

from renpy_overlay.pretranslate.concurrency import (
    KIND_FAILURE,
    KIND_SUCCESS,
    KIND_UNREACHABLE,
    AdaptiveLimiter,
)


def test_initial_clamped_to_range():
    assert AdaptiveLimiter(8, initial_concurrency=100).current == 8
    assert AdaptiveLimiter(8, initial_concurrency=0).current == 1
    assert AdaptiveLimiter(1, initial_concurrency=5).current == 1
    assert AdaptiveLimiter(4).current == 1  # 默认保守起步


def test_success_streak_increases_concurrency():
    limiter = AdaptiveLimiter(4, initial_concurrency=1, increase_after=3)
    for _round in range(3):
        limiter.record_success(0.5)
    assert limiter.current == 2
    for _round in range(3):
        limiter.record_success(0.5)
    assert limiter.current == 3
    assert not limiter.broken


def test_success_caps_at_max():
    limiter = AdaptiveLimiter(2, initial_concurrency=1, increase_after=2)
    for _ in range(20):
        limiter.record_success(0.1)
    assert limiter.current == 2


def test_latency_guard_blocks_increase():
    """近期时延中位数超过闸门 → 连成功再多也不升档。"""
    limiter = AdaptiveLimiter(4, initial_concurrency=1, increase_after=2, latency_guard_seconds=5.0)
    for _ in range(10):
        limiter.record_success(9.0)
    assert limiter.current == 1


def test_latency_guard_uses_median_window():
    """窗口内偶发慢请求被中位数吸收，不阻塞升档。"""
    limiter = AdaptiveLimiter(4, initial_concurrency=1, increase_after=3, latency_guard_seconds=5.0)
    limiter.record_success(0.5)
    limiter.record_success(0.6)
    limiter.record_success(30.0)  # 偶发慢请求
    assert limiter.current == 2  # 中位数 0.6 < 5.0


def test_failure_halves_with_floor():
    limiter = AdaptiveLimiter(8, initial_concurrency=8, increase_after=100)
    limiter.record_failure()
    assert limiter.current == 4
    limiter.record_failure()
    assert limiter.current == 2
    limiter.record_failure()
    assert limiter.current == 1
    limiter.record_failure()
    assert limiter.current == 1  # 下限 1


def test_unreachable_halves_and_circuit_breaks():
    limiter = AdaptiveLimiter(8, initial_concurrency=4, circuit_break_threshold=3)
    limiter.record_unreachable()
    assert limiter.current == 2
    assert limiter.unreachable_streak == 1
    assert not limiter.broken
    limiter.record_unreachable()
    assert not limiter.broken
    limiter.record_unreachable()
    assert limiter.broken  # 连续 3 条 → 熔断


def test_success_resets_unreachable_streak():
    limiter = AdaptiveLimiter(8, circuit_break_threshold=3)
    limiter.record_unreachable()
    limiter.record_unreachable()
    limiter.record_success(0.5)
    assert limiter.unreachable_streak == 0
    assert not limiter.broken


def test_success_resets_after_failure():
    """失败清零连成功计数：减档后需重新积累连成功才升档。"""
    limiter = AdaptiveLimiter(4, initial_concurrency=2, increase_after=2)
    limiter.record_success(0.1)
    limiter.record_failure()
    assert limiter.current == 1
    limiter.record_success(0.1)
    assert limiter.current == 1  # 只有 1 条连成功，不升档
    limiter.record_success(0.1)
    assert limiter.current == 2


def test_record_dispatches_by_kind():
    limiter = AdaptiveLimiter(8, circuit_break_threshold=1)
    limiter.record(KIND_UNREACHABLE)
    assert limiter.broken

    limiter2 = AdaptiveLimiter(8, initial_concurrency=2)
    limiter2.record(KIND_FAILURE)
    assert limiter2.current == 1

    limiter3 = AdaptiveLimiter(8, initial_concurrency=1, increase_after=1)
    limiter3.record(KIND_SUCCESS, latency=0.1)
    assert limiter3.current == 2
