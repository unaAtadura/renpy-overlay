"""批量任务流水验证：假 translator / 假 store 驱动的回调序列、取消与熔断。

monkeypatch 替换 ``runner`` 所引用的 ``translator.translate_text``，用鸭子
接口假对象替换 store，同步调用 ``task.run()`` 断言回调与入库行为。
"""

from __future__ import annotations

import threading

import pytest

from renpy_overlay import translator
from renpy_overlay.pretranslate.runner import BatchOptions, BatchTask
from renpy_overlay.translation_store import open_store


class FakeStore:
    """鸭子接口假 store：线程安全记录 put 调用（供并发断言）。"""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self._data = dict(existing or {})
        self._lock = threading.Lock()
        self.put_calls: list[tuple[str, str]] = []
        self.closed = False

    def has(self, original: str) -> bool:
        with self._lock:
            return original in self._data

    def put(self, original: str, translated: str) -> bool:
        with self._lock:
            self.put_calls.append((original, translated))
            self._data[original] = translated
        return True

    def close(self) -> None:
        self.closed = True


def make_options(**overrides) -> BatchOptions:
    values = dict(
        base_url="http://stub",
        timeout=1.0,
        model="m",
        system_prompt="sys",
        api_key="",
        enable_thinking=False,
        reasoning_effort="none",
        max_concurrency=2,
        initial_concurrency=1,
        increase_after=2,
        latency_guard_seconds=15.0,
        circuit_break_threshold=3,
        retries=0,
    )
    values.update(overrides)
    return BatchOptions(**values)


def make_task(tmp_path, files, store, options, recordings):
    """构造任务并把回调产物记入 recordings 字典（列表）。"""
    stop = threading.Event()
    task = BatchTask(
        files=files,
        game_dir=tmp_path,
        options=options,
        store=store,
        stop_event=stop,
        on_counting=lambda discovered: recordings["counting"].append(discovered),
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda done, failed, concurrency: recordings["progress"].append(
            (done, failed, concurrency)
        ),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    return task, stop


def write_script(tmp_path, name: str, count: int, prefix: str = "Line") -> str:
    game = tmp_path / "game"
    game.mkdir(exist_ok=True)
    body = "".join(f'e "{prefix}{index}\\n"\n' for index in range(1, count + 1))
    (game / name).write_text(body, encoding="utf-8")
    return f"game/{name}"


# ------------------------------------------------------------------ 正常完成


def test_completed_run_records_and_finishes(tmp_path, monkeypatch):
    rel = write_script(tmp_path, "a.rpy", 3)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, stop = make_task(tmp_path, [rel], store, make_options(), recordings)

    monkeypatch.setattr(
        translator, "translate_text", lambda text, **kwargs: f"译[{text}]", raising=True
    )
    task.run()

    assert recordings["finished"] == ["completed"]  # 终态只发一次
    assert recordings["ready"] == [3]
    assert len(store.put_calls) == 3
    assert store.closed
    # increase_after=2：第 2 条成功后并发爬坡 1 → 2，末条进度报告并发 2
    assert recordings["progress"] == [(1, 0, 1), (2, 0, 2), (3, 0, 2)]
    # 全部进度回调的 done 单调递增到 total
    assert [done for done, _failed, _conc in recordings["progress"]] == [1, 2, 3]


def test_existing_entries_skipped(tmp_path, monkeypatch):
    rel = write_script(tmp_path, "a.rpy", 2)
    store = FakeStore(existing={"Line1": "已有译文"})
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [rel], store, make_options(), recordings)

    requested: list[str] = []

    def spy(text, **kwargs):
        requested.append(text)
        return f"译[{text}]"

    monkeypatch.setattr(translator, "translate_text", spy, raising=True)
    task.run()

    assert recordings["ready"] == [1]  # 判存后只剩 1 条待翻译
    assert requested == ["Line2"]
    assert len(store.put_calls) == 1


