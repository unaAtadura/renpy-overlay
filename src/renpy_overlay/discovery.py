"""目标发现：找出本机正在运行的 Ren'Py 游戏进程。

判定策略是"打分 + 可解释"：命中任何一条特征都会加分，并把理由记进
:attr:`Candidate.reasons`，最终在选择界面里原样展示。这样当识别不准确时，用户能
直接看出是哪条特征误判（例如同名 exe），而不是面对一个黑盒结果。

两条判据的强度差异很大：
- 廉价判据（进程名、exe 同级目录、窗口尺寸）可以无成本地套在所有进程上；
- 强判据（进程里加载了 ``pythonXX.dll``）需要 OpenProcess 并枚举模块，
  所以只对"廉价判据已得分"或"有大窗口"的少量进程执行。
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import win32api
from .pe import ARCH_BY_MACHINE, rank_python_modules
from .win32api import ModuleEntry, RemoteProcess, WindowInfo

logger = logging.getLogger("renpy_overlay.discovery")

RENPY_EXE_NAMES = {"renpy.exe", "renpyw.exe", "renpy"}
PYTHON_EXE_NAMES = {"python.exe", "pythonw.exe", "python3.exe", "python3.9.exe", "python3.10.exe"}
RENPY_DIR_HINTS = ("renpy", "lib", "game")
BIG_WINDOW = (640, 360)
MIN_WINDOW = (200, 120)

SCORE_NAME_RENPY = 4
SCORE_NAME_PYTHON = 3
SCORE_DIR_HINT = 3
SCORE_DIR_HINT_CAP = 6
SCORE_PYTHON_DLL = 5
SCORE_BIG_WINDOW = 2
SCORE_CMDLINE_RENPY = 2


@dataclass
class Candidate:
    """一个候选目标进程。"""

    pid: int
    name: str
    exe: str = ""
    arch: str = "unknown"
    window_title: str = ""
    window_hwnd: int = 0
    score: int = 0
    reasons: tuple[str, ...] = ()
    python_dll: str = ""
    python_modules: tuple[ModuleEntry, ...] = field(default=(), repr=False)
    accessible: bool = False

    @property
    def is_renpy(self) -> bool:
        return bool(self.python_dll)

    def row(self) -> tuple[str, str, str, str, str]:
        return (
            str(self.pid),
            self.name,
            self.arch,
            str(self.score),
            self.window_title or "-",
        )

    def detail(self) -> str:
        lines = [
            f"PID          : {self.pid}",
            f"进程名       : {self.name}",
            f"可执行文件   : {self.exe or '<无法访问>'}",
            f"架构         : {self.arch}",
            f"窗口标题     : {self.window_title or '-'}",
            f"Python 运行时: {self.python_dll or '<未检测到>'}",
            "判定依据     : " + ("；".join(self.reasons) if self.reasons else "无"),
        ]
        return "\n".join(lines)


def arch_from_file(path: str) -> str:
    """直接读本地 PE 文件头判断架构（比 IsWow64Process 更准，且不受调用方位数影响）。"""
    if not path:
        return "unknown"
    try:
        with open(path, "rb") as handle:
            dos = handle.read(0x40)
            if len(dos) < 0x40 or dos[:2] != b"MZ":
                return "unknown"
            e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
            if not 0 < e_lfanew < 0x1000:
                return "unknown"
            handle.seek(e_lfanew)
            nt = handle.read(0x20)
    except OSError:
        return "unknown"
    if len(nt) < 6 or nt[:4] != b"PE\x00\x00":
        return "unknown"
    machine = struct.unpack_from("<H", nt, 4)[0]
    return ARCH_BY_MACHINE.get(machine, f"unknown(0x{machine:04X})")


def _dir_hints(exe: str) -> list[str]:
    """exe 同级（及其父级）目录里出现 Ren'Py 发行版特征目录时给出提示。"""
    if not exe:
        return []
    try:
        root = Path(exe).parent
    except (TypeError, ValueError):
        return []
    found: list[str] = []
    for base in (root, root.parent):
        for hint in RENPY_DIR_HINTS:
            if hint in found:
                continue
            try:
                if (base / hint).is_dir():
                    found.append(hint)
            except OSError:
                continue
    return found


def _process_info(proc: psutil.Process) -> tuple[int, str, str, list[str]] | None:
    try:
        with proc.oneshot():
            pid = proc.pid
            name = proc.name() or ""
            try:
                exe = proc.exe() or ""
            except (psutil.AccessDenied, OSError):
                exe = ""
            try:
                cmdline = proc.cmdline() or []
            except (psutil.AccessDenied, OSError):
                cmdline = []
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    return pid, name, exe, cmdline


