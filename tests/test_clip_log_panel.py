"""日志面板内部进度信息流单测（offscreen；不依赖 logging 传播链）。

面板内容只来自显式 append 系列（主线程）；本文件断言：
- 通道消息（info/success/warning/error）默认简洁模式恒可见并入环形缓存，
  切换显示等级不清空不丢行；debug 诊断通道仅完整调试模式可见；
- 与 logging 解耦：向 root logger 写日志（含引擎心跳）面板不再追加，
  attach/detach 上屏路径已移除；
- append 镜像写 logging（原始文本、对应级别）——文件/控制台存档保留；
- 长耗时任务进度（导出/字幕）经 worker 信号回主线程写入面板：离线驱动
  信号（不构造真实导出/ASR/媒体链路）断言可见性与缓存；
- 环形历史上限；按钮布局与阈值状态；root 级别不受切换影响。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from renpy_overlay.video.clip.editor_window import _ExportWorker, _SubtitleWorker
from renpy_overlay.video.clip.log_panel import COMPACT_LEVEL, DEBUG_LEVEL, LogPanel

TEST_LOGGER = logging.getLogger("renpy_overlay.video.clip.test.log_panel")
RENDER_LOGGER = logging.getLogger("renpy_overlay.video.clip.player.renderer_sdl2")
PANEL_LOGGER = logging.getLogger("renpy_overlay.video.clip.log_panel")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def debug_root():
    """root 临时放开 DEBUG（等价 setup_logging 的级别分工），测后恢复。"""
    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.DEBUG)
    yield root
    root.setLevel(saved)


@pytest.fixture()
def panel(qapp, debug_root):
    return LogPanel()


class TestPanelDefaults:
    def test_new_panel_defaults_compact(self, qapp):
        """重启等价场景：新建面板恢复默认简洁模式（状态不落盘）。"""
        p = LogPanel()
        assert not p.is_debug_visible()
        assert p._threshold == COMPACT_LEVEL
        assert not p._toggle.isChecked()

    def test_toggle_button_initial_state(self, panel):
        assert panel._toggle.text() == "调试"
        assert not panel._toggle.isChecked()
        assert "简洁模式" in panel._toggle.toolTip()
        assert "任务进度恒可见" in panel._toggle.toolTip()


class TestInternalChannel:
    """append 系列显式通道：可见性、格式、缓存。"""

    def test_append_info_visible_and_cached(self, panel):
        panel.append_info("任务状态 s1", category="系统")
        assert "任务状态 s1" in panel.toPlainText()  # 简洁模式默认可见
        assert any(
            lv == logging.INFO and not hb and "任务状态 s1" in m
            for lv, hb, m in panel._history
        )  # 入环形缓存且无心跳标记

    def test_line_format_has_stamp_level_category(self, panel):
        panel.append("文本 t1", level="warning", category="导出")
        line = next(m for *_, m in panel._history if "文本 t1" in m)
        assert "WARNING" in line
        assert "[导出]" in line
        assert "文本 t1" in line

    def test_append_success_tag_visible(self, panel):
        panel.append_success("导出完成行", category="导出")
        assert "SUCCESS" in panel.toPlainText()
        assert "导出完成行" in panel.toPlainText()

    def test_append_progress_alias_is_info(self, panel):
        panel.append_progress("周期进度 p1", category="导出")
        assert any(
            lv == logging.INFO and "周期进度 p1" in m for lv, _, m in panel._history
        )

    def test_warning_error_visible_in_compact(self, panel):
        panel.append_warning("回退 w1", category="导出")
        panel.append_error("失败 e1", category="字幕")
        text = panel.toPlainText()
        assert "回退 w1" in text and "失败 e1" in text
        assert "WARNING" in text and "ERROR" in text

    def test_append_mirrors_to_logging(self, panel, caplog):
        """镜像写 logging：原始文本经 log_panel logger 流向文件/控制台。"""
        with caplog.at_level(logging.INFO, logger=PANEL_LOGGER.name):
            panel.append_info("镜像存档 m1")
        assert any(
            r.name == PANEL_LOGGER.name
            and r.levelno == logging.INFO
            and r.getMessage() == "镜像存档 m1"  # 原始文本（不带面板行首格式）
            for r in caplog.records
        )


class TestLevelToggle:
    def test_channel_visible_across_toggles(self, panel):
        """进度通道两档均可见：切换不清空、先到缓存行与后到新行一致。"""
        panel.append_info("切换前 i1", category="导出")
        panel.set_debug_visible(True)
        panel.append_info("调试档 i2", category="导出")
        panel.set_debug_visible(False)
        text = panel.toPlainText()
        assert "切换前 i1" in text
        assert "调试档 i2" in text

    def test_debug_channel_gated(self, panel):
        """append_debug 诊断通道：简洁隐藏但入缓存，完整调试可见。"""
        panel.append_debug("诊断 d1", category="引擎")
        assert "诊断 d1" not in panel.toPlainText()  # 简洁隐藏
        assert any("诊断 d1" in m for *_, m in panel._history)  # 缓存保留
        panel.set_debug_visible(True)
        assert "诊断 d1" in panel.toPlainText()  # 完整调试复现
        panel.set_debug_visible(False)
        assert "诊断 d1" not in panel.toPlainText()  # 切回隐藏

    def test_toggle_keeps_root_level(self, panel, debug_root):
        before = debug_root.level
        panel.set_debug_visible(True)
        panel.set_debug_visible(False)
        assert debug_root.level == before  # 仅界面阈值，不动 logger 树/文件日志


class TestDecoupledFromRoot:
    """与 logging 上屏链路解耦：root 写入不再进面板，attach 路径已移除。"""

    def test_root_logging_does_not_reach_panel(self, panel):
        panel.append_info("通道行 c1")
        before = panel.toPlainText()
        TEST_LOGGER.info("root 写入 r1")
        TEST_LOGGER.warning("root 警告 r2")
        assert panel.toPlainText() == before  # 面板不追加任何 logging 内容
        assert "root 写入 r1" not in panel.toPlainText()
        assert "root 警告 r2" not in panel.toPlainText()

    def test_engine_heartbeat_does_not_reach_panel(self, panel):
        """引擎/渲染心跳走 logging 现状，不经通道进简洁视图。"""
        RENDER_LOGGER.info(
            "渲染循环心跳：window=embed presented=1", extra={"heartbeat": True}
        )
        assert "渲染循环心跳" not in panel.toPlainText()

    def test_attach_detach_path_removed(self):
        assert not hasattr(LogPanel, "attach_logger")
        assert not hasattr(LogPanel, "detach_logger")


class TestTaskProgressViaSignals:
    """长耗时任务进度经 worker 信号回主线程写入面板（离线驱动信号）。"""

    def test_export_worker_progress_reaches_panel(self, panel):
        worker = _ExportWorker([], "out.mp4")  # 不启动线程，不触真实导出
        worker.progress_logged.connect(
            lambda line: panel.append_progress(line, category="导出")
        )
        worker._on_progress(252, 600, "")  # 首次调用必过 0.5s 节流窗口
        text = panel.toPlainText()
        assert "导出进度 42.0%" in text
        assert "252/600 帧" in text
        assert "[导出]" in text
        assert any(
            lv == logging.INFO and "导出进度" in m for lv, _, m in panel._history
        )  # INFO 入缓存（切换等级不丢失的前提）

    def test_subtitle_worker_progress_reaches_panel(self, panel):
        worker = _SubtitleWorker([], "v.mp4", "v.ass", config=object())  # 不触 ASR
        worker.entry_progress.connect(
            lambda done, total, note: panel.append_progress(
                f"字幕进度：{done} / {total} {note}", category="字幕"
            )
        )
        worker.entry_progress.emit(
            3, 10, "切片 3/10 [6000-9000ms] ASR 完成，翻译中"
        )
        text = panel.toPlainText()
        assert "字幕进度：3 / 10" in text
        assert "切片 3/10 [6000-9000ms] ASR 完成，翻译中" in text  # 起止时间与阶段
        assert "[字幕]" in text

    def test_subtitle_abort_progress_reaches_panel(self, panel):
        """中断提示走同一进度通道（worker 经信号上报，不直接操作控件）。"""
        worker = _SubtitleWorker([], "v.mp4", "v.ass", config=object())
        worker.entry_progress.connect(
            lambda done, total, note: panel.append_progress(
                f"字幕进度：{done} / {total} {note}", category="字幕"
            )
        )
        worker.entry_progress.emit(4, 10, "制作已中断，保留已落盘 4 条")
        assert "制作已中断，保留已落盘 4 条" in panel.toPlainText()


class TestButtonRowLayout:
    def _show(self, panel: LogPanel, w: int, h: int) -> None:
        panel.resize(w, h)
        panel.show()
        QApplication.processEvents()

    def test_button_on_top_of_view_no_overlap(self, qapp, debug_root):
        p = LogPanel()
        self._show(p, 400, 300)
        try:
            assert p._toggle.geometry().bottom() <= p._view.geometry().top()
            assert not p._toggle.geometry().intersects(p._view.geometry())
        finally:
            p.close()

    def test_layout_stable_after_resize(self, qapp, debug_root):
        p = LogPanel()
        self._show(p, 800, 600)
        self._show(p, 300, 200)  # 缩小后布局仍稳定
        try:
            assert p._toggle.geometry().bottom() <= p._view.geometry().top()
        finally:
            p.close()

    def test_focus_policy_goes_to_text_view(self, qapp):
        p = LogPanel()
        p.set_text_focus_policy(Qt.FocusPolicy.ClickFocus)
        assert p._view.focusPolicy() == Qt.FocusPolicy.ClickFocus


class TestScopeLimited:
    def test_toggle_keeps_threshold_in_memory_only(self, panel):
        panel.set_debug_visible(True)
        assert panel._threshold == DEBUG_LEVEL  # 仅内存阈值
        panel.set_debug_visible(False)
        assert panel._threshold == COMPACT_LEVEL


class TestHistoryRing:
    def test_history_ring_cap(self, panel):
        for i in range(2100):
            panel.append_info(f"行 {i}")
        assert len(panel._history) == 2000
        assert "行 2099" in panel.toPlainText()
