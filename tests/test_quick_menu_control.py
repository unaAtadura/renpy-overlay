"""QuickMenu 布局锁定控制接口的离线验证：stub 窗口注入，不构造真实 QWidget。

控制模块三组接口（锁定/解锁截图布局、获取双击锁定窗口）只操作窗口池列表
与窗口的鸭子接口（``is_locked`` / ``hide`` / ``show``），用轻量 stub 即可
离线覆盖全部路径与边界（含创建/销毁的锁定态拦截、锁定期 show_all 守卫、
多轮往返一致性）。win32api 侧的穿透样式位计算抽为纯函数，配合 fake gui
验证调用链（通用能力保留，快捷键模式已改用整体隐藏方案）。
"""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QColor

from renpy_overlay import quick_menu as quick_menu_module
from renpy_overlay import win32api
from renpy_overlay.quick_menu import QuickMenu
from renpy_overlay.screenshot.frame_overlay import hint_geometry
from renpy_overlay.screenshot.window import needs_reshow_after_flags_change


class _StubWindow:
    """鸭子接口 stub：只实现 QuickMenu 控制接口触碰的窗口成员。"""

    def __init__(self, index: int = 0, locked: bool = False) -> None:
        self.index = index
        self._locked = locked
        self.visible = True
        self.closed = False
        self.deleted = False

    @property
    def is_locked(self) -> bool:
        return self._locked

    def hide(self) -> None:
        self.visible = False

    def show(self) -> None:
        self.visible = True

    def geometry(self) -> QRect:
        """锁定期线框快照源：Qt 全局逻辑坐标矩形。"""
        return QRect(100 + self.index * 30, 200, 300, 200)

    @property
    def border_color(self) -> QColor:
        """与真实 ScreenshotWindow 一致：property 访问（无括号）。"""
        return QColor(255, 50 + self.index * 10, 50)

    def close(self) -> None:
        self.closed = True

    def deleteLater(self) -> None:  # noqa: N802 - Qt 约定
        self.deleted = True


class _FakeOverlay:
    """框选提示窗口组鸭子接口 stub：记录快照与显示/隐藏/恢复次数。"""

    def __init__(self) -> None:
        self.frames: list | None = None
        self.shown = 0
        self.hidden = 0
        self.restored = 0
        self.destroyed = False
        self.showing = False  # 整组显示状态（is_showing 快照源）
        self.frame_visibility: list[tuple[int, bool]] = []  # 单帧压制记录

    def show_frames(self, frames) -> None:
        self.frames = list(frames)
        self.shown += 1
        self.showing = True

    def hide_overlay(self) -> None:
        self.hidden += 1
        self.showing = False

    def restore(self) -> None:
        self.restored += 1
        self.showing = True

    def is_showing(self) -> bool:
        """与真实 FrameOverlayLayer 同签名：任一提示窗口是否可见。"""
        return self.showing

    def set_frame_visible(self, index: int, visible: bool) -> None:
        """单帧显示/隐藏（自动化装填期压制），与真实 FrameOverlayLayer 同签名。"""
        self.frame_visibility.append((int(index), visible))

    def destroy(self) -> None:
        self.destroyed = True


def _menu_with(*locked_flags: bool, overlay: _FakeOverlay | None = None) -> QuickMenu:
    fake = overlay if overlay is not None else _FakeOverlay()
    menu = QuickMenu(
        on_capture_click=lambda w: None,
        on_open_history=lambda: None,
        frame_overlay_factory=lambda: fake,
    )
    menu._windows = [_StubWindow(i, flag) for i, flag in enumerate(locked_flags)]
    return menu


# ---- 锁定 / 解锁截图布局 -------------------------------------------------------


def test_lock_layout_hides_all_and_blocks_create_destroy():
    menu = _menu_with(False, True, False)
    menu.lock_layout()
    assert all(not w.visible for w in menu._windows)  # 整体隐藏：不显示、不可交互
    assert menu.create_window() is None  # 创建入口被屏蔽
    assert menu.destroy_latest() is False  # 销毁入口被屏蔽
    assert menu.window_count == 3  # 窗口池原样