def test_source_dir_loads_from_alternate_root(tmp_path, monkeypatch):
    """source_dir：源文件从指定根加载（页 0 unrpa 场景），DB 仍由 game_dir 定位。"""
    source_root = tmp_path / "renpy_overlay_cache" / "unrpa"
    source_root.mkdir(parents=True)
    (source_root / "a.rpy").write_text('e "Line1\\n"\n', encoding="utf-8")
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task = BatchTask(
        files=["a.rpy"],
        game_dir=tmp_path,
        options=make_options(),
        store=store,
        stop_event=threading.Event(),
        on_counting=lambda d: recordings["counting"].append(d),
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda d, f, c: recordings["progress"].append((d, f, c)),
        on_finished=lambda status: recordings["finished"].append(status),
        source_dir=source_root,
    )
    monkeypatch.setattr(
        translator, "translate_text", lambda text, **kwargs: f"译[{text}]", raising=True
    )
    task.run()

    assert recordings["finished"] == ["completed"]
    assert recordings["ready"] == [1]  # 从 source_root 加载到 1 条原文
    assert len(store.put_calls) == 1

    # 对照：不带 source_dir 时从 game/ 加载（该文件不存在）→ 无待翻译
    other: dict[str, list] = {"ready": [], "finished": []}
    fallback = BatchTask(
        files=["a.rpy"],
        game_dir=tmp_path,
        options=make_options(),
        store=store,
        stop_event=threading.Event(),
        on_counting=lambda d: None,
        on_ready=other["ready"].append,
        on_progress=lambda d, f, c: None,
        on_finished=other["finished"].append,
    )
    fallback.run()
    assert other["ready"] == [0]
    assert other["finished"] == ["completed"]


def test_no_pending_finishes_completed_without_requests(tmp_path, monkeypatch):
    rel = write_script(tmp_path, "a.rpy", 1)
    store = FakeStore(existing={"Line1": "已有译文"})
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [rel], store, make_options(), recordings)

    def _fail(text, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("不应发起请求")

    monkeypatch.setattr(translator, "translate_text", _fail, raising=True)
    task.run()

    assert recordings["ready"] == [0]
    assert recordings["finished"] == ["completed"]


def test_real_store_roundtrip_with_stub_translator(tmp_path, monkeypatch):
    """真实 SQLite store + 桩 translator 的端到端小循环（含哈希契约）。"""
    rel = write_script(tmp_path, "a.rpy", 2)
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    stop = threading.Event()
    store = open_store(tmp_path)
    task = BatchTask(
        files=[rel],
        game_dir=tmp_path,
        options=make_options(),
        store=store,
        stop_event=stop,
        on_counting=lambda d: recordings["counting"].append(d),
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda d, f, c: recordings["progress"].append((d, f, c)),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    monkeypatch.setattr(
        translator, "translate_text", lambda text, **kwargs: f"译[{text}]", raising=True
    )
    task.run()

    assert recordings["finished"] == ["completed"]
    # 重新打开同一数据库：上一轮条目已存在 → 无待翻译
    reopened = open_store(tmp_path)
    assert reopened is not None
    assert reopened.has("Line1")
    assert reopened.get("Line2") == "译[Line2]"
    reopened.close()


# ------------------------------------------------------------------ 失败与重试


def test_retries_then_success(tmp_path, monkeypatch):
    rel = write_script(tmp_path, "a.rpy", 1)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [rel], store, make_options(retries=2), recordings)

    attempts = {"n": 0}

    def flaky(text, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise translator.TranslationError("暂时失败")
        return "恢复了"

    monkeypatch.setattr(translator, "translate_text", flaky, raising=True)
    task.run()

    assert attempts["n"] == 3
    assert recordings["finished"] == ["completed"]
    assert len(store.put_calls) == 1


def test_retries_exhausted_counts_failure(tmp_path, monkeypatch):
    rel = write_script(tmp_path, "a.rpy", 1)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [rel], store, make_options(retries=1), recordings)

    attempts = {"n": 0}

    def always_fail(text, **kwargs):
        attempts["n"] += 1
        raise translator.TranslationError("模型拒绝")

    monkeypatch.setattr(translator, "translate_text", always_fail, raising=True)
    task.run()

    assert attempts["n"] == 2  # 首次 + 1 次重试
    assert recordings["finished"] == ["completed"]  # 失败不中断整轮
    assert recordings["progress"] == [(0, 1, 1)]


def test_unreachable_no_per_item_retry(tmp_path, monkeypatch):
    """端点不可达不做单条重试，直接上报（供熔断计数）。"""
    rel = write_script(tmp_path, "a.rpy", 1)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [rel], store, make_options(retries=3), recordings)

    attempts = {"n": 0}

    def unreachable(text, **kwargs):
        attempts["n"] += 1
        raise translator.EndpointUnavailable("连接失败")

    monkeypatch.setattr(translator, "translate_text", unreachable, raising=True)
    task.run()

    assert attempts["n"] == 1


def test_circuit_break_terminates_round(tmp_path, monkeypatch):
    """连续不可达达到阈值 → circuit_break 终态，不再派发新请求。"""
    rel = write_script(tmp_path, "a.rpy", 20)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(
        tmp_path, [rel], store, make_options(circuit_break_threshold=3, max_concurrency=1),
        recordings,
    )

    attempts = {"n": 0}
    lock = threading.Lock()

    def unreachable(text, **kwargs):
        with lock:
            attempts["n"] += 1
        raise translator.EndpointUnavailable("连接失败")

    monkeypatch.setattr(translator, "translate_text", unreachable, raising=True)
    task.run()

    assert recordings["finished"] == ["circuit_break"]
    assert attempts["n"] <= 6  # 阈值 3 + 少量在途，绝无 20 条全打


