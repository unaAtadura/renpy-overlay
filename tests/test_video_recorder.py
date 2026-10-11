"""录制控制器（VideoRecorder）的离线验证：双流编排、停止顺序与降级。

遵循项目离线测试约定：不构造真实窗口，QProcess 与录音线程经 stub 注入，
仅用 QCoreApplication 兜底 QTimer；合并经 merge_fn 注入，不触真实 ffmpeg。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from PyQt6.QtCore import QCoreApplication

from renpy_overlay.video.recorder import (
    MENU_LABEL_IDLE,
    RECORD_START_TEXT,
    RECORD_STOP_TEXT,
    VideoRecorder,
    menu_label,
    progress_text,
)


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """QTimer 是 QObject：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


class _Signal:
    """QProcess 信号的轻量 stub（connect/emit 语义）。"""

    def __init__(self):
        self._handlers = []

    def connect(self, handler):
        self._handlers.append(handler)

    def emit(self, *args):
        for handler in list(self._handlers):
            handler(*args)


class _StubProcess:
    """QProcess 鸭子接口 stub：start 釆用与真实 QProcess 相同的两参签名。"""

    def __init__(self):
        self.finished = _Signal()
        self.errorOccurred = _Signal()
        self.started_program: str | None = None
        self.started_arguments: list = []
        self.terminated = False
        self.killed = False
        self.wait_result = True

    def start(self, program, arguments=None):
        # 形状对齐真实 QProcess.start(program: str, arguments: Iterable[str])：
        # 传单列表在 PyQt6 下会 TypeError（实测崩溃，见 test_start_uses_two_argument_form）
        assert isinstance(program, str), "program 必须是字符串（PyQt6 两参形式）"
        assert arguments is not None, "arguments 必须显式传入（不接受单列表）"
        self.started_program = program
        self.started_arguments = list(arguments)
        self.started_args = [program, *self.started_arguments]  # 兼容既有断言

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def waitForFinished(self, ms):
        return self.wait_result


class _Harness:
    """构造一台完全注入 stub 的录制控制器，收集全部交互痕迹。"""

    def __init__(self, tmp_path, *, audio_available=True, merge_result=True):
        self.notifies: list[str] = []
        self.processes: list[_StubProcess] = []
        self.audio_calls: list = []
        self.merge_calls: list = []
        self.now = {"mono": 1000.0}
        self._merge_result = merge_result
        harness = self

        def _audio_recorder(path, stop_event):
            harness.audio_calls.append(path)
            stop_event.wait(timeout=2.0)
            path.write_bytes(b"RIFF-fake-wav")
            return True

        self.recorder = VideoRecorder(
            notify=self.notifies.append,
            game_dir=tmp_path,
            geometry_fn=lambda: (0, 0, 800, 600),
            process_factory=self._make_process,
            audio_recorder=_audio_recorder,
            audio_available=audio_available,
            ffmpeg_path="C:/tools/ffmpeg.exe",
            merge_fn=self._merge,
            clock_fn=lambda: datetime(2026, 10, 11, 10, 10, 10),
            monotonic_fn=lambda: self.now["mono"],
        )

    def _make_process(self):
        process = _StubProcess()
        self.processes.append(process)
        return process

    def _merge(self, ffmpeg, *, video_path, audio_path, out_path):
        self.merge_calls.append((ffmpeg, video_path, audio_path, out_path))
        return self._merge_result


def test_menu_label_idle():
    assert menu_label(None) == MENU_LABEL_IDLE == "录制视频"


def test_menu_label_elapsed_formatting():
    assert menu_label(0) == "录制视频 00:00"
    assert menu_label(75) == "录制视频 01:15"
    assert menu_label(600) == "录制视频 10:00"
    assert menu_label(-5) == "录制视频 00:00"  # 负值钳零


def test_progress_text_wording():
    assert progress_text(10) == "视频录制中，已经过 10分钟"


def test_start_starts_dual_streams_and_notifies(tmp_path):
    harness = _Harness(tmp_path)
    assert harness.recorder.start() is True
    assert harness.recorder.recording is True
    video_dir = tmp_path / "renpy_overlay_cache" / "video"
    assert video_dir.is_dir(), "应自动创建 video 目录"
    assert len(harness.processes) == 1
    args = harness.processes[0].started_args
    assert args[0] == "C:/tools/ffmpeg.exe"
    assert "-f" in args and args[args.index("-f") + 1] == "gdigrab"
    assert args[args.index("-video_size") + 1] == "800x600"
    assert args[-1] == str(video_dir / "20261011-101010.tmp.mp4")  # 双流：临时视频
    assert len(harness.audio_calls) == 1  # 录音线程已拉起
    assert harness.audio_calls[0] == video_dir / "20261011-101010.tmp.wav"
    assert harness.notifies[0] == RECORD_START_TEXT


def test_start_uses_two_argument_start_form(tmp_path):
    """防回归：QProcess.start 必须是 start(program, arguments) 两参形式。

    PyQt6 移除了 PyQt5 的 start(command) 单参数重载：传单个列表会 TypeError
    （Qt 主线程菜单分发链中未捕获 → 进程崩溃，实测见
    logs/renpy_overlay_20261011_154701.log）。stub 的 start 签名与真实
    QProcess 对齐，program/arguments 必须分开传入。
    """
    harness = _Harness(tmp_path)
    assert harness.recorder.start() is True
    process = harness.processes[0]
    assert isinstance(process.started_program, str)
    assert isinstance(process.started_arguments, list)
    assert process.started_program == "C:/tools/ffmpeg.exe"
    assert process.started_arguments[0] == "-f"
    assert process.started_arguments[-1] == str(
        tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.tmp.mp4"
    )


