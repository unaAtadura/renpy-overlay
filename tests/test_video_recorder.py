"""录制控制器（VideoRecorder）的离线验证：双流编排、停止顺序与降级。

遵循项目离线测试约定：不构造真实窗口，录屏编码与录音线程经 stub 注入，
仅用 QCoreApplication 兜底 QTimer/信号；合并经 merge_fn 注入，不触真实
PyAV 封装。完成回调（_on_capture_done）由测试手动驱动，模拟跨线程信号
到达主线程。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from PyQt6.QtCore import QCoreApplication, QRect

from renpy_overlay.video.recorder import (
    MENU_LABEL_IDLE,
    RECORD_START_TEXT,
    RECORD_STOP_TEXT,
    RECORD_UNAVAILABLE_TEXT,
    VideoRecorder,
    default_geometry,
    menu_label,
    physical_geometry,
    progress_text,
)


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """QTimer/signal 是 QObject 成员：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


class _Harness:
    """构造一台完全注入 stub 的录制控制器，收集全部交互痕迹。"""

    def __init__(
        self,
        tmp_path,
        *,
        audio_available=True,
        merge_result=True,
        capture_result=True,
        available=True,
    ):
        self.notifies: list[str] = []
        self.capture_calls: list = []
        self.audio_calls: list = []
        self.merge_calls: list = []
        self.now = {"mono": 1000.0}
        self._merge_result = merge_result
        self._capture_result = capture_result
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
            capture_fn=self._capture,
            audio_recorder=_audio_recorder,
            audio_available=audio_available,
            available_fn=lambda: available,
            merge_fn=self._merge,
            clock_fn=lambda: datetime(2026, 10, 11, 10, 10, 10),
            monotonic_fn=lambda: self.now["mono"],
        )

    def _capture(self, out_path, stop_event, **kwargs):
        self.capture_calls.append((out_path, stop_event, kwargs))
        return self._capture_result

    def _merge(self, *, video_path, audio_path, out_path):
        self.merge_calls.append((video_path, audio_path, out_path))
        return self._merge_result

    def finish_capture(self, ok: bool = True) -> None:
        """模拟录屏线程完成信号到达主线程（真实路径为 queued signal）。"""
        self.recorder._on_capture_done(ok)


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
    assert len(harness.capture_calls) == 1
    out_path, stop_event, kwargs = harness.capture_calls[0]
    assert out_path == video_dir / "20261011-101010.tmp.mkv"  # 双流：mkv 中间容器
    assert not stop_event.is_set(), "启动时停止事件不应置位"
    assert kwargs == {
        "offset_x": 0,
        "offset_y": 0,
        "width": 800,
        "height": 600,
        "fps": 30,
    }
    assert len(harness.audio_calls) == 1  # 录音线程已拉起
    assert harness.audio_calls[0] == video_dir / "20261011-101010.tmp.wav"
    assert harness.notifies[0] == RECORD_START_TEXT


def test_start_unavailable_degrades_with_notice(tmp_path):
    """PyAV 不可用：提示降级并返回 False（不拉起任何线程）。"""
    harness = _Harness(tmp_path, available=False)
    assert harness.recorder.start() is False
    assert harness.capture_calls == []
    assert harness.audio_calls == []
    assert harness.notifies == [RECORD_UNAVAILABLE_TEXT]


def test_start_video_only_without_recorder_schemes(tmp_path):
    harness = _Harness(tmp_path, audio_available=False)
    assert harness.recorder.start() is True
    out_path, _, _ = harness.capture_calls[0]
    assert out_path == (
        tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.tmp.mkv"
    ), "仅视频降级：同样录到 mkv 中间容器（停止时 remux 为 mp4）"
    assert harness.audio_calls == []
    assert harness.notifies[0] == RECORD_START_TEXT  # 三段提示文案不变


