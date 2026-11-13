"""宿主 request_restart 的离线验证（替身 spawn，不真启动进程、不构造窗口）。

用 ``StreamOverlayCore.__new__`` 绕过重量级 ``__init__``，只装配重启链路
所需的三个属性（``_restart_args`` / ``target_pid`` / ``on_quit``），
验证「spawn 成功才触发退出、失败保留会话、缺省回退自组 --pid」三契约。
"""

from __future__ import annotations

from renpy_overlay.stream.host_core import StreamOverlayCore

_SPAWN_TARGET = "renpy_overlay.stream.host_core.spawn_self"


def _bare_core(restart_args=None, target_pid=42, on_quit=None) -> StreamOverlayCore:
    core = StreamOverlayCore.__new__(StreamOverlayCore)
    core._restart_args = restart_args
    core.target_pid = target_pid
    core.on_quit = on_quit
    return core


def test_request_restart_spawns_then_quits(monkeypatch):
    """成功路径：spawn 收到 cli 组装的实参，随后才触发 on_quit 退出链。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        _SPAWN_TARGET, lambda extra: calls.append(list(extra)) or True
    )
    quit_calls: list[int] = []
    core = _bare_core(
        restart_args=["--pid", "42"], on_quit=lambda: quit_calls.append(1)
    )
    assert core.request_restart() is None
    assert calls == [["--pid", "42"]]
    assert quit_calls == [1]


def test_request_restart_failure_returns_error_and_keeps_session(monkeypatch):
    """失败路径：返回错误串供设置窗口提示，不触发退出（当前会话保留）。"""
    monkeypatch.setattr(_SPAWN_TARGET, lambda extra: False)
    quit_calls: list[int] = []
    core = _bare_core(
        restart_args=["--pid", "42"], on_quit=lambda: quit_calls.append(1)
    )
    error = core.request_restart()
    assert error is not None and "启动新实例失败" in error
    assert quit_calls == []


def test_request_restart_falls_back_to_target_pid(monkeypatch):
    """restart_args 缺省（旧入口未传）：回退自组 --pid <target_pid>。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        _SPAWN_TARGET, lambda extra: calls.append(list(extra)) or True
    )
    core = _bare_core(restart_args=None, target_pid=7, on_quit=lambda: None)
    assert core.request_restart() is None
    assert calls == [["--pid", "7"]]


def test_request_restart_without_any_args_returns_error(monkeypatch):
    """无实参可用（跳过注入且未传 restart_args）：报错且 spawn 不被调。"""

    def _unexpected(extra) -> bool:
        raise AssertionError("spawn 不应被调用")

    monkeypatch.setattr(_SPAWN_TARGET, _unexpected)
    core = _bare_core(restart_args=None, target_pid=0, on_quit=None)
    assert core.request_restart() is not None


def test_request_restart_tolerates_quit_callback_exception(monkeypatch):
    """on_quit 抛异常：不向设置窗口传播（重启语义不变，返回 None）。"""
    monkeypatch.setattr(_SPAWN_TARGET, lambda extra: True)

    def _boom() -> None:
        raise RuntimeError("退出回调故障")

    core = _bare_core(restart_args=["--pid", "42"], on_quit=_boom)
    assert core.request_restart() is None
