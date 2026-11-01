"""外部工具定位与子进程调用：rpaextract.exe（.rpa 解包）与 unrpyc.exe（.rpyc 反编译）。

工具随包分发于 ``assets/plugin/``：源码运行取包内目录，PyInstaller 打包
（onedir）运行取 ``sys._MEIPASS`` 解包目录。子进程经 list 形式 argv 调用
（不经 shell，天然兼容含空格 / 中文路径）；stdout/stderr 按行落 DEBUG 日志；
轮询循环响应 stop_event（先 terminate、宽限后 kill）与单文件超时（600 秒/文件）。

上层语义：

- :func:`decompile_rpyc`：目标 .rpy 已存在 → 跳过；跨目录输出时先复制
  .rpyc 到目标目录（unrpyc 只把输出写到输入同目录）、运行后清理临时副本，
  并校验目标 .rpy 确已产出；
- :func:`extract_rpa`：输出目录已存在且非空 → 整档跳过；解包完成后剥离
  产物中自带的 ``game/`` 前缀（上移到输出目录根，同名冲突保留既有条目并
  使本档计为失败）。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renpy_overlay.external_tools")

#: 单文件进程超时
TOOL_TIMEOUT_SECONDS = 600.0
#: Windows 下隐藏子进程控制台窗口
_CREATE_NO_WINDOW = 0x08000000
#: terminate 之后的强杀宽限秒数
_KILL_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class ToolResult:
    """一次外部工具调用的结果（``skipped`` 表示前置判定直接跳过、未启动进程）。"""

    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """正常完成（返回码 0、未超时 / 取消 / 跳过）。"""
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.cancelled
            and not self.skipped
        )


# ------------------------------------------------------------------ 工具定位


def plugin_dir() -> Path:
    """外部工具目录：源码运行为包内 ``assets/plugin``，打包运行为解包目录。

    打包时 assets 经 spec datas 收集到 ``_MEIPASS/renpy_overlay/assets``
    （保留包前缀），与源码包内路径同构。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / "renpy_overlay" / "assets" / "plugin"
        return Path(sys.executable).resolve().parent / "renpy_overlay" / "assets" / "plugin"
    return Path(__file__).resolve().parent / "assets" / "plugin"


def _locate(filename: str) -> Path:
    path = plugin_dir() / filename
    if not path.is_file():
        raise FileNotFoundError(f"外部工具缺失：{path}")
    return path


def rpaextract_exe() -> Path:
    """rpaextract.exe 路径（缺失抛 :class:`FileNotFoundError`，GUI 转提示）。"""
    return _locate("rpaextract.exe")


def unrpyc_exe() -> Path:
    """unrpyc.exe 路径（缺失抛 :class:`FileNotFoundError`，GUI 转提示）。"""
    return _locate("unrpyc.exe")


# ------------------------------------------------------------------ 子进程封装


def run_tool(
    exe: Path,
    args: list[str],
    *,
    timeout: float = TOOL_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
) -> ToolResult:
    """执行外部工具并等待收束（阻塞，供后台任务线程调用）。

    - list 形式 argv（不经 shell），路径含空格 / 中文无需转义；
    - 子进程环境注入 ``PYTHONIOENCODING=utf-8``（对 .NET 系 rpaextract 无害，
      使 Python 系 unrpyc 的 stdout 稳定为 UTF-8，避免 GBK 乱码日志）；
    - 轮询响应 ``stop_event``：置位即 terminate，宽限 :data:`_KILL_GRACE_SECONDS`
      秒后仍存活则 kill（Windows 上 terminate 即强制结束）；
    - 超时强制结束并记 ``timed_out``；
    - stdout / stderr 按行落 DEBUG 日志。
    """
    argv = [str(exe), *args]
    logger.debug("外部工具启动：%s", subprocess.list2cmdline(argv))
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"} if sys.platform == "win32" else None
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            env=env,
        )
    except OSError as exc:
        logger.error("外部工具启动失败：%s（%s）", exe, exc)
        return ToolResult()

    deadline = time.monotonic() + timeout
    timed_out = cancelled = False
    while process.poll() is None:
        if stop_event is not None and stop_event.is_set() and not cancelled:
            cancelled = True
            logger.info(
                "外部工具停止请求：%s（terminate，%s 秒后强杀）",
                exe.name,
                _KILL_GRACE_SECONDS,
            )
            process.terminate()
            deadline = min(deadline, time.monotonic() + _KILL_GRACE_SECONDS)
        if time.monotonic() >= deadline:
            timed_out = not cancelled
            if timed_out:
                logger.warning("外部工具超时（限时 %.0f 秒，强制结束）：%s", timeout, exe.name)
            process.kill()
            break
        time.sleep(0.05)

    try:
        stdout, stderr = process.communicate(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill 后极端僵死的兜底
        process.kill()
        stdout, stderr = process.communicate()

    result = ToolResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode("utf-8", "replace") if stdout else "",
        stderr=stderr.decode("utf-8", "replace") if stderr else "",
        timed_out=timed_out,
        cancelled=cancelled,
    )
    for stream_name, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        for line in text.splitlines():
            logger.debug("外部工具 %s %s：%s", exe.name, stream_name, line)
    if result.returncode != 0:
        logger.warning("外部工具 %s 退出码 %d", exe.name, result.returncode)
    return result


