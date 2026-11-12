"""WindowFollowMixin 跟随循环的离线单元测试（不构造真实窗口）。

覆盖场景（实测缺陷回归：日志 renpy_overlay_20261107_221504，锁定态下
游戏窗口最小化隐藏悬浮窗后，恢复前台时锁定分支只重申置顶、永不重显，
窗口进入永久不可见状态，托盘快照 0 项）：

- 锁定态 + 最小化 → 隐藏；恢复 → 锁定分支自愈重显（先 Qt show 后重申）；
- 锁定态且已可见：只重申不重复 _set_visible（幂等）；
- 拖动态：不重显不重申；
- 托盘隐藏 / 截图抑制 / 右键菜单三短路对锁定态重显的守卫不回归；
- 无游戏窗口（not hwnd）时隐藏行为不回归；
- skip_injection 路径托盘短路守卫不回归；
- 最小化隐藏节流 debug 日志与锁定态重显 info 日志。

stub 宿主模式沿用 test_tray_icon.py 的 _FollowHostStub：以类属性提供
Mixin 所需最小成员，直接调用 ``WindowFollowMixin._follow_once(host)``；
``is_minimized`` 经 monkeypatch 替换为受控开关。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from renpy_overlay.stream import follow
from renpy_overlay.stream.follow import WindowFollowMixin

HWND = 0x1234


class _FollowHostStub:
    """跟随循环常规路径所需的最小宿主 stub（离线，不触 GUI/win32）。

    ``_reassert_topmost`` 覆写为记录器：真实实现会触 win32api 与快捷菜单
    宿主；事件经 ``calls`` 记录顺序，供断言「先重显后重申」的时序。
    """

    _closed = False
    _screenshot_suppress = False
    _context_menu_open = False
    skip_injection = False
    target_pid = 0
    _locked = True
    _dragging = False
    _visible = True
    _layout_pending_position = False  # layout 恢复的一次性定位标志（默认已消费）
    _last_topmost_log = 0.0
    _last_minimize_log = 0.0

    def __init__(self, tray_hidden: bool = False) -> None:
        self._tray = SimpleNamespace(hidden=tray_hidden)
        self.calls: list[str] = []  # 事件序列："locate"/"show"/"hide"/"reassert"

    def _resolve_target(self) -> int:
        self.calls.append("locate")
        return HWND

    def _log_minimized_hidden(self) -> None:
        # 转发真实实现：stub 非 Mixin 子类，节流日志逻辑由日志用例验证
        WindowFollowMixin._log_minimized_hidden(self)

    def _hide_if_visible(self) -> None:
        # 转发真实实现：依赖 stub 的 _visible / _set_visible 记录隐藏事件
        WindowFollowMixin._hide_if_visible(self)

    def _set_visible(self, visible: bool) -> None:
        self._visible = visible
        self.calls.append("show" if visible else "hide")

    def _reassert_topmost(self) -> None:
        self.calls.append("reassert")

    def _update_title(self, text: str) -> None:
        pass


@pytest.fixture()
def minimized(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 follow 模块引用的 win32api.is_minimized 换成受控开关。"""
    state = {"value": False}
    monkeypatch.setattr(follow.win32api, "is_minimized", lambda hwnd: state["value"])
    return state


