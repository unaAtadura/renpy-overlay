"""restart 模块离线测试：命令行重建与分离进程拉起（全替身，不真启动进程）。

- ``build_spawn_args``：钉住 frozen / 源码两种形态与 extra 追加位置；
- ``spawn_self``：monkeypatch ``subprocess.Popen`` 断言调用参数与失败吞并。
"""

from __future__ import annotations

import subprocess
import sys

from renpy_overlay import restart


def test_build_spawn_args_source_mode(monkeypatch):
    """源码态：python -m renpy_overlay + 原 argv + extra 追加在末尾。"""
    monkeypatch.delattr(sys, "frozen", raising=False)  # 源码态无 frozen 属性
    monkeypatch.setattr(sys, "executable", r"C:\venv\python.exe")
    monkeypatch.setattr(sys, "argv", ["renpy-overlay", "--dock", "top-left"])

    command = restart.build_spawn_args(["--pid", "42"])

    assert command == [
        r"C:\venv\python.exe",
        "-m",
        "renpy_overlay",
        "--dock",
        "top-left",
        "--pid",
        "42",
    ]


def test_build_spawn_args_frozen_mode(monkeypatch):
    """frozen 态（PyInstaller）：exe 即入口，不再追加 -m。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # 源码态本无该属性
    monkeypatch.setattr(sys, "executable", r"C:\app\renpy-overlay.exe")
    monkeypatch.setattr(sys, "argv", [r"C:\app\renpy-overlay.exe", "--port", "9000"])

    command = restart.build_spawn_args(["--data-dir", r"D:\game"])

    assert command == [
        r"C:\app\renpy-overlay.exe",
        "--port",
        "9000",
        "--data-dir",
        r"D:\game",
    ]


def test_build_spawn_args_extra_overrides_repeated_keys(monkeypatch):
    """extra 追加末尾：同键重复时 argparse 取后者（覆盖语义在此，不在本函数）。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "python.exe")
    monkeypatch.setattr(sys, "argv", ["renpy-overlay", "--pid", "1"])

    command = restart.build_spawn_args(["--pid", "2"])

    assert command.count("--pid") == 2
    assert command[-1] == "2"  # 后者生效：新 pid 位于末尾


def test_spawn_self_invokes_popen_detached(monkeypatch):
    """成功路径：Popen 收到分离标志与重建命令行，返回 True。"""
    calls: list[dict] = []

    class _FakePopen:
        def __init__(self, command, **kwargs):
            calls.append({"command": command, **kwargs})

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "python.exe")
    monkeypatch.setattr(sys, "argv", ["renpy-overlay"])
    monkeypatch.setattr("subprocess.Popen", _FakePopen)

    assert restart.spawn_self(["--pid", "7"]) is True
    assert len(calls) == 1
    assert calls[0]["command"] == ["python.exe", "-m", "renpy_overlay", "--pid", "7"]
    assert calls[0]["close_fds"] is True
    if subprocess.CREATE_NEW_PROCESS_GROUP:  # Windows 平台断言分离标志
        assert calls[0]["creationflags"] == (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )


def test_spawn_self_swallows_failure(monkeypatch):
    """失败路径：Popen 抛异常被吞并，返回 False 不向调用方抛出。"""

    def _boom(*_args, **_kwargs):
        raise OSError("exe missing")

    monkeypatch.setattr("subprocess.Popen", _boom)

    assert restart.spawn_self(["--pid", "7"]) is False
