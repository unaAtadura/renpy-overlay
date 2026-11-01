"""外部工具批量任务编排：文件级串行流水（反编译 / 解包），回调契约与 BatchTask 一致。

由预构建主窗口在后台守护线程执行 :meth:`ToolFileTask.run`，进度与阶段经
回调产出（GUI 侧经 Qt Signal 转回主线程）。与翻译流水（``runner.BatchTask``）
的差异：

- 无 API 调用与并发调度：文件级串行执行（外部进程本身重负载）；
- 进度语义：``on_ready(文件总数)`` → ``on_progress(processed, failed, skipped)``，
  首参为已处理文件数（成功/失败/跳过均计入，进度条走满），失败与跳过为
  注记细分（复用三参信号：翻译任务=并发档位，工具任务=跳过数）；
- 单文件失败（返回码≠0 / 超时 / 取消 / 单文件异常）计数后继续，终态恒为
  completed（失败数只在进度文案体现）；仅编排层自身异常收敛为 error；
- skipped（前置跳过：目标已存在 / 输出目录非空）单独计数，不计失败。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from . import external_tools
from .external_tools import ToolResult
from .pretranslate.overlay_paths import decompile_target, extract_target_dir

logger = logging.getLogger("renpy_overlay.tools_runner")


class ToolFileTask:
    """文件级串行工具任务基类（子类实现 :meth:`_process_one`）。"""

    def __init__(
        self,
        files: list[str],
        game_dir: Path,
        *,
        stop_event: threading.Event,
        on_ready: Callable[[int], None],
        on_progress: Callable[[int, int, int], None],
        on_finished: Callable[[str], None],
    ) -> None:
        self._files = list(files)
        self._game_dir = Path(game_dir)
        self._stop = stop_event
        self._on_ready = on_ready
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._finished = False  # on_finished 只发一次（正常/取消/异常收敛）

    # ---- 对外入口 -----------------------------------------------------------

    def run(self) -> None:
        """执行整轮任务（在后台线程调用）；编排层异常收敛为 error 终态。"""
        try:
            self._run_inner()
        except Exception:  # pragma: no cover - 防御：不让线程静默死亡
            logger.exception("工具任务异常终止")
            self._finish("error")

    # ---- 内部流水 -----------------------------------------------------------

    def _run_inner(self) -> None:
        total = len(self._files)
        self._on_ready(total)
        if not total:
            logger.info("工具任务完成：未勾选任何文件")
            self._finish("completed")
            return
        done = failed = skipped = processed = 0
        for rel in self._files:
            if self._stop.is_set():
                logger.info(
                    "工具任务取消：%d/%d 已处理（成功 %d / 失败 %d / 跳过 %d）",
                    processed, total, done, failed, skipped,
                )
                self._finish("cancelled")
                return
            try:
                outcome = self._process_one(rel)
            except Exception:  # 单文件异常不中断整轮
                logger.exception("工具任务单文件异常，计失败：%s", rel)
                outcome = ToolResult(returncode=-1)
            if outcome.skipped:
                skipped += 1
                logger.info("工具任务跳过（目标已存在）：%s", rel)
            elif outcome.ok:
                done += 1
                logger.info("工具任务完成：%s", rel)
            else:
                failed += 1
                logger.warning(
                    "工具任务失败（继续下一个）：%s（returncode=%d%s）",
                    rel,
                    outcome.returncode,
                    "，超时强制结束" if outcome.timed_out else "，已取消" if outcome.cancelled else "",
                )
            processed += 1
            self._on_progress(processed, failed, skipped)
        logger.info(
            "工具任务完成：%d 成功 / %d 失败 / %d 跳过（共 %d 文件）",
            done, failed, skipped, total,
        )
        self._finish("completed")

    def _process_one(self, rel: str) -> ToolResult:  # pragma: no cover - 抽象
        raise NotImplementedError

    def _finish(self, status: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._on_finished(status)


class DecompileTask(ToolFileTask):
    """.rpyc 反编译任务（页 1）：目标 .rpy 已存在恒跳过。"""

    def __init__(
        self,
        files: list[str],
        game_dir: Path,
        scan_root: Path,
        exe: Path,
        *,
        stop_event: threading.Event,
        on_ready: Callable[[int], None],
        on_progress: Callable[[int, int, int], None],
        on_finished: Callable[[str], None],
    ) -> None:
        super().__init__(
            files, game_dir,
            stop_event=stop_event, on_ready=on_ready,
            on_progress=on_progress, on_finished=on_finished,
        )
        self._scan_root = Path(scan_root)
        self._exe = exe

    def _process_one(self, rel: str) -> ToolResult:
        src = self._scan_root / rel
        dst = decompile_target(self._game_dir, self._scan_root, rel)
        return external_tools.decompile_rpyc(self._exe, src, dst, stop_event=self._stop)


class ExtractTask(ToolFileTask):
    """.rpa 解包任务（页 2）：归档专属目录解包——输出目录非空整档跳过（同归档幂等）
    + game/ 前缀剥离（目录映射见 ``overlay_paths.extract_target_dir``）。"""

    def __init__(
        self,
        files: list[str],
        game_dir: Path,
        exe: Path,
        *,
        stop_event: threading.Event,
        on_ready: Callable[[int], None],
        on_progress: Callable[[int, int, int], None],
        on_finished: Callable[[str], None],
    ) -> None:
        super().__init__(
            files, game_dir,
            stop_event=stop_event, on_ready=on_ready,
            on_progress=on_progress, on_finished=on_finished,
        )
        self._exe = exe

    def _process_one(self, rel: str) -> ToolResult:
        archive = self._game_dir / "game" / rel
        out_dir = extract_target_dir(self._game_dir, rel)
        return external_tools.extract_rpa(self._exe, archive, out_dir, stop_event=self._stop)