def _cheap_score(
    pid: int, name: str, exe: str, cmdline: list[str], windows: dict[int, WindowInfo]
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    lowered = name.lower()

    if lowered in RENPY_EXE_NAMES or "renpy" in lowered:
        score += SCORE_NAME_RENPY
        reasons.append(f"+{SCORE_NAME_RENPY} 进程名匹配 Ren'Py（{name}）")
    elif lowered in PYTHON_EXE_NAMES:
        score += SCORE_NAME_PYTHON
        reasons.append(f"+{SCORE_NAME_PYTHON} 进程名是 Python 解释器（{name}）")

    hints = _dir_hints(exe)
    if hints:
        gained = min(SCORE_DIR_HINT * len(hints), SCORE_DIR_HINT_CAP)
        score += gained
        reasons.append(f"+{gained} exe 同级存在发行版目录：{'/'.join(hints)}")

    window = windows.get(pid)
    if window is not None:
        if window.width >= BIG_WINDOW[0] and window.height >= BIG_WINDOW[1]:
            score += SCORE_BIG_WINDOW
            reasons.append(f"+{SCORE_BIG_WINDOW} 有游戏尺寸窗口（{window.width}x{window.height}）")
    else:
        reasons.append("+0 无可见顶层窗口（可能在标题界面之外或已最小化）")

    joined = " ".join(cmdline).lower()
    if "renpy" in joined or ".rpy" in joined:
        score += SCORE_CMDLINE_RENPY
        reasons.append(f"+{SCORE_CMDLINE_RENPY} 命令行包含 Ren'Py 特征")

    return score, reasons


def _inspect_python_modules(pid: int) -> tuple[str, tuple[ModuleEntry, ...], bool]:
    """尝试打开目标进程并找出其中的 Python 运行库，返回 (dll 名, 全部候选, 是否可访问)。"""
    try:
        with RemoteProcess.open(pid, win32api.QUERY_ACCESS) as process:
            modules = process.modules()
    except OSError as exc:
        logger.debug("pid=%d 无法枚举模块：%s", pid, exc)
        return "", (), False
    candidates = rank_python_modules(modules)
    return (candidates[0].name if candidates else ""), tuple(candidates), True


def _is_own_launcher(name: str) -> bool:
    """识别本工具自身的启动器进程。

    ``uv run renpy-overlay`` 会在 venv 里启动一个 ``renpy-overlay.exe`` 重定向器，
    它也会加载 pythonXX.dll，命中多条特征而排到候选前列，但它显然不是可注入目标。
    """
    return name.lower().startswith(("renpy-overlay", "renpy_overlay"))


def enumerate_candidates(include_all: bool = False, deep: bool = True) -> list[Candidate]:
    """枚举候选进程，按分数从高到低返回。

    本进程自身与工具自己的启动器永远排除在外：它们会加载 pythonXX.dll，
    命中多条特征而排到候选前几名，但那显然不是可注入的目标。
    """
    own_pid = os.getpid()
    windows: dict[int, WindowInfo] = {}
    for window in win32api.list_windows():
        current = windows.get(window.pid)
        if current is None or window.width * window.height > current.width * current.height:
            windows[window.pid] = window

    results: list[Candidate] = []
    for proc in psutil.process_iter(["pid"]):
        info = _process_info(proc)
        if info is None:
            continue
        pid, name, exe, cmdline = info
        if pid == own_pid or _is_own_launcher(name):
            continue
        score, reasons = _cheap_score(pid, name, exe, cmdline, windows)
        window = windows.get(pid)
        needs_deep = deep and (
            score >= SCORE_DIR_HINT
            or (
                window is not None
                and window.width >= BIG_WINDOW[0]
                and window.height >= BIG_WINDOW[1]
            )
        )
        python_dll = ""
        modules: tuple[ModuleEntry, ...] = ()
        accessible = False
        if needs_deep:
            python_dll, modules, accessible = _inspect_python_modules(pid)
            if python_dll:
                score += SCORE_PYTHON_DLL
                reasons.append(f"+{SCORE_PYTHON_DLL} 进程内加载了 {python_dll}（可注入）")
        if not include_all and score <= 0:
            continue
        results.append(
            Candidate(
                pid=pid,
                name=name,
                exe=exe,
                arch=arch_from_file(exe),
                window_title=window.title if window else "",
                window_hwnd=window.hwnd if window else 0,
                score=score,
                reasons=tuple(reasons),
                python_dll=python_dll,
                python_modules=modules,
                accessible=accessible,
            )
        )

    results.sort(key=lambda item: (-item.score, item.pid))
    logger.info(
        "共枚举到 %d 个候选进程（其中 %d 个疑似 Ren'Py）",
        len(results),
        sum(1 for item in results if item.is_renpy),
    )
    return results


def find_candidate(pid: int) -> Candidate | None:
    """为指定的 PID 构造候选对象（跳过打分流程，但保留 Python 运行时探测）。"""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None
    info = _process_info(proc)
    if info is None:
        return None
    _, name, exe, _cmdline = info
    windows = {window.pid: window for window in win32api.list_windows() if window.pid == pid}
    window = None
    for item in windows.values():
        if window is None or item.width * item.height > window.width * window.height:
            window = item
    python_dll, modules, accessible = _inspect_python_modules(pid)
    reasons = []
    if python_dll:
        reasons.append(f"进程内加载了 {python_dll}")
    return Candidate(
        pid=pid,
        name=name,
        exe=exe,
        arch=arch_from_file(exe),
        window_title=window.title if window else "",
        window_hwnd=window.hwnd if window else 0,
        score=SCORE_PYTHON_DLL if python_dll else 0,
        reasons=tuple(reasons),
        python_dll=python_dll,
        python_modules=modules,
        accessible=accessible,
    )


def print_candidates(candidates: list[Candidate]) -> None:
    """在控制台打印候选表（--list 与回退菜单共用）。"""
    if not candidates:
        print("未发现候选进程。请先启动一个 Ren'Py 游戏，或使用 --all 查看全部进程。")
        return
    header = f"{'PID':>8}  {'分数':>4}  {'架构':<7}  {'进程名':<24}  窗口标题"
    print(header)
    print("-" * min(len(header) + 30, 120))
    for item in candidates:
        print(
            f"{item.pid:>8}  {item.score:>4}  {item.arch:<7}  {item.name[:24]:<24}  {item.window_title[:36]}"
        )
