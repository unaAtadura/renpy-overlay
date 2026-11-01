"""日志面板等级切换单测（offscreen；root 级别与文件/控制台两路不受影响）。

覆盖：默认简洁模式（DEBUG/心跳隐藏但入缓存）、切换即时全量刷新（双向）、
心跳标记日志仅调试模式可见（含历史复现；引擎指标与 renderer_sdl2 渲染
循环心跳同走统一谓词）、按钮位于文本区上方独立一行
（不重叠、缩放稳定）、按钮选中态与 tooltip、仅内存不落盘（不动 logger
树）、环形历史上限、卸载后不再转发。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import logging

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from renpy_overlay.video.clip.log_panel import COMPACT_LEVEL, DEBUG_LEVEL, LogPanel

TEST_LOGGER = logging.getLogger("renpy_overlay.video.clip.test.log_panel")
RENDER_LOGGER = logging.getLogger("renpy_overlay.video.clip.player.renderer_sdl2")


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
    p = LogPanel()
    p.attach_logger()
    yield p
    p.detach_logger()


class TestDefaultCompact:
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
        assert "DEBUG" in panel._toggle.toolTip()

    def test_debug_hidden_info_shown(self, panel):
        TEST_LOGGER.debug("调试细节 d1")
        TEST_LOGGER.info("任务进度 i1")
        text = panel.toPlainText()
        assert "任务进度 i1" in text
        assert "调试细节 d1" not in text

    def test_debug_cached_while_hidden(self, panel):
        TEST_LOGGER.debug("缓存 d1")
        assert any("缓存 d1" in m for *_, m in panel._history)


class TestToggleDebug:
    def test_shows_cached_debug_immediately(self, panel):
        TEST_LOGGER.debug("早期细节 d1")
        TEST_LOGGER.info("进度 i1")
        panel.set_debug_visible(True)
        text = panel.toPlainText()
        assert "早期细节 d1" in text  # 简洁期缓存的 DEBUG 切换后即时可见
        assert "进度 i1" in text

    def test_toggle_back_hides_debug_keeps_info(self, panel):
        TEST_LOGGER.debug("d1")
        TEST_LOGGER.info("i1")
        panel.set_debug_visible(True)
        panel.set_debug_visible(False)
        text = panel.toPlainText()
        assert "d1" not in text
        assert "i1" in text

    def test_order_preserved_after_refresh(self, panel):
        TEST_LOGGER.info("i1")
        TEST_LOGGER.debug("d1")
        TEST_LOGGER.info("i2")
        panel.set_debug_visible(True)
        text = panel.toPlainText()
        assert text.index("i1") < text.index("d1") < text.index("i2")

    def test_new_records_follow_threshold(self, panel):
        panel.set_debug_visible(True)
        TEST_LOGGER.debug("d2")
        assert "d2" in panel.toPlainText()
        panel.set_debug_visible(False)
        TEST_LOGGER.debug("d3")
        assert "d3" not in panel.toPlainText()

    def test_button_state_and_tooltip_follow(self, panel):
        panel.set_debug_visible(True)
        assert panel._toggle.isChecked()
        assert "完整调试模式" in panel._toggle.toolTip()
        panel.set_debug_visible(False)
        assert not panel._toggle.isChecked()
        assert "简洁模式" in panel._toggle.toolTip()

    def test_click_toggles_threshold(self, panel):
        panel._toggle.setChecked(True)  # 模拟点击（toggled → 阈值切换）
        assert panel.is_debug_visible()
        panel._toggle.setChecked(False)
        assert not panel.is_debug_visible()


class TestHeartbeatVisibility:
    """心跳标记日志（logging extra ``heartbeat``）：仅完整调试模式可见。"""

    def test_heartbeat_hidden_in_compact(self, panel):
        TEST_LOGGER.info("心跳行 hb1", extra={"heartbeat": True})
        TEST_LOGGER.info("普通进度 i1")
        text = panel.toPlainText()
        assert "心跳行 hb1" not in text  # INFO 心跳也完全隐藏（不刷屏）
        assert "普通进度 i1" in text

    def test_heartbeat_cached_while_hidden(self, panel):
        TEST_LOGGER.info("心跳行 hb1", extra={"heartbeat": True})
        assert any(
            "心跳行 hb1" in m and hb for _, hb, m in panel._history
        )  # 缓存带标记，格式化文本不受 extra 影响

    def test_heartbeat_visible_in_debug_including_cached(self, panel):
        TEST_LOGGER.info("心跳行 hb1", extra={"heartbeat": True})
        panel.set_debug_visible(True)
        assert "心跳行 hb1" in panel.toPlainText()  # 切换前缓存的历史心跳复现

    def test_heartbeat_hidden_again_after_toggle_back(self, panel):
        TEST_LOGGER.info("心跳行 hb1", extra={"heartbeat": True})
        panel.set_debug_visible(True)
        panel.set_debug_visible(False)
        assert "心跳行 hb1" not in panel.toPlainText()  # 切回即时隐藏

    def test_debug_level_heartbeat_also_gated(self, panel):
        TEST_LOGGER.debug("调试心跳 hb2", extra={"heartbeat": True})
        assert "调试心跳 hb2" not in panel.toPlainText()
        panel.set_debug_visible(True)
        assert "调试心跳 hb2" in panel.toPlainText()

    def test_non_heartbeat_info_unaffected(self, panel):
        TEST_LOGGER.info("普通进度 i1")
        panel.set_debug_visible(True)
        panel.set_debug_visible(False)
        assert "普通进度 i1" in panel.toPlainText()  # 非心跳 INFO 始终可见

    def test_extra_not_in_formatted_text(self, panel):
        TEST_LOGGER.info("心跳行 hb1", extra={"heartbeat": True})
        assert all(
            "heartbeat" not in m for *_, m in panel._history
        )  # extra 不进格式化文本 → 文件日志内容不变


class TestRendererSdl2Heartbeat:
    """renderer_sdl2 渲染循环心跳：同 extra 标记走统一谓词，无特例分支。"""

    HB_MSG = "渲染循环心跳：window=embed presented=1 cmd=idle"

    def test_heartbeat_hidden_in_compact(self, panel):
        RENDER_LOGGER.info(self.HB_MSG, extra={"heartbeat": True})
        RENDER_LOGGER.info("rebind 命令已投递（mode=embed hwnd=1）")
        text = panel.toPlainText()
        assert "渲染循环心跳" not in text  # 简洁模式完全隐藏（不刷屏）
        assert "rebind 命令已投递" in text  # 非心跳日志可见性不变

    def test_toggle_debug_shows_then_compact_hides(self, panel):
        RENDER_LOGGER.info(self.HB_MSG, extra={"heartbeat": True})
        panel.set_debug_visible(True)
        assert "渲染循环心跳" in panel.toPlainText()  # 切换前缓存历史心跳复现
        panel.set_debug_visible(False)
        assert "渲染循环心跳" not in panel.toPlainText()  # 切回即时隐藏

    def test_heartbeat_still_reaches_root_handlers(self, panel, caplog):
        # 仅界面过滤：心跳记录仍以 INFO 产生并流向 root 各 handler
        # （文件/控制台 handler 级别未动，照常落盘）
        with caplog.at_level(logging.INFO, logger="renpy_overlay.video.clip.player.renderer_sdl2"):
            RENDER_LOGGER.info(self.HB_MSG, extra={"heartbeat": True})
        assert any(
            r.name == "renpy_overlay.video.clip.player.renderer_sdl2"
            and r.levelno == logging.INFO
            and "渲染循环心跳" in r.getMessage()
            for r in caplog.records
        )

    def test_heartbeat_extra_not_in_formatted_text(self, panel):
        RENDER_LOGGER.info(self.HB_MSG, extra={"heartbeat": True})
        assert any(
            "渲染循环心跳" in m and hb and "heartbeat" not in m
            for _, hb, m in panel._history
        )  # 缓存带标记；extra 不进格式化文本 → 文件内容不变


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
            assert not p._toggle.geometry().intersects(p._view.geometry())
        finally:
            p.close()

    def test_focus_policy_goes_to_text_view(self, qapp):
        p = LogPanel()
        p.set_text_focus_policy(Qt.FocusPolicy.ClickFocus)
        assert p._view.focusPolicy() == Qt.FocusPolicy.ClickFocus


class TestScopeLimited:
    def test_handler_level_always_debug(self, panel):
        assert panel._handler.level == DEBUG_LEVEL  # 记录恒入缓存，过滤在主线程侧

    def test_toggle_keeps_root_level(self, panel, debug_root):
        before = debug_root.level
        panel.set_debug_visible(True)
        panel.set_debug_visible(False)
        assert debug_root.level == before  # 仅界面阈值，不动 logger 树/文件日志


class TestHistoryAndDetach:
    def test_history_ring_cap(self, panel):
        for i in range(2100):
            TEST_LOGGER.info("行 %d", i)
        assert len(panel._history) == 2000
        assert "行 2099" in panel.toPlainText()

    def test_detach_stops_forwarding(self, qapp, debug_root):
        p = LogPanel()
        p.attach_logger()
        TEST_LOGGER.info("挂载期 i1")
        p.detach_logger()
        TEST_LOGGER.info("卸载期 i2")
        assert "挂载期 i1" in p.toPlainText()
        assert "卸载期 i2" not in p.toPlainText()