def test_unlock_layout_restores_visibility_and_actions():
    menu = _menu_with(False)
    menu.lock_layout()
    menu.unlock_layout()
    assert menu._windows[0].visible  # 恢复显示且可交互（无穿透残留概念）
    assert menu._layout_locked is False


def test_layout_lock_and_unlock_are_idempotent():
    menu = _menu_with(False)
    menu.lock_layout()
    menu.lock_layout()  # 重复锁定无额外副作用：窗口保持隐藏
    assert not menu._windows[0].visible
    menu.unlock_layout()
    menu.unlock_layout()  # 重复解锁无额外副作用：窗口保持可见
    assert menu._windows[0].visible


def test_show_all_respects_layout_lock():
    # 快捷键模式（布局锁定）期间触发截图翻译：翻译流程 finally 中无条件
    # 调用的 show_all() 不得把被刻意隐藏的窗口放出来
    menu = _menu_with(False, True)
    menu.lock_layout()
    menu.show_all()
    assert all(not w.visible for w in menu._windows)  # 仍保持隐藏
    menu.unlock_layout()  # 解锁后恢复
    assert all(w.visible for w in menu._windows)


# ---- 框选提示窗口组：快照与生命周期 -------------------------------------------


def test_hint_geometry_matches_capture_region_exactly():
    # 提示窗口几何精确等于截屏区域：宽、高、位置零偏移（不外扩也不内缩），
    # 提示框所标示的范围即实际截屏范围
    assert hint_geometry((100, 200, 400, 300)) == QRect(100, 200, 300, 100)
    # 退化矩形钳制为最小 1x1，不产生负尺寸窗口
    assert hint_geometry((100, 200, 100, 200)) == QRect(100, 200, 1, 1)


