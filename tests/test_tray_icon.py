"""托盘图标模块的离线验证：stub 条目覆盖状态机全部路径（不构造 QWidget）。

状态机（TrayVisibilityController）与 QSystemTrayIcon 薄壳分离，本文件用
鸭子接口 stub 覆盖快照/恢复/幂等/校正式恢复/销毁防御等全部路径与边界；
``make_ro_icon`` 为纯绘制函数，冒烟验证惰性创建 QApplication（仅此一条
触 GUI，视觉本身人工验收）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from renpy_overlay.stream.follow import WindowFollowMixin
from renpy_overlay.tray_icon import (
    HIDE_LABEL,
    SHOW_LABEL,
    ManagedItem,
    TrayIconController,
    TrayVisibilityController,
    make_ro_icon,
)


class _StubItem:
    """鸭子接口 stub：记录 hide/restore 调用，可见性可外部改写。"""

    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self.hide_calls = 0
        self.restore_calls = 0

    def is_visible(self) -> bool:
        return self.visible

    def hide(self) -> None:
        self.hide_calls += 1
        self.visible = False

    def restore(self) -> None:
        self.restore_calls += 1
        self.visible = True


class _DeletedItem:
    """已销毁 Qt 对象形态：触碰即抛 RuntimeError（销毁竞态防御路径）。"""

    def is_visible(self) -> bool:
        raise RuntimeError("wrapped C/C++ object deleted")

    def hide(self) -> None:
        raise RuntimeError("wrapped C/C++ object deleted")

    def restore(self) -> None:
        raise RuntimeError("wrapped C/C++ object deleted")


def _wrap(stub: _StubItem | _DeletedItem) -> ManagedItem:
    """stub → 受管条目（与宿主组装方式一致：绑定方法注入三回调）。"""
    return ManagedItem(stub.is_visible, stub.hide, stub.restore)


def _controller(items: list) -> tuple[TrayVisibilityController, list[str]]:
    """构造状态机 + 文案记录器（set_label 回调收集历次文案）。"""
    labels: list[str] = []
    return TrayVisibilityController(lambda: items, labels.append), labels


# ---- 初始态 ------------------------------------------------------------------


def test_initial_state_is_showing_label():
    controller, labels = _controller([])
    assert controller.hidden is False
    assert controller.menu_label() == HIDE_LABEL
    assert labels == []  # 构造不回写文案


# ---- 隐藏分支 ------------------------------------------------------------------


def test_hide_all_hides_only_visible_items():
    visible_a, visible_b, hidden_c = _StubItem(), _StubItem(), _StubItem(False)
    controller, labels = _controller(
        [_wrap(visible_a), _wrap(hidden_c), _wrap(visible_b)]
    )
    controller.toggle()
    assert visible_a.hide_calls == 1
    assert visible_b.hide_calls == 1
    # 隐藏前不可见的条目（预构建后台/布局锁定/压制帧）不被触碰
    assert hidden_c.hide_calls == 0
    assert controller.hidden is True
    assert labels == [SHOW_LABEL]


def test_hide_all_tolerates_deleted_item():
    controller, _labels = _controller([_wrap(_DeletedItem()), _wrap(_StubItem())])
    controller.hide_all()  # is_visible 抛 RuntimeError：按不可见处理，不传播
    assert controller.hidden is True


# ---- 恢复分支（按快照） ---------------------------------------------------------


def test_restore_restores_only_snapshot_visible():
    visible, hidden_before = _StubItem(), _StubItem(False)
    controller, labels = _controller([_wrap(visible), _wrap(hidden_before)])
    controller.hide_all()
    controller.toggle()
    assert visible.restore_calls == 1
    # 需求：不得恢复隐藏前本就不可见的窗口（不复活）
    assert hidden_before.restore_calls == 0
    assert hidden_before.hide_calls == 0
    assert controller.hidden is False
    assert labels == [SHOW_LABEL, HIDE_LABEL]


def test_restore_suppresses_items_revived_while_hidden():
    """校正式恢复：隐藏期间被业务流程复活的窗口压回，不放任可见。"""
    visible = _StubItem()
    revived = _StubItem(False)
    items = [_wrap(visible)]
    controller, _labels = _controller(items)
    controller.hide_all()
    # 隐藏期间业务流程把新窗口/被藏窗口 show 出来，且进入受管清单
    revived.visible = True
    items.append(_wrap(revived))
    controller.toggle()
    assert revived.restore_calls == 0
    assert revived.hide_calls == 1  # 不在快照：压回隐藏
    assert visible.restore_calls == 1


def test_restore_when_provider_rebuilds_items_each_call():
    """核心回归（实测缺陷 211630）：宿主清单每次现取**重建** ManagedItem，

    以条目对象 id 匹配快照永不命中导致「放回 0 项」（日志 211630：流式窗
    看似恢复实为跟随循环短路解除后的自动重显，框选提示窗组与遥控 A/B
    则永久丢失）。修复后放回直接作用于快照条目，不依赖身份匹配。
    """
    visible_a, visible_b, hidden_c = _StubItem(), _StubItem(), _StubItem(False)
    labels: list[str] = []

    def provider() -> list[ManagedItem]:
        # 模拟宿主 _tray_managed_items：每次调用重建条目对象（不缓存）
        return [_wrap(visible_a), _wrap(hidden_c), _wrap(visible_b)]

    controller = TrayVisibilityController(provider, labels.append)
    controller.hide_all()
    controller.toggle()
    assert visible_a.restore_calls == 1
    assert visible_b.restore_calls == 1
    assert hidden_c.restore_calls == 0  # 隐藏前不可见：不复活
    assert labels == [SHOW_LABEL, HIDE_LABEL]


def test_restore_suppresses_revived_before_restoring_snapshot():
    """两步式恢复顺序：先压回复活窗口、后放回快照窗口。

    压回阶段放回尚未发生（快照窗口此刻仍隐藏，不会被误判为复活而压回），
    顺序即正确性；条目对象同样每次重建（与宿主行为一致）。
    """
    events: list[str] = []

    class _EventStub(_StubItem):
        def __init__(self, name: str, visible: bool = True) -> None:
            super().__init__(visible)
            self._name = name

        def hide(self) -> None:
            super().hide()
            events.append(f"hide:{self._name}")

        def restore(self) -> None:
            super().restore()
            events.append(f"restore:{self._name}")

    snapshot_visible = _EventStub("snap")
    revived = _EventStub("revived", visible=False)

    def provider() -> list[ManagedItem]:
        return [_wrap(snapshot_visible), _wrap(revived)]

    controller = TrayVisibilityController(provider, lambda text: None)
    controller.hide_all()
    revived.visible = True  # 隐藏期间被业务流程复活
    controller.toggle()
    # hide:snap 是隐藏分支的动作；随后压回复活窗口发生在放回之前
    #（此刻快照窗口仍隐藏，不会被误判为复活而压回）
    assert events == ["hide:snap", "hide:revived", "restore:snap"]
    assert snapshot_visible.visible is True
    assert revived.visible is False


def test_restore_skips_items_destroyed_while_hidden():
    """隐藏期间被销毁的条目（不在现取清单）自然跳过，不抛异常。"""
    destroyed = _StubItem()
    survivor = _StubItem(False)
    controller, _labels = _controller([_wrap(destroyed)])
    controller.hide_all()
    controller._items_provider = lambda: [_wrap(survivor)]
    controller.toggle()  # destroyed 不在现取清单：跳过；survivor 不可见：无操作
    assert controller.hidden is False


def test_restore_tolerates_deleted_item_in_current_list():
    controller, _labels = _controller([_wrap(_StubItem())])
    controller.hide_all()
    # 现取清单混入已销毁对象：is_visible/restore 抛 RuntimeError 均不传播
    controller._items_provider = lambda: [_wrap(_DeletedItem()), _wrap(_StubItem())]
    controller.toggle()
    assert controller.hidden is False


# ---- 幂等与往返 ------------------------------------------------------------------


def test_hide_all_is_idempotent_when_already_hidden():
    visible = _StubItem()
    controller, labels = _controller([_wrap(visible)])
    controller.hide_all()
    controller.hide_all()  # 重复触发隐藏：no-op
    assert visible.hide_calls == 1
    assert controller.hidden is True
    assert labels == [SHOW_LABEL]


def test_restore_is_noop_when_not_hidden():
    visible = _StubItem()
    controller, labels = _controller([_wrap(visible)])
    controller.restore()  # 非隐藏态直接恢复：no-op
    assert visible.restore_calls == 0
    assert controller.hidden is False
    assert labels == []


def test_empty_items_roundtrip():
    controller, _labels = _controller([])
    controller.hide_all()
    assert controller.hidden is True
    controller.toggle()
    assert controller.hidden is False  # 空清单往返不抛异常


def test_multiple_rounds_keep_state_consistent():
    visible = _StubItem()
    controller, labels = _controller([_wrap(visible)])
    for _ in range(2):
        controller.hide_all()
        assert visible.hide_calls >= 1 and not visible.visible
        controller.toggle()
        assert visible.visible and controller.hidden is False
    assert labels == [SHOW_LABEL, HIDE_LABEL, SHOW_LABEL, HIDE_LABEL]


# ---- 外壳对宿主的状态出口（实测缺陷回归：renpy_overlay_20261012_211126.log） ----


class _FollowHostStub:
    """WindowFollowMixin 的最小 stub 宿主：仅托盘短路判定所需成员。

    ``skip_injection=False`` 时 ``_follow_once`` 的常规路径首步是
    ``_resolve_target``；stub 返回 0（无游戏窗口）走 _update_title +
    _hide_if_visible 并记录定位调用次数，用于断言短路是否生效。
    """

    _screenshot_suppress = False
    _context_menu_open = False
    skip_injection = False
    target_pid = 0

    def __init__(self, tray_hidden: bool) -> None:
        self._tray = SimpleNamespace(hidden=tray_hidden)
        self.located = 0

    def _resolve_target(self) -> int:
        self.located += 1
        return 0

    def _update_title(self, text: str) -> None:
        pass

    def _hide_if_visible(self) -> None:
        pass


def test_tray_icon_controller_proxies_hidden_to_visibility():
    """回归守卫：follow 循环从外壳读 ``self._tray.hidden``（follow.py），

    外壳必须代理到内层状态机，否则 AttributeError 使跟随循环每 tick
    报错瘫痪（实测：日志 211126 每约 0.2 秒一次直到 Ctrl+C）。
    object.__new__ 绕过 GUI 构造，仅验证属性出口契约。
    """
    controller = TrayIconController.__new__(TrayIconController)
    controller.visibility = TrayVisibilityController(lambda: [], lambda text: None)
    assert controller.hidden is False
    controller.visibility.hide_all()
    assert controller.hidden is True


def test_follow_loop_short_circuits_while_tray_hidden():
    """托盘隐藏期间 follow 循环短路：不定位不重显示（计划 4.4 语义）。

    隐藏态下 _resolve_target 不被触达；非隐藏态正常走定位路径。
    """
    hidden_host = _FollowHostStub(tray_hidden=True)
    WindowFollowMixin._follow_once(hidden_host)
    assert hidden_host.located == 0  # 短路：未触达定位逻辑
    active_host = _FollowHostStub(tray_hidden=False)
    WindowFollowMixin._follow_once(active_host)
    assert active_host.located == 1  # 正常路径：进入定位


# ---- 图标绘制冒烟（唯一触 GUI 的测试） -------------------------------------------


def test_make_ro_icon_smoke():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    icon = make_ro_icon()
    assert not icon.isNull()
    for size in (16, 32):
        pixmap = icon.pixmap(size, size)
        assert not pixmap.isNull()
        # 高 DPI 下 QIcon 按设备像素比返回更大的物理 pixmap（合法），仅验证非空档位
        assert pixmap.width() >= size and pixmap.height() >= size
