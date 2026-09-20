"""cli.Session 的 say 暂存补推离线验证（不构造 Qt、不建网、不注入）。

覆盖「悬浮窗就绪前到达的 say 被静默丢弃」缺陷的修复：就绪前只暂存最新一条、
就绪后由 _flush_pending_say 补推（保留原 ts），就绪后的消息直接推送不入暂存。
"""

from __future__ import annotations

from types import SimpleNamespace

from renpy_overlay.cli import Session
from renpy_overlay.discovery import Candidate


def _make_session() -> Session:
    args = SimpleNamespace(timeout=15.0, port=0, no_overlay=False)
    return Session(args, Candidate(pid=0, name="stub"))


class _FakeOverlay:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str, str, object]] = []

    def push_say(self, who, what, source="", ts=None) -> None:
        self.pushed.append((who, what, source, ts))


def _say(what: str, ts: float = 100.0) -> dict:
    return {"t": "say", "who": "Charles", "what": what, "src": "poll", "ts": ts}


def test_say_before_overlay_is_buffered_latest_only():
    session = _make_session()
    session._on_message(_say("第一句", ts=100.0), None)
    session._on_message(_say("第二句", ts=101.0), None)
    assert session._say_count == 2
    assert session._pending_say is not None
    assert session._pending_say["what"] == "第二句"  # 只保留最新一条
    assert session._pending_say["ts"] == 101.0


def test_flush_pending_say_pushes_after_overlay_ready():
    session = _make_session()
    session._on_message(_say("Hello.", ts=123.0), None)
    overlay = _FakeOverlay()
    session.overlay = overlay
    session._flush_pending_say()
    assert session._pending_say is None  # 补推后清空
    assert overlay.pushed == [("Charles", "Hello.", "poll", 123.0)]  # 原 ts 保留
    session._flush_pending_say()  # 幂等：无暂存时无动作
    assert len(overlay.pushed) == 1


def test_say_after_overlay_ready_pushes_directly():
    session = _make_session()
    overlay = _FakeOverlay()
    session.overlay = overlay
    session._on_message(_say("Live."), None)
    # 就绪后的消息直接推送（时间戳由 push_say 内部取当前时刻，无需透传 ts）
    assert overlay.pushed == [("Charles", "Live.", "poll", None)]
    assert session._pending_say is None  # 就绪后不进入暂存