def test_lock_layout_shows_frame_overlay_with_snapshot():
    menu = _menu_with(True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    assert overlay.shown == 1
    assert overlay.frames is not None and len(overlay.frames) == 1
    rect, color = overlay.frames[0]
    # 快照取窗口几何（全局逻辑坐标 ltrb）与各自边框色，零偏移——
    # 截屏抓取范围（隐藏窗口的 GetWindowRect）即该矩形本身
    assert rect == (100, 200, 400, 400)
    assert isinstance(color, QColor)


def test_capture_cycle_in_layout_lock_hides_and_restores_frame_overlay():
    # 快捷键模式（布局锁定）触发截图翻译的时序（需求）：hide_all（抓屏前，
    # request_screenshot_translation 调用）必须连同提示窗口一起隐藏，
    # show_all（抓屏收尾 finally 调用）立即恢复提示窗口且不放出截图窗口
    menu = _menu_with(True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    menu.hide_all()
    assert overlay.hidden == 1  # 抓屏前：线框随之隐藏，不进入识别截图
    menu.show_all()
    assert overlay.restored == 1  # 抓屏后：立即恢复显示
    assert all(not w.visible for w in menu._windows)  # 截图窗口仍保持隐藏


def test_capture_cycle_without_layout_lock_does_not_show_frame_overlay():
    # 普通单击截屏（未锁定布局）：hide_all/show_all 不复活提示窗口
    menu = _menu_with(False)
    menu.lock_layout()
    menu.unlock_layout()
    overlay = menu._frame_overlay
    menu.hide_all()
    menu.show_all()
    assert overlay.shown == 1
    assert menu._windows[0].visible  # 窗口照常恢复


def test_unlock_layout_hides_frame_overlay():
    menu = _menu_with(True)
    menu.lock_layout()
    menu.unlock_layout()
    overlay = menu._frame_overlay
    assert overlay.hidden == 1


def test_frame_overlay_roundtrip_is_stable():
    # 多轮开启/关闭：每次锁定重新快照并显示、解锁隐藏；再次锁定时
    # hide_all 侧先做一次幂等隐藏清理（防上一轮残留），随后重新显示
    menu = _menu_with(True)
    menu.lock_layout()  # 首次锁定：惰性创建覆盖层（hide_all 清理 1 次）
    overlay = menu._frame_overlay
    for _ in range(3):
        menu.unlock_layout()  # 解锁隐藏（+1）
        menu.lock_layout()  # hide_all 幂等清理（+1）+ 重新快照显示
        assert overlay.frames is not None  # 每次锁定重新快照
    assert overlay.shown == 4  # 首次 + 3 轮重开
    assert overlay.hidden == 6  # 首次锁定时覆盖层尚未创建（惰性），每轮（解锁 1 + 再锁定清理 1）× 3


def test_close_all_destroys_frame_overlay():
    menu = _menu_with(True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    menu.close_all()
    assert overlay.destroyed
    assert menu._frame_overlay is None


def test_layout_lock_keeps_double_click_lock_state_unchanged():
    # 三组接口彼此独立：布局锁定/解锁不得改变窗口的双击锁定状态
    menu = _menu_with(False, True)
    menu.lock_layout()
    assert [w.index for w in menu.locked_windows()] == [1]
    menu.unlock_layout()
    assert [w.index for w in menu.locked_windows()] == [1]


def test_unlocked_actions_not_blocked():
    # 未锁定态：动作层拦截不误伤正常销毁路径
    menu = _menu_with()
    menu._windows.append(_StubWindow(9))
    assert menu.destroy_latest() is True
    assert menu.window_count == 0
    assert menu._windows == []


# ---- 获取双击锁定的截图窗口 -----------------------------------------------------


def test_locked_windows_returns_only_double_click_locked_in_stack_order():
    menu = _menu_with(False, True, False, True)
    locked = menu.locked_windows()
    assert [w.index for w in locked] == [1, 3]  # 栈序，只含双击锁定窗口


def test_locked_windows_empty_when_none_locked():
    menu = _menu_with(False, False)
    assert menu.locked_windows() == []


def test_locked_windows_empty_pool():
    menu = _menu_with()
    assert menu.locked_windows() == []


# ---- win32api 鼠标穿透：纯函数 + fake gui 调用链 -------------------------------


def test_clickthrough_exstyle_pure_function():
    enabled = win32api.clickthrough_exstyle(0, True)
    assert enabled & win32api.WS_EX_TRANSPARENT
    assert enabled & win32api.WS_EX_LAYERED  # 微软要求两者同时存在才生效
    disabled = win32api.clickthrough_exstyle(enabled, False)
    assert not disabled & win32api.WS_EX_TRANSPARENT
    assert disabled & win32api.WS_EX_LAYERED  # 取消只清 TRANSPARENT，保留 LAYERED
    other_bit = 0x00000100  # 无关样式位不被覆盖
    assert win32api.clickthrough_exstyle(other_bit, True) & other_bit


def test_set_clickthrough_updates_exstyle_and_refreshes(monkeypatch):
    calls = {}

    class _FakeGui:
        def GetWindowLong(self, hwnd, index):
            calls["get"] = (hwnd, index)
            return 0x00000100

        def SetWindowLong(self, hwnd, index, value):
            calls["set"] = (hwnd, index, value)

        def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
            calls["refresh"] = (hwnd, flags)

    class _FakeCon:
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020

        @classmethod
        def expected_refresh_flags(cls) -> int:
            return (
                cls.SWP_NOMOVE
                | cls.SWP_NOSIZE
                | cls.SWP_NOZORDER
                | cls.SWP_NOACTIVATE
                | cls.SWP_FRAMECHANGED
            )

    monkeypatch.setattr(win32api, "_win32gui", lambda: _FakeGui())
    monkeypatch.setattr(win32api, "_win32con", lambda: _FakeCon())
    win32api.set_clickthrough(0xAA, True)
    assert calls["get"] == (0xAA, win32api.GWL_EXSTYLE)
    hwnd, index, value = calls["set"]
    assert (hwnd, index) == (0xAA, win32api.GWL_EXSTYLE)
    assert value == 0x00000100 | win32api.WS_EX_TRANSPARENT | win32api.WS_EX_LAYERED
    # 扩展样式修改后必须带 SWP_FRAMECHANGED 刷新，命中测试才会真正生效
    refresh_hwnd, flags = calls["refresh"]
    assert refresh_hwnd == 0xAA
    assert flags == _FakeCon.expected_refresh_flags()
    win32api.set_clickthrough(0xAA, False)
    assert calls["set"][2] == 0x00000100  # TRANSPARENT 被清除，其余保留
    # 取消穿透同样必须以 FRAMECHANGED 收尾刷新，命中测试缓存才能恢复可交互
    assert calls["refresh"] == (0xAA, _FakeCon.expected_refresh_flags())


# ---- 穿透与显示状态切换：补显示判定（纯函数） ----------------------------------


def test_needs_reshow_after_flags_change():
    # 可见 → 被标志修改隐藏：必须补显示（保持窗口持续可见的唯一补显路径）
    assert needs_reshow_after_flags_change(True, False) is True
    # 可见 → 仍可见：不补，避免多余的 show 造成闪烁
    assert needs_reshow_after_flags_change(True, True) is False
    # 原本隐藏：保持隐藏，不主动显示
    assert needs_reshow_after_flags_change(False, False) is False
    assert needs_reshow_after_flags_change(False, True) is False


# ---- 遥控器自动化：提示框单帧压制（future_截图翻译功能补充_遥控截图自动化） ----


def test_set_frame_suppressed_and_survives_capture_cycle():
    """装填期压制：抓屏的 hide_all/show_all 批量恢复不得复活被压制帧。"""
    menu = _menu_with(True, True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    overlay.frame_visibility.clear()
    menu.set_frame_suppressed(menu._windows[0], True)  # B 进入装填区域
    assert overlay.frame_visibility == [(0, False)]
    menu.hide_all()  # 抓屏前批量隐藏
    menu.show_all()  # 抓屏后恢复：被压制帧立即压回隐藏
    assert overlay.frame_visibility == [(0, False), (0, False)]
    menu.set_frame_suppressed(menu._windows[0], False)  # B 离开：恢复提示框
    assert overlay.frame_visibility[-1] == (0, True)


def test_set_frame_suppressed_unknown_window_ignored():
    menu = _menu_with(True)
    menu.set_frame_suppressed(object(), True)  # 不在窗口池内：静默忽略
    assert menu._suppressed_frames == set()


def test_unlock_layout_clears_suppressed_frames():
    menu = _menu_with(True)
    menu.lock_layout()
    menu.set_frame_suppressed(menu._windows[0], True)
    menu.unlock_layout()  # 解锁即快照失效：压制集合一并清除
    assert menu._suppressed_frames == set()


# ---- 托盘显隐接口（需求 future_托盘图标.txt：框选提示窗组与窗口池清单） -----------


def test_tray_frame_overlay_interfaces_noop_without_overlay():
    # 未锁定过布局（无覆盖层）：三方法 no-op 不抛异常
    menu = _menu_with()
    assert menu.frame_overlay_shown() is False
    menu.hide_frame_overlay()
    menu.show_frame_overlay()
    assert menu.frame_overlay_shown() is False


def test_tray_frame_overlay_restore_replays_suppressed_frames():
    # 托盘「显示窗口」恢复提示窗组：restore 后压制帧立即压回隐藏
    # （抓屏时序不得复活被压制的提示框，与 show_all 锁定期分支同序列）
    menu = _menu_with(True, True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    assert menu.frame_overlay_shown() is True
    menu.set_frame_suppressed(menu._windows[0], True)
    overlay.frame_visibility.clear()
    menu.hide_frame_overlay()
    assert overlay.hidden == 1
    assert menu.frame_overlay_shown() is False
    menu.show_frame_overlay()
    assert overlay.restored == 1
    assert overlay.frame_visibility == [(0, False)]  # 压制帧重放压回
    assert menu.frame_overlay_shown() is True


def test_tray_screenshot_windows_returns_snapshot_copy():
    menu = _menu_with(True, False)
    snapshot = menu.screenshot_windows()
    assert snapshot == menu._windows
    snapshot.clear()  # 外部修改副本不影响窗口池
    assert menu.window_count == 2


# ---- 遥控器自动化：菜单项顺序与分发（假 QMenu，不构造真实窗口） --------------------


class _FakeMenuAction:
    """QAction 替身：仅携带文案与禁用态。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 约定
        self.enabled = enabled


class _FakeMenu:
    """QMenu 替身：记录构建顺序，exec 返回预置文案对应的当次选中项。"""

    def __init__(self) -> None:
        self.items: list[object] = []
        self.choose_text: str | None = None  # exec 返回该文案对应的 action（当次构建）

    def windowFlags(self) -> int:
        return 0

    def setWindowFlags(self, flags) -> None:  # noqa: ANN001 - 测试替身
        pass

    def addAction(self, text: str) -> _FakeMenuAction:
        action = _FakeMenuAction(text)
        self.items.append(action)
        return action

    def addSeparator(self) -> str:
        self.items.append("separator")
        return "separator"

    def addMenu(self, title: str) -> _FakeMenu:
        sub = _FakeMenu()
        self.items.append(sub)
        return sub

    def exec(self, pos) -> object | None:  # noqa: N802, ANN001 - Qt 约定
        # show_context_menu 每次调用都重建 action，按文案取本次构建的最后一项
        matches = [
            item
            for item in self.items
            if isinstance(item, _FakeMenuAction) and item.text == self.choose_text
        ]
        return matches[-1] if matches else None


def _menu_labels(fake_menu: _FakeMenu) -> list[str]:
    """菜单构建序列 → 标签列表（分隔线/子菜单用占位名）。"""
    return [
        item.text if isinstance(item, _FakeMenuAction) else str(item)
        for item in fake_menu.items
    ]


def _automation_stub(calls: list[str]) -> SimpleNamespace:
    """自动化控制器替身：状态可切换，点击动作记录到 calls。"""
    return SimpleNamespace(
        enabled=False,
        remote_hidden=False,
        toggle_automation=lambda: calls.append("automation"),
        toggle_remote_hidden=lambda: calls.append("hidden"),
    )


def test_menu_places_remote_automation_items_between_lock_and_separator(monkeypatch):
    """布局顺序（需求）：锁鼠标区域之下、分隔线之上插入两项。"""
    fake_menu = _FakeMenu()
    monkeypatch.setattr(quick_menu_module, "QMenu", lambda: fake_menu)
    menu = _menu_with(True)
    menu.attach_mouse_lock(SimpleNamespace(enabled=False, toggle=lambda: None))
    menu.attach_remote_automation(_automation_stub([]))
    menu.show_context_menu(QPoint(0, 0))
    labels = _menu_labels(fake_menu)
    assert "遥控器自动化: 关" in labels
    assert "隐藏遥控器" in labels
    assert (
        labels.index("锁鼠标区域: 关")
        < labels.index("遥控器自动化: 关")
        < labels.index("隐藏遥控器")
        < labels.index("separator")
        < labels.index("录制视频")
    )


def test_menu_remote_automation_labels_follow_state(monkeypatch):
    fake_menu = _FakeMenu()
    monkeypatch.setattr(quick_menu_module, "QMenu", lambda: fake_menu)
    menu = _menu_with(True)
    stub = _automation_stub([])
    menu.attach_remote_automation(stub)
    menu.show_context_menu(QPoint(0, 0))
    assert "遥控器自动化: 关" in _menu_labels(fake_menu)
    assert "隐藏遥控器" in _menu_labels(fake_menu)
    stub.enabled = True
    stub.remote_hidden = True
    fake_menu.items.clear()
    menu.show_context_menu(QPoint(0, 0))
    labels = _menu_labels(fake_menu)
    assert "遥控器自动化: 开" in labels
    assert "显示遥控器" in labels


def test_menu_dispatch_toggles_automation_and_hidden(monkeypatch):
    calls: list[str] = []
    fake_menu = _FakeMenu()
    monkeypatch.setattr(quick_menu_module, "QMenu", lambda: fake_menu)
    menu = _menu_with(True)
    stub = _automation_stub(calls)
    menu.attach_remote_automation(stub)
    fake_menu.choose_text = "遥控器自动化: 关"
    menu.show_context_menu(QPoint(0, 0))
    fake_menu.choose_text = "隐藏遥控器"
    menu.show_context_menu(QPoint(0, 0))
    assert calls == ["automation", "hidden"]


def test_menu_without_remote_automation_has_no_items(monkeypatch):
    """未注入自动化控制器（宿主未组装）：菜单不出现两项，构建不报错。"""
    fake_menu = _FakeMenu()
    monkeypatch.setattr(quick_menu_module, "QMenu", lambda: fake_menu)
    menu = _menu_with(True)
    menu.show_context_menu(QPoint(0, 0))
    labels = _menu_labels(fake_menu)
    assert "遥控器自动化: 关" not in labels
    assert "隐藏遥控器" not in labels