class _ListHandler(logging.Handler):
    """直连列表的捕获 handler（独立于 caplog，避免传播链上重复计数）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def follow_log(caplog: pytest.LogCaptureFixture) -> logging.Handler:
    """在 follow 的 logger 上直挂独立捕获 handler 并放开到 DEBUG。

    caplog 自身 handler 挂在 root：若把它再直挂到子 logger，传播链通畅时
    同一记录会被同一 handler 处理两遍，故用独立 _ListHandler。级别经
    caplog 设置（测试结束自动恢复），不受项目 propagate 配置影响。
    """
    handler = _ListHandler()
    logger = logging.getLogger("renpy_overlay.stream_window")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


# ---- 核心回归：锁定态「最小化→隐藏 / 恢复→自愈重显」 -----------------------------


def test_locked_hidden_by_minimize_then_restored_on_unminimize(minimized):
    """锁定态 + 游戏最小化 → 隐藏且不重申；恢复 → 先重显再重申（修复点）。"""
    host = _FollowHostStub()

    minimized["value"] = True
    WindowFollowMixin._follow_once(host)
    assert host._visible is False
    assert "hide" in host.calls
    assert "reassert" not in host.calls  # 最小化分支提前返回，锁定重申被跳过

    minimized["value"] = False
    WindowFollowMixin._follow_once(host)
    assert host.calls[-2:] == ["show", "reassert"]  # 先 Qt show() 后 set_topmost
    assert host._visible is True


def test_locked_visible_does_not_repeat_set_visible(minimized):
    """锁定态且已可见：每 tick 只重申置顶，不重复 _set_visible（幂等）。"""
    host = _FollowHostStub()

    WindowFollowMixin._follow_once(host)
    WindowFollowMixin._follow_once(host)
    assert host.calls == ["locate", "reassert", "locate", "reassert"]


def test_dragging_neither_shows_nor_reasserts(minimized):
    """拖动态：不重显（不受修复影响）也不重申置顶。"""
    host = _FollowHostStub()
    host._dragging = True
    host._visible = False

    WindowFollowMixin._follow_once(host)
    assert host.calls == ["locate"]


# ---- 短路守卫不回归：三标志均先于锁定分支 ----------------------------------------


def test_tray_hidden_blocks_reshow_while_locked(minimized):
    """托盘隐藏期间锁定态不重显不定位（短路在最前）。"""
    host = _FollowHostStub(tray_hidden=True)
    host._visible = False

    WindowFollowMixin._follow_once(host)
    assert host.calls == []


def test_screenshot_suppress_blocks_reshow_while_locked(minimized):
    """截图抑制期间锁定态不重显（抓屏时序不受修复影响）。"""
    host = _FollowHostStub()
    host._screenshot_suppress = True
    host._visible = False

    WindowFollowMixin._follow_once(host)
    assert host.calls == []


def test_context_menu_blocks_reshow_while_locked(minimized):
    """右键菜单打开期间锁定态不重显。"""
    host = _FollowHostStub()
    host._context_menu_open = True
    host._visible = False

    WindowFollowMixin._follow_once(host)
    assert host.calls == []


# ---- 其它隐藏/路径行为保持 -------------------------------------------------------


def test_no_target_window_hides_even_locked(minimized):
    """无游戏窗口（not hwnd）：锁定态同样隐藏（既有行为不回归）。"""
    host = _FollowHostStub()

    def _no_target() -> int:
        host.calls.append("locate")
        return 0

    host._resolve_target = _no_target

    WindowFollowMixin._follow_once(host)
    assert host._visible is False
    assert host.calls == ["locate", "hide"]


def test_unlocked_reshow_via_normal_path_still_works(minimized, monkeypatch):
    """解锁态走常规路径重显 + move_window 的行为不回归（修复只动锁定分支）。"""
    host = _FollowHostStub()
    host._locked = False
    host._visible = False
    host.width = 400
    host.height = 120
    host._title_h = 24
    host._title_gap = 6
    host.dock = "bottom-center"
    host._user_offset = None
    host._body_hwnd = 0xA
    host._title_hwnd = 0xB
    monkeypatch.setattr(follow.win32api, "window_rect", lambda hwnd: (0, 0, 800, 600))
    moved: list[tuple] = []
    monkeypatch.setattr(follow.win32api, "move_window", lambda *a, **k: moved.append(a))

    WindowFollowMixin._follow_once(host)
    assert host.calls == ["locate", "show"]
    assert host._visible is True
    assert len(moved) == 2  # 正文窗 + 标题窗各一次定位


def test_skip_injection_tray_hidden_still_short_circuits():
    """skip_injection 路径的托盘短路守卫不回归（该路径无重显修复点）。"""

    class _SkipInjectionStub(_FollowHostStub):
        skip_injection = True

        def _resolve_target(self) -> int:  # pragma: no cover - 不应被触达
            raise AssertionError("托盘隐藏期间不应触达 skip_injection 路径")

    host = _SkipInjectionStub(tray_hidden=True)
    WindowFollowMixin._follow_once(host)
    assert host.calls == []


# ---- 新增日志的可观测性 ---------------------------------------------------------


def test_minimized_hidden_log_fires_once_per_state_change(minimized, follow_log):
    """最小化隐藏节流日志：状态变化时刻打一次；持续最小化期间不刷屏。"""
    host = _FollowHostStub()

    minimized["value"] = True
    WindowFollowMixin._follow_once(host)
    WindowFollowMixin._follow_once(host)  # 已隐藏：无状态变化，不打

    texts = [r.getMessage() for r in follow_log.records if "最小化" in r.getMessage()]
    assert len(texts) == 1
    assert "locked=True" in texts[0]


def test_locked_reshow_logs_info(minimized, follow_log):
    """锁定态自愈重显打 INFO 日志（便于下次复现快速定位）。"""
    host = _FollowHostStub()
    host._visible = False

    WindowFollowMixin._follow_once(host)

    assert any("恢复显示" in r.getMessage() for r in follow_log.records)


def test_reassert_log_contains_visible_state(monkeypatch, follow_log):
    """重申置顶 debug 日志附带 visible 状态（区分可见/不可见矛盾现象）。

    直调真实 ``_reassert_topmost`` 验证日志格式（经 _follow_once 的锁定
    分支会先自愈重显，visible 必为 True，无法覆盖不可见侧的日志取值）。
    """
    host = _FollowHostStub()
    host._visible = False
    host._body_hwnd = 0xA
    host._title_hwnd = 0xB
    host._quick_menu = SimpleNamespace(reassert_topmost=lambda: None)
    host._remote_screenshot = SimpleNamespace(reassert_topmost=lambda: None)
    monkeypatch.setattr(follow.win32api, "set_topmost", lambda hwnd: None)

    WindowFollowMixin._reassert_topmost(host)

    texts = [r.getMessage() for r in follow_log.records if "重申置顶" in r.getMessage()]
    assert len(texts) == 1
    assert "visible=False" in texts[0]