def test_stop_sets_stop_event_then_finishes(tmp_path):
    """停止：置位编码线程停止事件（协作式）；完成信号到达后才收尾。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    _, stop_event, _ = harness.capture_calls[0]
    harness.recorder.stop()
    assert stop_event.is_set(), "停止应置位录屏线程停止事件"
    assert harness.recorder.recording is True, "完成信号到达前仍处于录制态"
    assert RECORD_STOP_TEXT not in harness.notifies
    harness.finish_capture(True)  # 录屏线程正常结束
    assert harness.recorder.recording is False
    assert harness.notifies[-1] == RECORD_STOP_TEXT
    assert len(harness.merge_calls) == 1
    video, audio, out = harness.merge_calls[0]
    video_dir = tmp_path / "renpy_overlay_cache" / "video"
    assert video == video_dir / "20261011-101010.tmp.mkv"
    assert audio == video_dir / "20261011-101010.tmp.wav"
    assert out == video_dir / "20261011-101010.mp4"
    assert "视频+系统音频" not in harness.notifies


def test_stop_joins_audio_thread_and_cleans_tmp(tmp_path):
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.recorder.stop()
    harness.finish_capture(True)
    assert harness.recorder._audio_thread is None, "录音线程应已 join 并清引用"
    assert harness.recorder._capture_thread is None, "录屏线程引用应已清空"


def test_merge_failure_falls_back_to_intermediate_mkv(tmp_path):
    """封装失败回退：mkv 中间文件改名为同 Stem 的 .mkv 最终产物。"""
    harness = _Harness(tmp_path, merge_result=False)
    harness.recorder.start()
    final = tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.mp4"
    tmp_video = final.with_name("20261011-101010.tmp.mkv")
    tmp_video.write_bytes(b"fake-mkv")  # 模拟录屏线程已写出的中间容器
    harness.recorder.stop()
    harness.finish_capture(True)
    fallback = final.with_suffix(".mkv")
    assert fallback.is_file(), "封装失败应保留 mkv 中间容器为最终产物"
    assert not tmp_video.exists(), "临时视频应已改名"
    assert not final.exists(), "封装失败时 mp4 最终名不应存在"


def test_capture_failure_aborts_recording(tmp_path):
    """编码失败（record_screen 返回 False）：按中止收尾，不合并、提示结束。"""
    harness = _Harness(tmp_path, capture_result=False)
    harness.recorder.start()
    harness.recorder.stop()
    harness.finish_capture(False)
    assert harness.recorder.recording is False
    assert harness.merge_calls == []
    assert harness.notifies[-1] == RECORD_STOP_TEXT


def test_unexpected_capture_finish_aborts_recording(tmp_path):
    """未请求停止即结束（编码线程意外退出）：按中止收尾，不保留产物。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.finish_capture(True)  # 未 stop() 直接结束
    assert harness.recorder.recording is False
    assert harness.merge_calls == []
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
    harness.recorder.toggle()  # 进入停止流程（等完成信号）
    harness.finish_capture(True)
    assert harness.recorder.recording is False
    harness.recorder.toggle()  # 可再次开始
    assert harness.recorder.recording is True


def test_shutdown_stops_and_finishes(tmp_path):
    """宿主退出：置停止事件并同步等待编码线程后收尾（不抛异常）。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    _, stop_event, _ = harness.capture_calls[0]
    harness.recorder.shutdown()
    assert stop_event.is_set(), "shutdown 应置位录屏线程停止事件"
    assert harness.recorder.recording is False
    assert len(harness.merge_calls) == 1  # shutdown 同步完成合并收尾


def test_late_capture_done_after_shutdown_is_ignored(tmp_path):
    """shutdown 同步收尾后迟到的完成信号：幂等守卫拦截，不重复收尾。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.recorder.shutdown()
    assert len(harness.merge_calls) == 1
    harness.finish_capture(True)  # queued 信号迟到
    assert len(harness.merge_calls) == 1, "迟到信号不应重复封装/清理"
    assert harness.notifies.count(RECORD_STOP_TEXT) == 1


def test_start_ignored_while_recording(tmp_path):
    harness = _Harness(tmp_path)
    assert harness.recorder.start() is True
    assert harness.recorder.start() is False
    assert len(harness.capture_calls) == 1


