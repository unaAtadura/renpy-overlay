"""外部工具定位与子进程调用验证。

``run_tool`` 用 ``sys.executable -c`` 桩脚本做真实子进程验证；``decompile_rpyc``
/ ``extract_rpa`` 的上层语义经 monkeypatch 替换 ``run_tool`` 验证（复制 / 清理 /
跳过 / 前缀剥离序列），不依赖真实 exe。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from renpy_overlay import external_tools as et
from renpy_overlay.external_tools import (
    ToolResult,
    decompile_rpyc,
    extract_rpa,
    plugin_dir,
    run_tool,
)

PY = Path(sys.executable)


# ------------------------------------------------------------------ run_tool（真实子进程）


def test_run_tool_success_captures_stdout():
    result = run_tool(PY, ["-c", "print('ok')"])
    assert result.ok
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_run_tool_nonzero_returncode():
    result = run_tool(PY, ["-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.returncode == 3


def test_run_tool_timeout_kills_process():
    result = run_tool(PY, ["-c", "import time; time.sleep(30)"], timeout=0.3)
    assert result.timed_out
    assert not result.ok


def test_run_tool_stop_event_cancels():
    stop = threading.Event()
    stop.set()
    result = run_tool(PY, ["-c", "import time; time.sleep(30)"], timeout=30, stop_event=stop)
    assert result.cancelled
    assert not result.ok


def test_run_tool_path_with_spaces_and_chinese(tmp_path):
    work = tmp_path / "中文 目录"
    work.mkdir()
    data = work / "数 据.txt"
    data.write_text("内容", encoding="utf-8")
    result = run_tool(
        PY,
        [
            "-c",
            "import sys; print(open(sys.argv[1], encoding='utf-8').read())",
            str(data),
        ],
    )
    assert result.ok
    assert "内容" in result.stdout


def test_run_tool_start_failure_returns_unfinished(tmp_path):
    result = run_tool(tmp_path / "no_such_tool.exe", [])
    assert not result.ok
    assert not result.cancelled and not result.timed_out and not result.skipped


# ------------------------------------------------------------------ 工具定位


def test_plugin_dir_source_mode():
    expected = Path(et.__file__).resolve().parent / "assets" / "plugin"
    assert plugin_dir() == expected


def test_plugin_dir_frozen_mode(tmp_path, monkeypatch):
    """打包环境：assets 经 spec datas 收集到 _MEIPASS 下带包前缀的路径。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert plugin_dir() == tmp_path / "renpy_overlay" / "assets" / "plugin"


def test_plugin_dir_frozen_without_meipass_falls_back_to_exe_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app.exe"), raising=False)
    assert plugin_dir() == tmp_path / "renpy_overlay" / "assets" / "plugin"


def test_exe_locators_missing_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(FileNotFoundError):
        et.unrpyc_exe()
    with pytest.raises(FileNotFoundError):
        et.rpaextract_exe()


# ------------------------------------------------------------------ 反编译语义


@pytest.fixture()
def record_run(monkeypatch):
    """桩 run_tool：记录调用并在输入同目录产出同名 .rpy。"""
    calls: list[list[Path]] = []

    def fake(exe, args, *, timeout, stop_event):
        calls.append([Path(arg) for arg in args])
        source = Path(args[0])
        source.with_suffix(".rpy").write_text("decompiled", encoding="utf-8")
        return ToolResult(returncode=0)

    monkeypatch.setattr(et, "run_tool", fake)
    return calls


def test_decompile_cross_dir_copies_and_cleans(tmp_path, record_run):
    src = tmp_path / "game" / "ep2.rpyc"
    src.parent.mkdir()
    src.write_bytes(b"compiled")
    dst = tmp_path / "renpy_overlay_cache" / "unrpa" / "ep2.rpy"

    result = decompile_rpyc(Path("unrpyc.exe"), src, dst)

    assert result.ok
    assert dst.read_text(encoding="utf-8") == "decompiled"
    (called_input,) = record_run[0]
    assert called_input.parent == dst.parent  # 在目标目录内就地反编译
    assert called_input != src
    assert not called_input.exists()  # 临时 .rpyc 副本已清理
    assert src.exists()  # 源文件不受影响


def test_decompile_same_dir_runs_inplace(tmp_path, record_run):
    src = tmp_path / "x.rpyc"
    src.write_bytes(b"compiled")
    result = decompile_rpyc(Path("unrpyc.exe"), src, tmp_path / "x.rpy")
    assert result.ok
    assert record_run[0] == [src]  # 同目录：直接对源文件调用，无副本


def test_decompile_skips_existing_target(tmp_path, record_run):
    src = tmp_path / "x.rpyc"
    src.write_bytes(b"compiled")
    dst = tmp_path / "x.rpy"
    dst.write_text("已有", encoding="utf-8")

    result = decompile_rpyc(Path("unrpyc.exe"), src, dst)

    assert result.skipped
    assert record_run == []  # 未启动工具
    assert dst.read_text(encoding="utf-8") == "已有"  # 目标不被覆盖


def test_decompile_tool_failure_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        et, "run_tool", lambda *args, **kwargs: ToolResult(returncode=2, stderr="boom")
    )
    src = tmp_path / "x.rpyc"
    src.write_bytes(b"compiled")
    result = decompile_rpyc(Path("unrpyc.exe"), src, tmp_path / "x.rpy")
    assert not result.ok
    assert result.returncode == 2


