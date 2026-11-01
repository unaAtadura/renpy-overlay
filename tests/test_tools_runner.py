"""工具任务编排验证：回调序列、失败继续、跳过计数、取消与异常收敛。

monkeypatch 替换 ``external_tools.decompile_rpyc`` / ``extract_rpa``，用真实
tmp_path 目录结构同时验证路径映射与回调行为（同步调用 ``task.run()``）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from renpy_overlay import external_tools as et
from renpy_overlay.external_tools import ToolResult
from renpy_overlay.pretranslate.overlay_paths import unrpa_root
from renpy_overlay.tools_runner import DecompileTask, ExtractTask


def make_recordings() -> dict[str, list]:
    return {"ready": [], "progress": [], "finished": []}


def make_decompile_task(
    tmp_path,
    files,
    recordings,
    stop,
    monkeypatch,
    scan_root=None,
    behavior=None,
):
    """构造反编译任务：patch 掉 external_tools.decompile_rpyc（行为可注入）。"""
    calls: list[tuple[Path, Path]] = []

    def fake_decompile(exe, src, dst, *, timeout=None, stop_event=None):
        calls.append((src, dst))
        return behavior(src, dst) if behavior else ToolResult(returncode=0)

    monkeypatch.setattr(et, "decompile_rpyc", fake_decompile)
    task = DecompileTask(
        files, tmp_path, scan_root or (tmp_path / "game"), Path("unrpyc.exe"),
        stop_event=stop,
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda done, failed, skipped: recordings["progress"].append(
            (done, failed, skipped)
        ),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    return task, calls


# ------------------------------------------------------------------ 反编译任务


def test_decompile_completed_callbacks_and_mapping(tmp_path, monkeypatch):
    """回调序列（ready 一次 → progress N 次 → finished 一次）与 game→unrpa 映射。"""
    game = tmp_path / "game"
    game.mkdir()
    recordings = make_recordings()
    task, calls = make_decompile_task(
        tmp_path, ["a.rpyc", "sub/b.rpyc"], recordings, threading.Event(),
        monkeypatch, scan_root=game,
    )
    task.run()

    assert recordings["finished"] == ["completed"]  # 终态只发一次
    assert recordings["ready"] == [2]
    assert recordings["progress"] == [(1, 0, 0), (2, 0, 0)]
    unrpa = unrpa_root(tmp_path)
    assert calls == [
        (game / "a.rpyc", unrpa / "a.rpy"),
        (game / "sub" / "b.rpyc", unrpa / "sub" / "b.rpy"),
    ]


def test_decompile_skipped_counted_separately(tmp_path, monkeypatch):
    """目标已存在的跳过计入第三参，不算成功也不算失败。"""
    recordings = make_recordings()
    state = {"n": 0}

    def behavior(src, dst):
        state["n"] += 1
        return ToolResult(skipped=True) if state["n"] == 1 else ToolResult(returncode=0)

    task, _calls = make_decompile_task(
        tmp_path, ["a.rpyc", "b.rpyc"], recordings, threading.Event(),
        monkeypatch, behavior=behavior,
    )
    task.run()

    assert recordings["progress"] == [(1, 0, 1), (2, 0, 1)]
    assert recordings["finished"] == ["completed"]


def test_decompile_failure_continues_and_completes(tmp_path, monkeypatch):
    """单文件失败计数后继续，终态仍 completed。"""
    recordings = make_recordings()
    state = {"n": 0}

    def behavior(src, dst):
        state["n"] += 1
        return ToolResult(returncode=2) if state["n"] == 1 else ToolResult(returncode=0)

    task, _calls = make_decompile_task(
        tmp_path, ["bad.rpyc", "good.rpyc"], recordings, threading.Event(),
        monkeypatch, behavior=behavior,
    )
    task.run()

    assert recordings["progress"] == [(1, 1, 0), (2, 1, 0)]  # 首参=已处理数
    assert recordings["finished"] == ["completed"]


def test_decompile_worker_exception_counts_failure_and_continues(tmp_path, monkeypatch):
    """单文件编排异常同样计失败继续。"""
    recordings = make_recordings()
    state = {"n": 0}

    def behavior(src, dst):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("复制失败")
        return ToolResult(returncode=0)

    task, _calls = make_decompile_task(
        tmp_path, ["x.rpyc", "y.rpyc"], recordings, threading.Event(),
        monkeypatch, behavior=behavior,
    )
    task.run()

    assert recordings["progress"] == [(1, 1, 0), (2, 1, 0)]  # 首参=已处理数
    assert recordings["finished"] == ["completed"]


def test_stop_before_run_yields_cancelled(tmp_path, monkeypatch):
    """停止置位后不处理任何文件，终态 cancelled。"""
    recordings = make_recordings()
    stop = threading.Event()
    stop.set()

    def behavior(src, dst):  # pragma: no cover - 不应被调用
        raise AssertionError("取消后不应调用工具")

    task, _calls = make_decompile_task(
        tmp_path, ["a.rpyc"], recordings, stop, monkeypatch, behavior=behavior,
    )
    task.run()

    assert recordings["ready"] == [1]
    assert recordings["progress"] == []
    assert recordings["finished"] == ["cancelled"]


def test_empty_files_finishes_completed(tmp_path, monkeypatch):
    recordings = make_recordings()
    task, _calls = make_decompile_task(tmp_path, [], recordings, threading.Event(), monkeypatch)
    task.run()
    assert recordings["ready"] == [0]
    assert recordings["progress"] == []
    assert recordings["finished"] == ["completed"]


def test_orchestration_exception_yields_error(tmp_path):
    """编排层自身异常（回调抛错）收敛为 error 终态（只发一次）。"""
    recordings = make_recordings()
    stop = threading.Event()

    def bad_ready(_total: int) -> None:
        raise RuntimeError("回调爆炸")

    task = DecompileTask(
        ["a.rpyc"], tmp_path, tmp_path / "game", Path("unrpyc.exe"),
        stop_event=stop,
        on_ready=bad_ready,
        on_progress=lambda d, f, s: recordings["progress"].append((d, f, s)),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    task.run()

    assert recordings["finished"] == ["error"]


# ------------------------------------------------------------------ 解包任务


def test_extract_mapping_and_callbacks(tmp_path, monkeypatch):
    """归档路径取 game 下相对路径；输出目录按相对目录映射进 unrpa。"""
    recordings = make_recordings()
    stop = threading.Event()
    game = tmp_path / "game"
    game.mkdir()
    calls: list[tuple[Path, Path]] = []

    def fake_extract(exe, archive, out_dir, *, timeout=None, stop_event=None):
        calls.append((archive, out_dir))
        return ToolResult(returncode=0)

    monkeypatch.setattr(et, "extract_rpa", fake_extract)
    task = ExtractTask(
        ["achieve.rpa", "folder/ep1_0.rpa"], tmp_path, Path("rpaextract.exe"),
        stop_event=stop,
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda done, failed, skipped: recordings["progress"].append(
            (done, failed, skipped)
        ),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    task.run()

    unrpa = unrpa_root(tmp_path)
    assert calls == [
        (game / "achieve.rpa", unrpa / "achieve"),
        (game / "folder" / "ep1_0.rpa", unrpa / "folder" / "ep1_0"),
    ]
    assert recordings["progress"] == [(1, 0, 0), (2, 0, 0)]
    assert recordings["finished"] == ["completed"]


def test_extract_different_archives_isolated_dirs(tmp_path, monkeypatch):
    """不同归档映射到各自专属目录，第二个不再因第一个而跳过。"""
    recordings = make_recordings()
    stop = threading.Event()
    game = tmp_path / "game"
    game.mkdir()
    out_dirs: list[Path] = []

    def fake_extract(exe, archive, out_dir, *, timeout=None, stop_event=None):
        out_dirs.append(out_dir)
        return ToolResult(returncode=0)

    monkeypatch.setattr(et, "extract_rpa", fake_extract)
    task = ExtractTask(
        ["ep1.rpa", "ep2.rpa"], tmp_path, Path("rpaextract.exe"),
        stop_event=stop,
        on_ready=lambda total: recordings["ready"].append(total),
        on_progress=lambda done, failed, skipped: recordings["progress"].append(
            (done, failed, skipped)
        ),
        on_finished=lambda status: recordings["finished"].append(status),
    )
    task.run()

    unrpa = unrpa_root(tmp_path)
    assert out_dirs == [unrpa / "ep1", unrpa / "ep2"]
    assert recordings["progress"] == [(1, 0, 0), (2, 0, 0)]  # 无跳过：两档都执行
    assert recordings["finished"] == ["completed"]