# ---------------------------------------------------------------- 录制几何（DPI）


class _StubScreen:
    """QScreen 鸭子接口 stub：geometry() 返回 Qt 逻辑像素，DPR 可指定。"""

    def __init__(self, rect: QRect, dpr: float):
        self._rect = rect
        self._dpr = dpr

    def geometry(self) -> QRect:
        return self._rect

    def devicePixelRatio(self) -> float:  # noqa: N802 - Qt 约定
        return self._dpr


def test_physical_geometry_scales_by_dpr():
    """逻辑像素 → 物理像素（gdigrab 只认物理）：物理 = 逻辑 × DPR。

    DPR≠1 时直接用 Qt 逻辑宽高当抓屏区域会只录到屏幕左上一块
    （实测缺陷：录出 1560x1040 而非全屏）。
    """
    assert physical_geometry(-960, 0, 1440, 900, 2.0) == (-1920, 0, 2880, 1800)
    assert physical_geometry(0, 0, 1560, 1040, 1.5) == (0, 0, 2340, 1560)  # 四舍五入
    assert physical_geometry(0, 0, 1920, 1080, 1.0) == (0, 0, 1920, 1080)  # DPR=1 恒等


def test_physical_geometry_invalid_dpr_falls_back_to_identity():
    assert physical_geometry(0, 0, 1920, 1080, 0) == (0, 0, 1920, 1080)  # 非法回退 1.0
    assert physical_geometry(0, 0, 1920, 1080, None) == (0, 0, 1920, 1080)
    assert physical_geometry(0, 0, 0, 0, 2.0) == (0, 0, 1, 1)  # 宽高下限钳 1


def test_default_geometry_multiplies_device_pixel_ratio():
    """default_geometry 须把 Qt 逻辑几何换算为物理像素（多屏负偏移一并缩放）。"""
    stub = _StubScreen(QRect(-1560, 0, 1560, 1040), 2.0)
    assert default_geometry(screen=stub) == (-3120, 0, 3120, 2080)


def test_default_geometry_identity_at_dpr_one():
    stub = _StubScreen(QRect(0, 0, 1920, 1080), 1.0)
    assert default_geometry(screen=stub) == (0, 0, 1920, 1080)


# ---------------------------------------------------------------- 停止逻辑健壮性


def test_stop_repeated_clicks_are_idempotent(tmp_path):
    """录制中重复点击菜单（stop 多次）与重复完成回调：收尾仅执行一次。"""
    harness = _Harness(tmp_path)
    harness.recorder.start()
    harness.recorder.stop()
    harness.recorder.stop()  # 重复点击：被 _stopping 守卫拦截
    harness.recorder.toggle()  # toggle 亦走 stop 分支：同样被拦截
    assert len(harness.capture_calls) == 1
    _, stop_event, _ = harness.capture_calls[0]
    assert stop_event.is_set(), "首次停止已置位事件（重复点击不重复触发收尾）"
    assert harness.recorder.recording is True  # 仍在等完成信号
    harness.finish_capture(True)
    harness.finish_capture(True)  # 重复完成回调：幂等守卫拦截
    assert harness.recorder.recording is False
    assert len(harness.merge_calls) == 1, "收尾流程不应重入（不重复封装/清理）"
    assert harness.notifies.count(RECORD_STOP_TEXT) == 1
    assert harness.recorder.menu_label() == "录制视频", "结束后菜单文案应恢复"


def test_video_only_stop_remuxes_without_audio(tmp_path):
    """仅视频降级路径：统一 mkv 中间容器，停止时单流 remux（audio_path=None）。"""
    harness = _Harness(tmp_path, audio_available=False)
    harness.recorder.start()
    harness.recorder.stop()
    harness.finish_capture(True)
    assert len(harness.merge_calls) == 1
    video, audio, out = harness.merge_calls[0]
    assert video == tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.tmp.mkv"
    assert audio is None, "仅视频降级应退化为单流 remux（无音频输入）"
    assert out == tmp_path / "renpy_overlay_cache" / "video" / "20261011-101010.mp4"