def test_decompile_missing_output_counts_failure(tmp_path, monkeypatch):
    """返回码 0 但未产出 .rpy：按失败处理。"""
    monkeypatch.setattr(et, "run_tool", lambda *args, **kwargs: ToolResult(returncode=0))
    src = tmp_path / "x.rpyc"
    src.write_bytes(b"compiled")
    result = decompile_rpyc(Path("unrpyc.exe"), src, tmp_path / "x.rpy")
    assert not result.ok


# ------------------------------------------------------------------ 解包语义


def make_extract_stub(monkeypatch, layout: dict[str, bytes]) -> list[Path]:
    """桩 run_tool：按 ``-o`` 参数在输出目录生成归档内容，记录归档路径。"""
    calls: list[Path] = []

    def fake(exe, args, *, timeout, stop_event):
        assert args[0] == "-x" and args[1] == "-f" and args[3] == "-o"
        archive = Path(args[2])
        out_dir = Path(args[4])
        calls.append(archive)
        for rel, payload in layout.items():
            target = out_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return ToolResult(returncode=0)

    monkeypatch.setattr(et, "run_tool", fake)
    return calls


def test_extract_runs_tool_and_creates_dir(tmp_path, monkeypatch):
    calls = make_extract_stub(monkeypatch, {"images/a.png": b"png"})
    archive = tmp_path / "game" / "achieve.rpa"
    archive.parent.mkdir()
    archive.write_bytes(b"RPA")
    out = tmp_path / "renpy_overlay_cache" / "unrpa"

    result = extract_rpa(Path("rpaextract.exe"), archive, out)

    assert result.ok
    assert calls == [archive]
    assert (out / "images" / "a.png").read_bytes() == b"png"


def test_extract_skips_when_out_dir_non_empty(tmp_path, monkeypatch):
    calls = make_extract_stub(monkeypatch, {})
    archive = tmp_path / "a.rpa"
    archive.write_bytes(b"RPA")
    out = tmp_path / "renpy_overlay_cache" / "unrpa"
    (out / "images").mkdir(parents=True)
    (out / "images" / "x.png").write_bytes(b"png")

    result = extract_rpa(Path("rpaextract.exe"), archive, out)

    assert result.skipped
    assert calls == []  # 整档跳过，未启动工具


def test_extract_empty_out_dir_is_extracted(tmp_path, monkeypatch):
    calls = make_extract_stub(monkeypatch, {"a.txt": b"x"})
    archive = tmp_path / "a.rpa"
    archive.write_bytes(b"RPA")
    out = tmp_path / "renpy_overlay_cache" / "unrpa"
    out.mkdir(parents=True)  # 存在但为空：不跳过

    result = extract_rpa(Path("rpaextract.exe"), archive, out)

    assert result.ok
    assert calls == [archive]


def test_same_archive_rerun_skips_in_own_dir(tmp_path, monkeypatch):
    """输出目录为归档专属目录，重复解包同一归档 → 整档跳过（幂等）。"""
    calls = make_extract_stub(monkeypatch, {"script/a.rpy": b"s"})
    archive = tmp_path / "game" / "ep1.rpa"
    archive.parent.mkdir()
    archive.write_bytes(b"RPA")
    out = tmp_path / "renpy_overlay_cache" / "unrpa" / "ep1"

    first = extract_rpa(Path("rpaextract.exe"), archive, out)
    second = extract_rpa(Path("rpaextract.exe"), archive, out)

    assert first.ok
    assert second.skipped
    assert calls == [archive]  # 第二次未启动工具
    assert (out / "script" / "a.rpy").read_bytes() == b"s"


def test_extract_strips_game_prefix(tmp_path, monkeypatch):
    """归档条目自带 game/ 前缀 → 上移到输出目录根。"""
    make_extract_stub(monkeypatch, {"game/script/a.rpyc": b"c", "game/tl/x.rpy": b"t"})
    archive = tmp_path / "a.rpa"
    archive.write_bytes(b"RPA")
    out = tmp_path / "renpy_overlay_cache" / "unrpa"

    result = extract_rpa(Path("rpaextract.exe"), archive, out)

    assert result.ok
    assert (out / "script" / "a.rpyc").read_bytes() == b"c"
    assert (out / "tl" / "x.rpy").read_bytes() == b"t"
    assert not (out / "game").exists()  # 空的 game/ 已删除


def test_extract_game_prefix_conflict_counts_failure(tmp_path, monkeypatch):
    """上移同名冲突：归档同时含 a.txt 与 game/a.txt → 保留前者，本档计失败。"""
    make_extract_stub(monkeypatch, {"a.txt": b"old", "game/a.txt": b"new"})
    archive = tmp_path / "a.rpa"
    archive.write_bytes(b"RPA")
    out = tmp_path / "renpy_overlay_cache" / "unrpa"

    result = extract_rpa(Path("rpaextract.exe"), archive, out)

    assert not result.ok
    assert (out / "a.txt").read_bytes() == b"old"  # 冲突保留既有条目
    assert (out / "game" / "a.txt").read_bytes() == b"new"  # 残留供人工检查