def test_real_qprocess_start_smoke():
    """真实 QProcess.start(program, arguments) 冒烟：钉住 API 形状不回归。

    QProcess 是 QObject 非窗口，QCoreApplication 环境即可实例化（离线约定
    允许）；启动 Python 自身执行 -c pass，不弹窗口、不依赖 ffmpeg——若有
    人改回单列表形式，本测试会立即以同样的 TypeError 失败。
    """
    import sys

    from PyQt6.QtCore import QProcess

    process = QProcess()
    process.start(sys.executable, ["-c", "pass"])
    try:
        assert process.waitForFinished(10_000), "进程未在 10s 内退出"
        assert process.exitStatus() == QProcess.ExitStatus.NormalExit
        assert process.exitCode() == 0
    finally:
        process.kill()
        process.waitForFinished(1000)


def test_start_without_ffmpeg_degrades_with_notice(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder._ffmpeg_path = None
    harness.recorder._find_fn = lambda: None
    assert harness.recorder.start() is False
    assert harness.processes == []
    assert harness.notifies == ["视频录制不可用：未找到 ffmpeg.exe"]


def test_start_video_only_without_recorder_schemes(tmp_path):
    harness = _Harness(tmp_path, audio_available=False)
    assert harness.recorder.start() is True
    args = harness.processes[0].started_args
    assert args[-1] == str(
        tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.mp4"
    ), "仅视频降级：直接录到最终名"
    assert harness.audio_calls == []
    assert harness.notifies[0] == RECORD_START_TEXT  # 三段提示文案不变


def test_stop_order_terminate_then_finish(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.start()
    process = harness.processes[0]
    harness.recorder.stop()
    assert process.terminated is True, "停止应先优雅结束 ffmpeg（收 moov）"
    assert harness.recorder.recording is True, "finished 到达前仍处于录制态"
    assert RECORD_STOP_TEXT not in harness.notifies
    process.finished.emit(0, 0)  # ffmpeg finalize 完成
    assert harness.recorder.recording is False
    assert harness.notifies[-1] == RECORD_STOP_TEXT
    assert len(harness.merge_calls) == 1
    ffmpeg, video, audio, out = harness.merge_calls[0]
    assert video == tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.tmp.mp4"
    assert audio == tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.tmp.wav"
    assert out == tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.mp4"
    assert "视频+系统音频" not in harness.notifies


def test_stop_joins_audio_thread_and_cleans_tmp(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.recorder.stop()
    harness.processes[0].finished.emit(0, 0)
    assert harness.recorder._audio_thread is None, "录音线程应已 join 并清引用"


def test_merge_failure_falls_back_to_silent_video(tmp_path):
    harness = _Harness(tmp_path, merge_result=False)
    harness.recorder.start()
    final = tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.mp4"
    tmp_video = final.with_name("20261011-101010.tmp.mp4")
    tmp_video.write_bytes(b"fake-video")  # 模拟 ffmpeg 已写出的临时视频
    harness.recorder.stop()
    harness.processes[0].finished.emit(0, 0)
    assert final.is_file(), "合并失败应保留无声视频为最终产物"
    assert not tmp_video.exists(), "临时视频应已改名"


def test_process_error_aborts_recording(tmp_path):
    """FailedToStart 等进程错误：按中止收尾，不合并、提示结束。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.processes[0].errorOccurred.emit(1)
    assert harness.recorder.recording is False
    assert harness.merge_calls == []
    assert harness.notifies[-1] == RECORD_STOP_TEXT


def test_unexpected_exit_finishes_recording(tmp_path):
    """ffmpeg 自行退出（未请求停止）：结束录制并提示。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.processes[0].finished.emit(1, 1)  # CrashExit
    assert harness.recorder.recording is False
    assert harness.notifies[-1] == RECORD_STOP_TEXT


def test_progress_tick_notifies_every_ten_minutes(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.recorder._on_progress_tick()
    harness.recorder._on_progress_tick()
    assert "视频录制中，已经过 10分钟" in harness.notifies
    assert "视频录制中，已经过 20分钟" in harness.notifies


def test_elapsed_seconds_and_menu_label(tmp_path):
    harness = _Harness(tmp_path)
    assert harness.recorder.menu_label() == "录制视频"
    harness.recorder.start()
    harness.now["mono"] = 1075.0  # +75s
    assert harness.recorder.elapsed_seconds() == 75
    assert harness.recorder.menu_label() == "录制视频 01:15"


def test_toggle_switches_start_and_stop(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.toggle()
    assert harness.recorder.recording is True
    harness.recorder.toggle()  # 进入停止流程（等 finished）
    harness.processes[0].finished.emit(0, 0)
    assert harness.recorder.recording is False
    harness.recorder.toggle()  # 可再次开始
    assert harness.recorder.recording is True


def test_shutdown_stops_and_finishes(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.recorder.shutdown()
    assert harness.processes[0].terminated is True
    assert harness.processes[0].killed is False  # waitForFinished 成功无需 kill
    assert harness.recorder.recording is False
    assert len(harness.merge_calls) == 1  # shutdown 同步完成合并收尾


def test_shutdown_kills_when_wait_times_out(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.processes[0].wait_result = False
    harness.recorder.shutdown()
    assert harness.processes[0].killed is True
    assert harness.recorder.recording is False


def test_start_ignored_while_recording(tmp_path):
    harness = _Harness(tmp_path)
    assert harness.recorder.start() is True
    assert harness.recorder.start() is False
    assert len(harness.processes) == 1
