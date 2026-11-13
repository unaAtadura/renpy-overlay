"""cli.Session 的 say 暂存补推与 hooks 注入确认竞态的离线验证。

不构造 Qt、不建网、不注入。覆盖两类「悬浮窗就绪前到达的信号被静默丢弃」
缺陷的修复：say 只暂存最新一条、就绪后由 _flush_pending_say 补推；hooks
确认置 _hooks_confirmed、就绪后由 _flush_injected 补发 mark_injected
（绑定窗口显示）；就绪后的消息直接推送不入暂存。
"""

from __future__ import annotations

from types import SimpleNamespace

from renpy_overlay import cli
from renpy_overlay.cli import Session
from renpy_overlay.discovery import Candidate


def _make_session(idle_exit: float = 2.0) -> Session:
    args = SimpleNamespace(timeout=15.0, port=0, no_overlay=False, idle_exit=idle_exit)
    return Session(args, Candidate(pid=0, name="stub"))


class _FakeOverlay:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str, str, object]] = []
        self.statuses: list[str] = []
        self.hints: list[str] = []
        self.injected_calls = 0
        self.choices: list[dict] = []
        self.request_close_calls = 0

    def request_close(self) -> None:
        self.request_close_calls += 1

    def push_say(self, who, what, source="", ts=None) -> None:
        self.pushed.append((who, what, source, ts))

    def push_choice(self, items, who="", what="", effects=None) -> None:
        self.choices.append({"items": items, "who": who, "what": what, "effects": effects})

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


def test_choice_effects_passthrough():
    """choice 消息的 effects 字段原样透传给悬浮窗（副作用日志旁路数据）。"""
    session = _make_session()
    overlay = _FakeOverlay()
    session.overlay = overlay
    session._on_message(
        {
            "t": "choice",
            "items": ["A", "B"],
            "who": "",
            "what": "",
            "effects": [["$ a += 1"], []],
        },
        None,
    )
    assert overlay.choices == [
        {"items": ["A", "B"], "who": "", "what": "", "effects": [["$ a += 1"], []]}
    ]


def test_choice_effects_missing_or_invalid_degrades_to_none():
    """无 effects 字段（旧版 agent）或非列表形态：降级为 None，不抛异常。"""
    session = _make_session()
    overlay = _FakeOverlay()
    session.overlay = overlay
    session._on_message({"t": "choice", "items": ["A"], "who": "", "what": ""}, None)
    session._on_message(
        {"t": "choice", "items": ["A"], "who": "", "what": "", "effects": "bad"}, None
    )
    assert [choice["effects"] for choice in overlay.choices] == [None, None]


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


# ---- 退出时机（idle_exit 语义与 _request_stop 幂等） ------------------------


class _FakeStopEvent:
    """threading.Event 替身：wait 按脚本逐次返回，避免真实轮询等待。"""

    def __init__(self, script: list[bool]) -> None:
        self._script = list(script)
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout=None) -> bool:
        if self._script:
            return self._script.pop(0)
        return self._set


def _monitor_session(idle_exit: float) -> Session:
    session = _make_session(idle_exit=idle_exit)
    session.overlay = _FakeOverlay()
    return session


def test_monitor_loop_zero_idle_exit_stops_immediately(monkeypatch):
    """idle_exit=0：检测到进程退出的当下即 _request_stop，不再多等一轮轮询。"""
    monkeypatch.setattr(cli.psutil, "pid_exists", lambda pid: False)
    session = _monitor_session(idle_exit=0.0)
    session._stop = _FakeStopEvent([False])  # 首次进入循环体即命中退出路径
    session._monitor_loop()
    assert session._stop.is_set()
    assert session.overlay.request_close_calls == 1


def test_monitor_loop_positive_idle_exit_waits_then_stops(monkeypatch):
    """idle_exit>0：保留「发现 → 计时 → 等满 idle_exit 再退」的原路径。"""
    monkeypatch.setattr(cli.psutil, "pid_exists", lambda pid: False)
    clock = iter([1000.0, 1002.5])  # 发现时刻与判定时刻，间隔 2.5s >= idle_exit
    monkeypatch.setattr(cli.time, "time", lambda: next(clock))
    session = _monitor_session(idle_exit=2.0)
    session._stop = _FakeStopEvent([False, False])
    session._monitor_loop()
    assert session._stop.is_set()
    assert session.overlay.request_close_calls == 1


def test_monitor_loop_positive_idle_exit_not_due_keeps_waiting(monkeypatch):
    """idle_exit>0：等待未满时不退出（证明 0 的「立即」不是轮询周期的巧合）。"""
    monkeypatch.setattr(cli.psutil, "pid_exists", lambda pid: False)
    clock = iter([1000.0, 1001.0])  # 间隔 1.0s < idle_exit=2.0
    monkeypatch.setattr(cli.time, "time", lambda: next(clock))
    session = _monitor_session(idle_exit=2.0)
    session._stop = _FakeStopEvent([False, True])  # 第二次 wait 置位结束循环
    session._monitor_loop()
    assert not session._stop.is_set()


def test_request_stop_is_idempotent():
    """重复 _request_stop 不重复通知界面（幂等），任意时刻可安全触发。"""
    session = _monitor_session(idle_exit=2.0)
    session._request_stop()
    session._request_stop()
    session._request_stop()
    assert session._stop.is_set()
    assert session.overlay.request_close_calls == 1


# ---- 重启实参组装（--data-dir 直通与 overlay restart_args） -------------------


def test_build_parser_accepts_data_dir():
    """--data-dir 可解析（重启透传跳过注入模式的入口参数）。"""
    args = cli.build_parser().parse_args(["--data-dir", "D:/game"])
    assert args.data_dir == "D:/game"


def test_main_data_dir_routes_to_skip_injection(monkeypatch):
    """--data-dir 且无 --pid/--unload/--status：直通跳过注入模式。"""
    captured: dict = {}

    def _fake_skip(args, selected_dir):
        captured["selected_dir"] = selected_dir
        return 7

    monkeypatch.setattr(cli, "_run_skip_injection_mode", _fake_skip)
    monkeypatch.setattr(cli.logs, "force_utf8_stdio", lambda: None)
    monkeypatch.setattr(cli.logs, "setup_logging", lambda **_kw: None)
    monkeypatch.setattr(cli.win32api, "enable_dpi_awareness", lambda: None)
    monkeypatch.setattr(cli.win32api, "IS_WINDOWS", True)

    assert cli.main(["--data-dir", "D:/game"]) == 7
    assert captured["selected_dir"] == "D:/game"


def test_run_overlay_mode_passes_restart_args(monkeypatch):
    """悬浮窗构造收到重启实参 --pid（cli 层持有目标 PID，宿主只透传）。"""
    captured: dict = {}

    class _StubWindow:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def set_status(self, _text) -> None:
            pass

        def hint(self, _text) -> None:
            pass

        def run(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "StreamOverlayWindow", _StubWindow)
    monkeypatch.setattr(cli.psutil, "pid_exists", lambda _pid: False)
    session = _make_session()
    session.args.dock = "top-center"
    session.target = Candidate(pid=1234, name="game")
    session.link = SimpleNamespace(stop=lambda: None)
    session.injector = SimpleNamespace(close=lambda: None)

    assert session._run_overlay_mode() == cli.EXIT_OK
    assert captured["restart_args"] == ["--pid", "1234"]
