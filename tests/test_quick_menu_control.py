"""QuickMenu 布局锁定控制接口的离线验证：stub 窗口注入，不构造真实 QWidget。

控制模块三组接口（锁定/解锁截图布局、获取双击锁定窗口）只操作窗口池列表
与窗口的鸭子接口（``is_locked`` / ``hide`` / ``show``），用轻量 stub 即可
离线覆盖全部路径与边界（含创建/销毁的锁定态拦截、锁定期 show_all 守卫、
多轮往返一致性）。win32api 侧的穿透样式位计算抽为纯函数，配合 fake gui
验证调用链（通用能力保留，快捷键模式已改用整体隐藏方案）。
"""

from __future__ import annotations

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor

from renpy_overlay import win32api
from renpy_overlay.quick_menu import QuickMenu
from renpy_overlay.screenshot.frame_overlay import FRAME_OFFSET_PX, offset_frame_rect
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
    """框选提示覆盖层鸭子接口 stub：记录快照与显示/隐藏次数。"""

    def __init__(self) -> None:
        self.frames: list | None = None
        self.shown = 0
        self.hidden = 0
        self.deleted = False

    def show_frames(self, frames) -> None:
        self.frames = list(frames)
        self.shown += 1

    def hide_overlay(self) -> None:
        self.hidden += 1

    def deleteLater(self) -> None:  # noqa: N802 - Qt 约定
        self.deleted = True


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


# ---- 框选提示覆盖层：快照与生命周期 -------------------------------------------


def test_offset_frame_rect_expands_outside_region():
    # 线框中心线外扩 FRAME_OFFSET_PX（留缝 3 + 半线宽 1）：
    # 内沿距截图区域 3px（含抗锯齿溢出仍在区域外），不污染识别截图
    rect = (100, 200, 400, 300)
    assert offset_frame_rect(rect, FRAME_OFFSET_PX) == (96, 196, 404, 304)
    left, top, right, bottom = offset_frame_rect(rect)
    assert left < rect[0] and top < rect[1] and right > rect[2] and bottom > rect[3]


def test_lock_layout_shows_frame_overlay_with_snapshot():
    menu = _menu_with(True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    assert overlay.shown == 1
    assert overlay.frames is not None and len(overlay.frames) == 1
    rect, color = overlay.frames[0]
    # 快照取窗口几何（全局逻辑坐标 ltrb）与各自边框色
    assert rect == (100, 200, 400, 400)
    assert isinstance(color, QColor)
    # 线框整体绘制在截图区域之外（偏移规则）
    fl, ft, fr, fb = offset_frame_rect(rect)
    assert fl < rect[0] and ft < rect[1] and fr > rect[2] and fb > rect[3]


def test_unlock_layout_hides_frame_overlay():
    menu = _menu_with(True)
    menu.lock_layout()
    menu.unlock_layout()
    overlay = menu._frame_overlay
    assert overlay.hidden == 1


def test_frame_overlay_roundtrip_is_stable():
    # 多轮开启/关闭：每次锁定重新快照并显示、解锁隐藏，无累积状态
    menu = _menu_with(True)
    menu.lock_layout()  # 首次锁定：惰性创建覆盖层
    overlay = menu._frame_overlay
    for round_no in range(1, 4):
        menu.unlock_layout()
        assert overlay.hidden == round_no
        menu.lock_layout()
        assert overlay.frames is not None  # 每次锁定重新快照
    assert overlay.shown == 4  # 首次 + 3 轮重开


def test_close_all_destroys_frame_overlay():
    menu = _menu_with(True)
    menu.lock_layout()
    overlay = menu._frame_overlay
    menu.close_all()
    assert overlay.deleted
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
