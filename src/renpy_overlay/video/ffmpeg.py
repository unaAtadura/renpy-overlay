"""ffmpeg 探测与命令构造：视频录制 / 双流合并 / 音轨截取的唯一封装。

选型（需求确认：ffmpeg.exe 不捆绑，由用户自备）：

- 探测顺序 = 程序目录（打包后 exe 旁，开发期为项目根）→ PATH
  （``shutil.which``）；找不到返回 ``None``，调用方降级提示；
- 命令行参数全部由纯函数构造（可离线快照测试），执行由调用方决定：
  录制用 QProcess（需要 WM_CLOSE 优雅收 moov），截取/合并用 subprocess；
- wav 截取经 stdout 管道（``-f wav pipe:1``）直接返回字节，不落临时文件；
- 双流合并（视频流 copy + aac 音轨）失败时由调用方回退保留无声视频。

为什么音轨截取用 ``-ss <start> -t <dur>`` 而非 ``-to``：``-ss`` 作为输入
选项重置时间戳后，``-to`` 的参考点存在版本歧义，时长 ``-t`` 语义唯一。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("renpy_overlay.video.ffmpeg")

FFMPEG_FILENAME = "ffmpeg.exe"

#: 录制帧率：gdigrab 实时抓屏的稳妥值（过高易丢帧）
DEFAULT_RECORD_FPS = 30
#: 合并阶段 subprocess 超时（秒）：视频流 copy 通常秒级完成
MERGE_TIMEOUT_S = 120.0
#: 音轨截取输出规格：16k 单声道 s16le（ASR 够用且体积最小，~32KB/s）
ASR_SAMPLE_RATE = 16000
ASR_CHANNELS = 1

#: 截取/合并的 subprocess 通用参数（隐藏窗口，输出走管道不刷控制台）
_SUBPROCESS_KWARGS = {
    "stdout": subprocess.PIPE,
    "stderr": subprocess.PIPE,
    "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
}


def default_search_dirs() -> list[Path]:
    """程序目录候选：打包后 exe 旁，开发期为项目根（与 config.default_path 同源）。"""
    if getattr(sys, "frozen", False):
        return [Path(sys.executable).resolve().parent]
    package_root = Path(__file__).resolve().parents[3]
    if (package_root / "pyproject.toml").is_file():
        return [package_root]
    return [Path.cwd()]


def find_ffmpeg(
    extra_dirs: list[Path] | None = None,
    which_fn=shutil.which,
) -> str | None:
    """定位 ffmpeg.exe：先候选目录（程序目录优先于 PATH），再 PATH 探测。

    ``which_fn`` 仅供离线测试注入；找不到返回 ``None``（调用方降级提示）。
    """
    dirs = default_search_dirs() if extra_dirs is None else extra_dirs
    for directory in dirs:
        candidate = Path(directory) / FFMPEG_FILENAME
        if candidate.is_file():
            logger.info("使用程序目录的 ffmpeg：%s", candidate)
            return str(candidate)
    found = which_fn("ffmpeg")
    if found:
        logger.info("使用 PATH 中的 ffmpeg：%s", found)
        return found
    logger.warning("未找到 ffmpeg.exe（程序目录与 PATH 均缺失），视频功能降级")
    return None


def video_record_args(
    ffmpeg_path: str,
    *,
    offset_x: int,
    offset_y: int,
    width: int,
    height: int,
    out_path: str | Path,
    fps: int = DEFAULT_RECORD_FPS,
) -> list[str]:
    """gdigrab 全屏录制命令（纯视频；音频流由环回录音另行录制，停止时合并）。

    ``(offset_x, offset_y, width, height)`` 取主屏几何（多屏布局下主屏原点
    可能非零，由调用方经 QGuiApplication 屏幕几何计算）。
    """
    return [
        ffmpeg_path,
        "-f", "gdigrab",
        "-framerate", str(fps),
        "-offset_x", str(offset_x),
        "-offset_y", str(offset_y),
        "-video_size", f"{width}x{height}",
        "-i", "desktop",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-y",
        str(out_path),
    ]


def mux_args(
    ffmpeg_path: str,
    *,
    video_path: str | Path,
    audio_path: str | Path | None,
    out_path: str | Path,
) -> list[str]:
    """停止阶段封装命令：视频流不重编码 copy 到最终 mp4 容器。

    ``audio_path`` 为 None（仅视频降级）时省略音频输入，退化为单流 remux
    （mkv 中间容器 → mp4，流 copy 秒级完成）；双流时音频编码为 aac 并用
    -shortest 对齐到较短流。
    """
    args = [ffmpeg_path, "-i", str(video_path)]
    if audio_path is not None:
        args += ["-i", str(audio_path)]
    args += ["-c:v", "copy"]
    if audio_path is not None:
        args += ["-c:a", "aac", "-shortest"]
    args += ["-y", str(out_path)]
    return args


def normalize_range(start_s: float, end_s: float) -> tuple[float, float] | None:
    """整理截取区间：终点早于起点自动交换（需求）；间隔为 0 视为无效。

    返回 ``(start, end)``（start < end）或 ``None``（调用方不发起请求）。
    """
    start_s, end_s = float(start_s), float(end_s)
    if start_s > end_s:
        start_s, end_s = end_s, start_s
    if end_s - start_s <= 0:
        return None
    return start_s, end_s


def audio_extract_args(
    ffmpeg_path: str,
    *,
    video_path: str | Path,
    start_s: float,
    duration_s: float,
) -> list[str]:
    """音轨截取命令：只取音频、转 16k 单声道 wav、经 stdout 管道输出。"""
    return [
        ffmpeg_path,
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", str(video_path),
        "-vn",
        "-ac", str(ASR_CHANNELS),
        "-ar", str(ASR_SAMPLE_RATE),
        "-acodec", "pcm_s16le",
        "-f", "wav",
        "pipe:1",
    ]


def extract_wav(
    ffmpeg_path: str,
    *,
    video_path: str | Path,
    start_s: float,
    end_s: float,
    run_fn=subprocess.run,
    timeout: float = MERGE_TIMEOUT_S,
) -> bytes | None:
    """截取 ``[start_s, end_s]`` 音频轨为 wav 字节；失败/空输出返回 ``None``。

    ``run_fn`` 仅供离线测试注入。区间终点早于起点自动交换（需求）。
    """
    rng = normalize_range(start_s, end_s)
    if rng is None:
        logger.warning("音频截取区间无效（start=%s，end=%s）", start_s, end_s)
        return None
    start, duration = rng[0], rng[1] - rng[0]
    args = audio_extract_args(
        ffmpeg_path, video_path=video_path, start_s=start, duration_s=duration
    )
    try:
        completed = run_fn(args, timeout=timeout, **_SUBPROCESS_KWARGS)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("音频截取失败（%s）：%s", video_path, exc)
        return None
    if completed.returncode != 0 or not completed.stdout:
        logger.error(
            "音频截取失败（returncode=%s，stderr=%s）",
            completed.returncode,
            (completed.stderr or b"")[-200:],
        )
        return None
    logger.info("音频截取完成：%s [%s, %s] -> %d 字节", video_path, start_s, end_s, len(completed.stdout))
    return completed.stdout


def merge_streams(
    ffmpeg_path: str,
    *,
    video_path: str | Path,
    audio_path: str | Path | None,
    out_path: str | Path,
    run_fn=subprocess.run,
    timeout: float = MERGE_TIMEOUT_S,
) -> bool:
    """停止阶段封装执行（双流合并或仅视频 remux）；失败返回 ``False``。

    ``audio_path`` 为 None 时退化为单流 remux（mkv 中间容器 → mp4）。
    ``run_fn`` 仅供离线测试注入。
    """
    args = mux_args(
        ffmpeg_path, video_path=video_path, audio_path=audio_path, out_path=out_path
    )
    try:
        completed = run_fn(args, timeout=timeout, **_SUBPROCESS_KWARGS)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("双流合并失败（%s）：%s", out_path, exc)
        return False
    if completed.returncode != 0:
        logger.error(
            "双流合并失败（returncode=%s，stderr=%s）",
            completed.returncode,
            (completed.stderr or b"")[-200:],
        )
        return False
    logger.info("双流合并完成：%s", out_path)
    return True


def probe_ok(ffmpeg_path: str | None) -> bool:
    """ffmpeg 是否可用（非 None 且文件存在；诊断与降级判断用）。"""
    return bool(ffmpeg_path) and os.path.isfile(ffmpeg_path)
