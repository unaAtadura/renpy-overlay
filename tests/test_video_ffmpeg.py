"""ffmpeg 探测与命令构造的离线验证：探测顺序、命令快照、截取与合并降级。

不触真实 ffmpeg 进程：探测经 which_fn 注入、subprocess 经 run_fn 注入，
仅验证纯函数行为与调用契约（命令行参数逐项断言）。
"""

from __future__ import annotations

import subprocess

from renpy_overlay.video import ffmpeg

FFMPEG = "C:/tools/ffmpeg.exe"
VIDEO = "D:/game/renpy_overlay_cache/video/tmp_video.mp4"
AUDIO = "D:/game/renpy_overlay_cache/video/tmp_audio.wav"
OUT = "D:/game/renpy_overlay_cache/video/20261011-101010.mp4"


# ---------------------------------------------------------------- ffmpeg 探测


def test_find_ffmpeg_prefers_extra_dirs(tmp_path):
    bundled = tmp_path / ffmpeg.FFMPEG_FILENAME
    bundled.write_text("stub", encoding="utf-8")  # 文件占位即视为存在

    def _which(name):
        raise AssertionError("程序目录命中时不应再探测 PATH")

    assert ffmpeg.find_ffmpeg([tmp_path], which_fn=_which) == str(bundled)


def test_find_ffmpeg_falls_back_to_path(tmp_path):
    def _which(name):
        assert name == "ffmpeg"
        return FFMPEG

    assert ffmpeg.find_ffmpeg([tmp_path], which_fn=_which) == FFMPEG


def test_find_ffmpeg_missing_degrades_to_none(tmp_path):
    assert ffmpeg.find_ffmpeg([tmp_path], which_fn=lambda name: None) is None


def test_default_search_dirs_points_to_project_root():
    dirs = ffmpeg.default_search_dirs()
    assert dirs and (dirs[0] / "pyproject.toml").is_file(), (
        "开发期默认候选目录应为项目根（与 config.default_path 同源）"
    )


# ---------------------------------------------------------------- 命令构造


def test_video_record_args_snapshot():
    args = ffmpeg.video_record_args(
        FFMPEG, offset_x=-1920, offset_y=0, width=1920, height=1080, out_path=VIDEO
    )
    assert args == [
        FFMPEG,
        "-f", "gdigrab",
        "-framerate", "30",
        "-offset_x", "-1920",
        "-offset_y", "0",
        "-video_size", "1920x1080",
        "-i", "desktop",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-y",
        VIDEO,
    ]


def test_mux_args_snapshot():
    assert ffmpeg.mux_args(FFMPEG, video_path=VIDEO, audio_path=AUDIO, out_path=OUT) == [
        FFMPEG,
        "-i", VIDEO,
        "-i", AUDIO,
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        "-y",
        OUT,
    ]


def test_audio_extract_args_snapshot():
    args = ffmpeg.audio_extract_args(
        FFMPEG, video_path=VIDEO, start_s=1.5, duration_s=3.25
    )
    assert args == [
        FFMPEG,
        "-ss", "1.500",
        "-t", "3.250",
        "-i", VIDEO,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-acodec", "pcm_s16le",
        "-f", "wav",
        "pipe:1",
    ]


# ---------------------------------------------------------------- 区间整理


def test_normalize_range_swaps_reversed_endpoints():
    assert ffmpeg.normalize_range(5.0, 2.0) == (2.0, 5.0)


def test_normalize_range_keeps_normal_interval():
    assert ffmpeg.normalize_range(2.0, 5.0) == (2.0, 5.0)


def test_normalize_range_rejects_zero_interval():
    assert ffmpeg.normalize_range(3.0, 3.0) is None


# ---------------------------------------------------------------- wav 截取


class _Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_extract_wav_returns_stdout_bytes():
    calls = []

    def _run(args, **kwargs):
        calls.append((args, kwargs))
        return _Completed(stdout=b"RIFF....wav")

    assert ffmpeg.extract_wav(FFMPEG, video_path=VIDEO, start_s=4.0, end_s=1.0, run_fn=_run) == b"RIFF....wav"
    args, kwargs = calls[0]
    # 终点早于起点：交换后 -ss=1.0，时长 = 3.0（-ss/-t 而非 -to，语义唯一）
    assert args[2] == "1.000" and args[4] == "3.000"
    assert kwargs["timeout"] == ffmpeg.MERGE_TIMEOUT_S


def test_extract_wav_failure_returns_none():
    def _run(args, **kwargs):
        return _Completed(returncode=1, stderr=b"boom")

    assert ffmpeg.extract_wav(FFMPEG, video_path=VIDEO, start_s=1, end_s=2, run_fn=_run) is None


def test_extract_wav_empty_output_returns_none():
    assert ffmpeg.extract_wav(
        FFMPEG, video_path=VIDEO, start_s=1, end_s=2,
        run_fn=lambda args, **kw: _Completed(stdout=b""),
    ) is None


def test_extract_wav_invalid_interval_skips_execution():
    def _run(args, **kwargs):
        raise AssertionError("区间无效时不应发起进程调用")

    assert ffmpeg.extract_wav(FFMPEG, video_path=VIDEO, start_s=2, end_s=2, run_fn=_run) is None


def test_extract_wav_process_error_returns_none():
    def _run(args, **kwargs):
        raise subprocess.SubprocessError("no ffmpeg")

    assert ffmpeg.extract_wav(FFMPEG, video_path=VIDEO, start_s=1, end_s=2, run_fn=_run) is None


# ---------------------------------------------------------------- 双流合并


def test_merge_streams_success(tmp_path):
    assert ffmpeg.merge_streams(
        FFMPEG, video_path=VIDEO, audio_path=AUDIO, out_path=OUT,
        run_fn=lambda args, **kw: _Completed(),
    ) is True


def test_merge_streams_failure_returns_false(tmp_path):
    assert ffmpeg.merge_streams(
        FFMPEG, video_path=VIDEO, audio_path=AUDIO, out_path=OUT,
        run_fn=lambda args, **kw: _Completed(returncode=1, stderr=b"bad"),
    ) is False


def test_merge_streams_process_error_returns_false():
    def _run(args, **kwargs):
        raise OSError("spawn failed")

    assert ffmpeg.merge_streams(
        FFMPEG, video_path=VIDEO, audio_path=AUDIO, out_path=OUT, run_fn=_run
    ) is False


# ---------------------------------------------------------------- 可用性判断


def test_probe_ok(tmp_path):
    assert ffmpeg.probe_ok(FFMPEG) is False  # 不存在的路径
    assert ffmpeg.probe_ok(None) is False
    bundled = tmp_path / ffmpeg.FFMPEG_FILENAME
    bundled.write_text("stub", encoding="utf-8")
    assert ffmpeg.probe_ok(str(bundled)) is True
