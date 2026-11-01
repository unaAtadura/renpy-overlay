"""预构建入口链路的离线验证：跳过注入模式可打开、路径取值、阻塞与唤回。

入口 ``request_prebuild``（``InjectionFlowMixin``）经 stub 宿主驱动；
``PrebuildWindow`` 被 monkeypatch 为记录器替身，不构造真实 Qt 窗口
（离线测试约定）。目录回溯纯函数用例见 ``test_prebuild_formatting.py``。
"""

from __future__ import annotations

import pytest

import renpy_overlay.prebuild_window as prebuild_window_module
from renpy_overlay.stream import injection_flow


class _StubAsrRt:
    """实时识别控制器 stub：仅 enabled 状态。"""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled


class _StubWindow:
    """PrebuildWindow 替身：记录构造参数与 show/raise 次数。"""

    instances: list[_StubWindow] = []

    def __init__(self, cfg, initial_game_dir: str = "", on_closed=None) -> None:
        self.cfg = cfg
        self.initial_game_dir = initial_game_dir
        self.on_closed = on_closed
        self.show_count = 0
        self.closed_by = None
        _StubWindow.instances.append(self)

    def show(self) -> None:
        self.show_count += 1

    def raise_(self) -> None:  # noqa: N802 - 与 QWidget 同名
        pass

    def activateWindow(self) -> None:  # noqa: N802
        pass

    def close(self) -> None:
        if callable(self.on_closed):
            self.on_closed()


class _StubHost:
    """鸭子接口宿主：只提供 request_prebuild 访问的属性与方法。"""

    def __init__(self, game_dir: str = "", skip_injection: bool = False) -> None:
        self._closed = False
        self.skip_injection = skip_injection
        self._game_dir = game_dir
        self._config = object()  # 仅透传给替身，不消费其字段
        self._asr_rt = _StubAsrRt()
        self._prebuild_window = None
        self._queue = None  # 本测试路径不触发 mark_injected
        self._title_updates: list[str] = []

    def _update_title(self, text: str) -> None:
        self._title_updates.append(text)


@pytest.fixture()
def stub_prebuild_window(monkeypatch):
    """替换 PrebuildWindow 为替身并清空实例记录（局部导入每次取模块属性）。"""
    _StubWindow.instances.clear()
    monkeypatch.setattr(prebuild_window_module, "PrebuildWindow", _StubWindow)
    return _StubWindow.instances


def _make_flow(host: _StubHost):
    """把 request_prebuild 绑定到 stub 宿主上（Mixin 方法直接借用）。"""
    return injection_flow.InjectionFlowMixin.request_prebuild.__get__(host)


def test_skip_injection_mode_opens_window_without_dir(stub_prebuild_window):
    """跳过注入模式：入口不再拦截，窗口可打开且目录行留空（占位提示）。"""
    host = _StubHost(game_dir="", skip_injection=True)
    _make_flow(host)()
    assert len(stub_prebuild_window) == 1, "跳过注入模式不应在入口处拦截"
    window = stub_prebuild_window[0]
    assert window.initial_game_dir == "", "无预置目录时目录行应留空占位"
    assert window.show_count == 1
    assert host._title_updates == [], "不应再有「需注入游戏后可用」提示文案"


def test_injection_mode_passes_resolved_game_root(stub_prebuild_window, tmp_path):
    """注入模式：exe 所在目录被回溯为含 game/ 的游戏根（路径取值正确性）。"""
    root = tmp_path / "ActingLessons-1.0.2-pc"
    exe_dir = root / "lib" / "windows-i686"
    exe_dir.mkdir(parents=True)
    (root / "game").mkdir(parents=True)
    host = _StubHost(game_dir=str(exe_dir))
    _make_flow(host)()
    assert len(stub_prebuild_window) == 1
    assert stub_prebuild_window[0].initial_game_dir == str(root), (
        "预构建窗口应拿到游戏根目录而非 exe 所在目录"
    )


def test_injection_mode_unresolvable_falls_back_to_original(stub_prebuild_window, tmp_path):
    """非标准布局（无 game/ 祖先）：回退原值，由窗口提示无效后用户改选。"""
    flat = tmp_path / "flat"
    flat.mkdir()
    host = _StubHost(game_dir=str(flat))
    _make_flow(host)()
    assert stub_prebuild_window[0].initial_game_dir == str(flat)


def test_asr_rt_blocking_rule_unchanged(stub_prebuild_window):
    """实时语音识别开启期间阻塞预构建的既有规则维持原样。"""
    host = _StubHost(game_dir="D:/Games/Sample")
    host._asr_rt = _StubAsrRt(enabled=True)
    _make_flow(host)()
    assert stub_prebuild_window == [], "实时识别开启期间仍应阻塞"
    assert host._prebuild_window is None


def test_existing_window_is_activated_not_recreated(stub_prebuild_window):
    """窗口已打开：唤回置顶（show +1）而不重建实例。"""
    host = _StubHost(game_dir="")
    flow = _make_flow(host)
    flow()
    flow()
    assert len(stub_prebuild_window) == 1
    assert stub_prebuild_window[0].show_count == 2


def test_window_closed_clears_host_reference(stub_prebuild_window):
    """窗口关闭回调清宿主引用：下次入口重新创建而非唤回旧实例。"""
    host = _StubHost(game_dir="")
    flow = _make_flow(host)
    flow()
    window = host._prebuild_window
    window.close()  # 触发 on_closed
    assert host._prebuild_window is None
    flow()
    assert len(stub_prebuild_window) == 2


def test_closed_host_ignores_request(stub_prebuild_window):
    """宿主已关闭：直接返回，不创建窗口。"""
    host = _StubHost(game_dir="")
    host._closed = True
    _make_flow(host)()
    assert stub_prebuild_window == []