# ------------------------------------------------------------------ 上层语义


def decompile_rpyc(
    exe: Path,
    src: Path,
    dst_rpy: Path,
    *,
    timeout: float = TOOL_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
) -> ToolResult:
    """反编译单个 .rpyc 到指定 .rpy 路径。

    - ``dst_rpy`` 已存在 → 直接跳过（不调用工具、不计失败）；
    - unrpyc 只把输出写到输入同目录：跨目录目标先把 .rpyc 复制过去，运行
      后无论成败都删除临时副本；
    - 返回码 0 但目标未产出 → 按失败处理。
    """
    src, dst_rpy = Path(src), Path(dst_rpy)
    if dst_rpy.exists():
        logger.debug("反编译跳过（目标已存在）：%s", dst_rpy)
        return ToolResult(skipped=True)
    try:
        dst_rpy.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("反编译输出目录创建失败：%s（%s）", dst_rpy.parent, exc)
        return ToolResult()

    temp_copy: Path | None = None
    if _same_dir(src.parent, dst_rpy.parent):
        tool_input = src
    else:
        temp_copy = dst_rpy.parent / src.name
        try:
            shutil.copyfile(src, temp_copy)
        except OSError as exc:
            logger.error("反编译源复制失败：%s → %s（%s）", src, temp_copy, exc)
            return ToolResult()
        logger.debug("反编译源已复制到目标目录：%s", temp_copy)
        tool_input = temp_copy

    try:
        result = run_tool(exe, [str(tool_input)], timeout=timeout, stop_event=stop_event)
    finally:
        if temp_copy is not None:
            try:
                temp_copy.unlink()
            except OSError as exc:
                logger.warning("反编译临时副本删除失败：%s（%s）", temp_copy, exc)

    if result.ok and not dst_rpy.exists():
        logger.error("反编译返回成功但未产出目标文件：%s", dst_rpy)
        return ToolResult(
            returncode=result.returncode if result.returncode else 1,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
        )
    return result


def extract_rpa(
    exe: Path,
    archive: Path,
    out_dir: Path,
    *,
    timeout: float = TOOL_TIMEOUT_SECONDS,
    stop_event: threading.Event | None = None,
) -> ToolResult:
    """整档解包 .rpa 到输出目录。

    ``out_dir`` 由 ``overlay_paths.extract_target_dir`` 产生，为归档专属
    目录（不同归档互不覆盖），"非空跳过"即同归档重复解包的幂等防护。

    - ``out_dir`` 已存在且非空 → 整档跳过（不启动工具）；
    - 调用 ``rpaextract.exe -x -f <archive> -o <out_dir>``；
    - 解包成功后剥离产物中自带的 ``game/`` 前缀；有上移冲突 → 本档计失败
      （冲突条目保留在 ``game/`` 下供人工检查）。
    """
    archive, out_dir = Path(archive), Path(out_dir)
    if out_dir.is_dir() and any(out_dir.iterdir()):
        logger.debug("解包跳过（输出目录非空）：%s", out_dir)
        return ToolResult(skipped=True)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("解包输出目录创建失败：%s（%s）", out_dir, exc)
        return ToolResult()

    result = run_tool(
        exe,
        ["-x", "-f", str(archive), "-o", str(out_dir)],
        timeout=timeout,
        stop_event=stop_event,
    )
    if result.ok and _strip_game_prefix(out_dir) > 0:
        return ToolResult(
            returncode=result.returncode if result.returncode else 1,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def _strip_game_prefix(out_dir: Path) -> int:
    """把 ``out_dir/game`` 内条目上移到输出目录根并删除空目录。

    返回冲突 / 失败条数（>0 时调用方将本档计为失败；残留条目保留在
    ``game/`` 下并记日志，不抛异常）。
    """
    game_dir = out_dir / "game"
    if not game_dir.is_dir():
        return 0
    conflicts = 0
    for child in sorted(game_dir.iterdir()):
        target = out_dir / child.name
        if target.exists():
            conflicts += 1
            logger.error("game/ 前缀上移冲突（保留目标既有条目）：%s", target)
            continue
        try:
            shutil.move(str(child), str(target))
        except (OSError, shutil.Error) as exc:
            conflicts += 1
            logger.error("game/ 前缀上移失败：%s（%s）", child, exc)
    try:
        game_dir.rmdir()
    except OSError:
        logger.warning("game/ 目录未清空（存在冲突条目），保留供人工检查：%s", game_dir)
    return conflicts


def _same_dir(left: Path, right: Path) -> bool:
    """两个目录是否为同一目录（resolve 归一，防御符号链接 / 大小写差异）。"""
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:  # pragma: no cover - resolve 失败退回字面比较
        return Path(left) == Path(right)
