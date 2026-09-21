"""cli.Session 的 say 暂存补推与 hooks 注入确认竞态的离线验证。

不构造 Qt、不建网、不注入。覆盖两类「悬浮窗就绪前到达的信号被静默丢弃」
缺陷的修复：say 只暂存最新一条、就绪后由 _flush_pending_say 补推；hooks
确认置 _hooks_confirmed、就绪后由 _flush_injected 补发 mark_injected
（绑定窗口显示）；就绪后的消息直接推送不入暂存。
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
        self.statuses: list[str] = []
        self.hints: list[str] = []
        self.injected_calls = 0

    def push_say(self, who, what, source="", ts=None) -> None:
        self.pushed.append((who, what, source, ts))

    def set_status(self, text: str) -> None:
        self.statuses.append(text)

    def hint(self, text: str) -> None:
        self.hints.append(text)

    def mark_injected(self) -> None:
        self.injected_calls += 1


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


# ---- hooks 注入确认竞态（绑定窗口显示前提） ---------------------------------


def _hooks(layers=None) -> dict:
    return {"t": "hooks", "layers": layers or ["all_character_callbacks", "exports_menu"]}


def test_hooks_before_overlay_records_confirmation_only():
    """hooks 早于悬浮窗（实测日志 18.920 vs 19.448）：只记录确认，不崩溃。"""
    session = _make_session()
    assert session._hooks_confirmed is False
    session._on_message(_hooks(), None)  # overlay=None：旧行为是整块静默丢弃
    assert session._hooks_confirmed is True


def test_flush_injected_replays_after_overlay_ready():
    """就绪后补发 mark_injected：绑定窗口得以显示（本次缺陷的修复点）。"""
    session = _make_session()
    session._on_message(_hooks(), None)  # 窗口未就绪：确认被暂存
    overlay = _FakeOverlay()
    session.overlay = overlay
    session._flush_injected()
    assert overlay.injected_calls == 1
    session._flush_injected()  # 幂等：重复补发无额外副作用
    assert overlay.injected_calls == 1


def test_hooks_after_overlay_direct_and_unconfirmed_no_replay():
    """消息晚到场景（重连）直接分发；未确认时不补发（跳过注入等价安全）。"""
    session = _make_session()
    overlay = _FakeOverlay()
    session.overlay = overlay
    session._on_message(_hooks(), None)  # 就绪后到达：直接分发
    assert session._hooks_confirmed is True
    assert overlay.injected_calls == 1
    assert overlay.statuses and "Hook 2 层" in overlay.statuses[-1]

    session2 = _make_session()
    overlay2 = _FakeOverlay()
    session2.overlay = overlay2
    session2._flush_injected()  # 从未确认：不得补发（跳过注入模式同口径）
    assert overlay2.injected_calls == 0
