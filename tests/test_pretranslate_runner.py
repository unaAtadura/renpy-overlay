"""PrebuildTask 流水线的离线验证：判存统计、失败重试、熔断与取消收束。

translator 以 monkeypatch 桩替换，store 用鸭子接口假对象，全程无线程 ——
直接调用 ``task.run()``，断言回调序列与落库行为。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from renpy_overlay import translator
from renpy_overlay.config import AppConfig
from renpy_overlay.pretranslate import runner as runner_module
from renpy_overlay.pretranslate.runner import DEFAULT_CIRCUIT_BREAK, PrebuildTask


class FakeStore:
    """TranslationStore 鸭子接口：记录 get/put/close 调用。"""

    def __init__(self, existing: set[str] | None = None):
        self.existing = set(existing or ())
        self.put_calls: list[tuple[str, str]] = []
        self.closed = False

    def get(self, original: str) -> str | None:
        return "已存" if original in self.existing else None

    def put(self, original: str, translated: str) -> bool:
        self.put_calls.append((original, translated))
        self.existing.add(original)
        return True

    def close(self) -> None:
        self.closed = True


class Recorder:
    """回调收集器：阶段 / 进度 / 终态序列。"""

    def __init__(self):
        self.phases: list[tuple[str, dict]] = []
        self.progress: list[tuple[int, int, int]] = []
        self.finished: list[str] = []

    def on_phase(self, phase, info):
        self.phases.append((phase, dict(info)))

    def on_progress(self, done, failed, skipped):
        self.progress.append((done, failed, skipped))

    def on_finished(self, status):
        self.finished.append(status)


def make_task(files, texts_map, store="default", stop=None, recorder=None):
    recorder = recorder or Recorder()
    # store="default" 哨兵区分「未传」（用 FakeStore）与「显式 None」（验证降级）
    resolved_store = FakeStore() if store == "default" else store
    task = PrebuildTask(
        files,
        game_dir=Path("."),  # 文本来源经 monkeypatch 的 collect_texts 注入
        cfg=AppConfig(),
        store=resolved_store,
        stop_event=stop or threading.Event(),
        on_phase=recorder.on_phase,
        on_progress=recorder.on_progress,
        on_finished=recorder.on_finished,
    )
    return task, recorder


@pytest.fixture()
def patch_collect(monkeypatch):
    """把 collect_texts 替换为固定产物：files → SourceText 列表。"""

    def _patch(by_file: dict):
        from renpy_overlay.pretranslate.parser import SourceText

        def fake_collect(files, game_dir):
            texts = []
            for rel in files:
                for kind, text in by_file.get(rel, []):
                    texts.append(SourceText(text=text, kind=kind, source=rel))
            return texts

        monkeypatch.setattr(runner_module, "collect_texts", fake_collect)

    return _patch


def patch_translate(monkeypatch, fn):
    monkeypatch.setattr(runner_module.translator, "translate_text", fn)


def test_full_success_pipeline(monkeypatch, patch_collect):
    patch_collect({"game/a.rpy": [("say", "Hello"), ("menu", "1. A\n2. B")]})
    calls: list[str] = []

    def fake_translate(text, **options):
        calls.append(text)
        # 运行时一致：API 参数集必须与 AppConfig 字段一一对应
        assert options["base_url"] == AppConfig().api_base_url
        assert options["reasoning_effort"] == AppConfig().reasoning_effort
        assert "base_url_stanby" in options
        return f"译[{text}]"

    patch_translate(monkeypatch, fake_translate)
    task, rec = make_task(["game/a.rpy"], None)
    task.run()

    assert rec.phases == [("counting", {}), ("counting", {"discovered": 2}), ("ready", {"total": 2})]
    assert rec.progress == [(1, 0, 0), (2, 0, 0)]
    assert rec.finished == ["completed"]
    assert task._store.put_calls == [("Hello", "译[Hello]"), ("1. A\n2. B", "译[1. A\n2. B]")]
    assert calls == ["Hello", "1. A\n2. B"]
    assert task._store.closed, "任务结束应自行关闭私有连接"


def test_existing_entries_excluded_from_total(monkeypatch, patch_collect):
    patch_collect({"game/a.rpy": [("say", "Have"), ("say", "Missing")]})
    store = FakeStore(existing={"Have"})
    patch_translate(monkeypatch, lambda text, **_: "译")
    task, rec = make_task(["game/a.rpy"], None, store=store)
    task.run()

    assert rec.phases[-1] == ("ready", {"total": 1})
    assert store.put_calls == [("Missing", "译")]
    assert rec.progress == [(1, 0, 0)]


def test_failure_retries_then_continues(monkeypatch, patch_collect):
    patch_collect({"a": [("say", "one"), ("say", "two")]})
    attempts: list[str] = []

    def flaky(text, **_):
        attempts.append(text)
        if text == "one" and len([a for a in attempts if a == "one"]) <= 2:
            raise translator.TranslationError("暂时失败")
        return "译"

    patch_translate(monkeypatch, flaky)
    task, rec = make_task(["a"], None)
    task.run()

    assert rec.finished == ["completed"]
    assert rec.progress == [(1, 0, 0), (2, 0, 0)]
    assert attempts.count("one") == 3  # 首次 + DEFAULT_RETRIES 次重试
    assert attempts.count("two") == 1
    assert task._store.put_calls == [("one", "译"), ("two", "译")]


def test_permanent_failures_counted_not_fatal(monkeypatch, patch_collect):
    patch_collect({"a": [("say", "one"), ("say", "two")]})

    def always_fail(text, **_):
        raise translator.TranslationError("模型拒绝")

    patch_translate(monkeypatch, always_fail)
    task, rec = make_task(["a"], None)
    task.run()

    assert rec.finished == ["completed"]  # 单条失败不中断
    assert rec.progress == [(0, 1, 0), (0, 2, 0)]
    assert task._store.put_calls == []


def test_circuit_break_on_consecutive_unreachable(monkeypatch, patch_collect):
    texts = [("say", f"t{i}") for i in range(8)]
    patch_collect({"a": texts})

    def unreachable(text, **_):
        raise translator.EndpointUnavailable("无法连接")

    patch_translate(monkeypatch, unreachable)
    task, rec = make_task(["a"], None)
    task.run()

    assert rec.finished == ["circuit_break"]
    # 熔断那条不发进度：最后一条进度停在熔断前累计的失败数
    assert rec.progress[-1][1] == DEFAULT_CIRCUIT_BREAK - 1
    assert task._store.put_calls == []


def test_endpoint_unreachable_reset_by_success(monkeypatch, patch_collect):
    patch_collect({"a": [("say", "a"), ("say", "b"), ("say", "c")]})
    state = {"n": 0}

    def sometimes_unreachable(text, **_):
        state["n"] += 1
        if text == "a":
            raise translator.EndpointUnavailable("down")
        return "译"

    patch_translate(monkeypatch, sometimes_unreachable)
    task, rec = make_task(["a"], None)
    task.run()
    assert rec.finished == ["completed"]  # 成功会重置连续不可达计数


def test_stop_before_run_yields_cancelled(monkeypatch, patch_collect):
    patch_collect({"a": [("say", "x")]})
    translate_calls: list[str] = []
    patch_translate(monkeypatch, lambda text, **_: translate_calls.append(text))
    stop = threading.Event()
    stop.set()
    task, rec = make_task(["a"], None, stop=stop)
    task.run()

    assert rec.finished == ["cancelled"]
    assert translate_calls == []
    assert rec.progress == []


def test_stop_during_translation(monkeypatch, patch_collect):
    patch_collect({"a": [("say", "one"), ("say", "two"), ("say", "three")]})
    stop = threading.Event()

    def translate_and_stop(text, **_):
        if text == "one":
            stop.set()  # 模拟用户在第一条之后取消
        return "译"

    patch_translate(monkeypatch, translate_and_stop)
    task, rec = make_task(["a"], None, stop=stop)
    task.run()

    assert rec.finished == ["cancelled"]
    assert task._store.put_calls == [("one", "译")]


def test_none_store_yields_error():
    task, rec = make_task(["a"], None, store=None)
    task.run()
    assert rec.finished == ["error"]


def test_on_finished_emitted_once(monkeypatch, patch_collect):
    """取消后 run 不重复发终态（统计/翻译两阶段的 _finish 幂等）。"""
    patch_collect({"a": [("say", "x")]})
    stop = threading.Event()
    stop.set()
    task, rec = make_task(["a"], None, stop=stop)
    task.run()
    task._finish("completed")  # 手动再发一次也应被幂等吞掉
    assert rec.finished.count("cancelled") == 1