# ------------------------------------------------------------------ 取消


def test_cancel_stops_dispatch(tmp_path, monkeypatch):
    """进度回调中置位 stop_event：派发停止、终态 cancelled、store 已关闭。"""
    rel = write_script(tmp_path, "a.rpy", 50)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}

    stop = threading.Event()
    requested = {"n": 0}
    lock = threading.Lock()

    def slow_translate(text, **kwargs):
        with lock:
            requested["n"] += 1
        return f"译[{text}]"

    task = BatchTask(
        files=[rel],
        game_dir=tmp_path,
        options=make_options(max_concurrency=1, initial_concurrency=1),
        store=store,
        stop_event=stop,
        on_counting=lambda d: recordings["counting"].append(d),
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda done, failed, concurrency: (
            recordings["progress"].append((done, failed, concurrency)),
            stop.set(),  # 第一条完成后立即取消
        )[0],
        on_finished=lambda status: recordings["finished"].append(status),
    )
    monkeypatch.setattr(translator, "translate_text", slow_translate, raising=True)
    task.run()

    assert recordings["finished"] == ["cancelled"]
    assert recordings["progress"][0][0] == 1
    assert requested["n"] <= 2  # 取消后不派发新请求（≤1 在途收尾）
    assert store.closed


def test_cancel_during_counting(tmp_path, monkeypatch):
    """统计阶段取消：不发任何请求。"""
    rel = write_script(tmp_path, "a.rpy", 5)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    stop = threading.Event()

    def _fail(text, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("不应发起请求")

    monkeypatch.setattr(translator, "translate_text", _fail, raising=True)

    def on_counting(_discovered: int) -> None:
        stop.set()

    task = BatchTask(
        files=[rel],
        game_dir=tmp_path,
        options=make_options(),
        store=store,
        stop_event=stop,
        on_counting=on_counting,
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda d, f, c: recordings["progress"].append((d, f, c)),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    task.run()
    assert recordings["finished"] == ["cancelled"]
    assert recordings["ready"] == []


def test_store_unavailable_finishes_error(tmp_path):
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [], None, make_options(), recordings)
    task.run()
    assert recordings["finished"] == ["error"]


# ------------------------------------------------------------------ 并发档位


def test_dynamic_concurrency_uses_limiter(tmp_path, monkeypatch):
    """成功爬坡在调度循环中生效：并发上限随连成功增长（max=2）。"""
    rel = write_script(tmp_path, "a.rpy", 12)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(
        tmp_path, [rel], store, make_options(max_concurrency=2, initial_concurrency=1,
                                             increase_after=2),
        recordings,
    )
    monkeypatch.setattr(
        translator, "translate_text", lambda text, **kwargs: f"译[{text}]", raising=True
    )
    task.run()

    assert recordings["finished"] == ["completed"]
    concurrency_over_time = [conc for _done, _failed, conc in recordings["progress"]]
    assert concurrency_over_time[0] == 1
    assert max(concurrency_over_time) == 2  # 连成功 2 条后升到 2


def test_worker_returns_failure_on_empty_translation(tmp_path, monkeypatch):
    rel = write_script(tmp_path, "a.rpy", 1)
    store = FakeStore()
    recordings: dict[str, list] = {"counting": [], "ready": [], "progress": [], "finished": []}
    task, _stop = make_task(tmp_path, [rel], store, make_options(), recordings)
    monkeypatch.setattr(
        translator, "translate_text", lambda text, **kwargs: "   ", raising=True
    )
    task.run()
    assert recordings["progress"] == [(0, 1, 1)]
    assert store.put_calls == []


@pytest.mark.parametrize(
    "options",
    [
        make_options(max_concurrency=0),
        make_options(initial_concurrency=99, max_concurrency=4),
    ],
)
def test_options_clamped_by_limiter(options, tmp_path, monkeypatch):
    """并发参数越界由 AdaptiveLimiter 钳制（上限 1..max，下限 1）。"""
    from renpy_overlay.pretranslate.concurrency import AdaptiveLimiter

    limiter = AdaptiveLimiter(
        options.max_concurrency,
        initial_concurrency=options.initial_concurrency,
        increase_after=options.increase_after,
        latency_guard_seconds=options.latency_guard_seconds,
        circuit_break_threshold=options.circuit_break_threshold,
    )
    assert 1 <= limiter.current <= max(1, options.max_concurrency)
