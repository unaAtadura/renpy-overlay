"""锁鼠标区域控制器的离线验证：纯函数 + stub 注入，不构造真实 QWidget。

覆盖：逃脱提示文案、开关状态机（创建/销毁范围窗口、逃脱热键注册与注销）、
双击锁定生效（ClipCursor 施加、提示窗口等几何替换、标题窗提示含配置快捷
键）、坐标分流（物理喂 ClipCursor、Qt 逻辑喂提示窗）、逃脱热键解锁（解除
限制、销毁提示窗、恢复范围窗、无提示）、低级鼠标钩子双保险（出界移动钳
回、出界点击拦截、随锁定装/卸）、注册失败与快捷键解析失败降级、shutdown
幂等清理，以及与截图布局锁定的隔离（lock_layout / unlock_layout /
locked_windows 不触及本功能窗口）。WM_HOTKEY 到 _dispatch 的映射逻辑经
_dispatch 直接驱动。
"""

from __future__ import annotations

import ctypes
import inspect

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QColor

from renpy_overlay import win32api
from renpy_overlay.quick_menu import QuickMenu
from renpy_overlay.screenshot.hotkeys import MSG_HOTKEY_REGISTER_FAILED
from renpy_overlay.screenshot.mouse_lock import (
    DEFAULT_MOUSE_ESCAPE_HOTKEY,
    MOUSE_LOCK_HOTKEY_ID,
    MOUSE_LOCK_OFF_TEXT,
    MOUSE_LOCK_ON_TEXT,
    OUTCOME_CLAMP,
    OUTCOME_PASS,
    OUTCOME_SWALLOW,
    MouseLockController,
    clamp_point_to_rect,
    escape_message,
    mouse_hook_outcome,
)
from renpy_overlay.screenshot.mouse_lock_window import (
    MASK_COLOR,
    MouseLockRegionWindow,
)

#: 与 window_visual 的边缘检测阈值语义一致（stub 窗口不参与，仅供阅读参考）
_REGION_RECT = (100, 200, 500, 400)
#: 范围窗口 geometry() 的 Qt 全局逻辑矩形：刻意与物理矩形不同值，
# 用于验证坐标分流（物理喂 ClipCursor、逻辑喂提示窗，不得混用）
_LOGICAL_RECT = (80, 60, 880, 660)


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """QTimer 是 QObject：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


# ---- escape_message：提示文案（纯函数） ----------------------------------------


def test_escape_message_contains_configured_hotkey():
    # 提示中的逃脱快捷键展示配置文件的实际值（需求）
    assert escape_message("ctrl+alt+o") == "鼠标活动范围已被限制，逃脱快捷键ctrl+alt+o"
    assert escape_message("f9") == "鼠标活动范围已被限制，逃脱快捷键f9"


def test_default_escape_hotkey():
    assert DEFAULT_MOUSE_ESCAPE_HOTKEY == "ctrl+alt+o"


# ---- stub：范围窗口 / 提示窗口 / clip 记录器 -----------------------------------


class _FakeGeometry:
    """geometry() 返回值的鸭子 stub（Qt QRect 风格只读访问器）。"""

    def __init__(self, x: int, y: int, w: int, h: int) -> None:
        self._rect = (x, y, w, h)

    def x(self) -> int:
        return self._rect[0]

    def y(self) -> int:
        return self._rect[1]

    def width(self) -> int:
        return self._rect[2]

    def height(self) -> int:
        return self._rect[3]


class _StubRegionWindow:
    """范围窗口鸭子接口 stub：记录 show/hide/close 与移动，模拟 hwnd。

    geometry()（Qt 逻辑）与 hwnd 的 GetWindowRect（物理）刻意返回不同
    数值，用于验证控制器的坐标分流。
    """

    def __init__(self, on_lock=None) -> None:
        self.on_lock = on_lock
        self.hwnd = 4321
        self.shown = 0
        self.hidden = 0
        self.closed = 0
        self.deleted = 0
        self.moved_to: tuple[int, int] | None = None

    def show(self) -> None:
        self.shown += 1

    def hide(self) -> None:
        self.hidden += 1

    def close(self) -> None:
        self.closed += 1

    def deleteLater(self) -> None:  # noqa: N802 - 与 Qt 同名
        self.deleted += 1

    def move(self, x: int, y: int) -> None:
        self.moved_to = (x, y)

    def width(self) -> int:
        return 800

    def height(self) -> int:
        return 600

    def geometry(self) -> _FakeGeometry:
        return _FakeGeometry(80, 60, 800, 600)  # 对应 _LOGICAL_RECT

    def devicePixelRatioF(self) -> float:
        return 1.25  # 模拟 DPR≠1 的屏幕（坐标混用会在此暴露）

    def double_click(self) -> None:
        """模拟用户在窗口内双击（走真实回调链路）。"""
        if callable(self.on_lock):
            self.on_lock(self)


class _StubIndicator:
    """提示窗口鸭子接口 stub：记录 show_region 几何与销毁调用。"""

    def __init__(self) -> None:
        self.hwnd = 8765
        self.region: tuple[int, int, int, int] | None = None
        self.shown = 0
        self.hidden_indicator = 0
        self.deleted = 0

    def show_region(self, rect) -> None:
        self.region = tuple(rect)
        self.shown += 1

    def hide_indicator(self) -> None:
        self.hidden_indicator += 1

    def deleteLater(self) -> None:  # noqa: N802 - 与 Qt 同名
        self.deleted += 1


class _ClipRecorder:
    """ClipCursor stub：记录每次施加/解除调用，可指定返回值。"""

    def __init__(self, result: bool = True) -> None:
        self.calls: list[tuple[int, int, int, int] | None] = []
        self.result = result

    def __call__(self, rect) -> bool:
        self.calls.append(tuple(rect) if rect is not None else None)
        return self.result


def _make_controller(
    *,
    escape_hotkey: str = DEFAULT_MOUSE_ESCAPE_HOTKEY,
    clip_result=True,
    get_clip_cursor=None,
):
    """构造 (controller, notices, regions, indicators, clip) 五元组。"""
    notices: list[str] = []
    regions: list[_StubRegionWindow] = []
    indicators: list[_StubIndicator] = []
    clip = _ClipRecorder(clip_result)

    def region_factory(on_lock=None):
        window = _StubRegionWindow(on_lock)
        regions.append(window)
        return window

    def indicator_factory():
        window = _StubIndicator()
        indicators.append(window)
        return window

    def window_rect_fn(hwnd):
        assert hwnd == 4321
        return _REGION_RECT

    def default_readback():
        return _REGION_RECT  # 读回一致：模拟限制正常生效（离线不触达 Win32）

    controller = MouseLockController(
        notices.append,
        escape_hotkey=escape_hotkey,
        region_window_factory=region_factory,
        indicator_factory=indicator_factory,
        clip_cursor=clip,
        window_rect_fn=window_rect_fn,
        get_clip_cursor=get_clip_cursor or default_readback,
    )
    return controller, notices, regions, indicators, clip


@pytest.fixture()
def registered_ids(monkeypatch):
    """假热键注册表：记录注册 id，unregister 同步移除。"""
    ids: list[int] = []
    monkeypatch.setattr(
        win32api, "register_hotkey", lambda key_id, _mod, _vk: ids.append(key_id) or True
    )
    monkeypatch.setattr(win32api, "unregister_hotkey", ids.remove)
    return ids


@pytest.fixture()
def mouse_hook(monkeypatch):
    """假低级鼠标钩子：记录安装/卸载与放行，call_next 返回标记值 7。"""
    state = {
        "handle": 1234,
        "installed": 0,
        "uninstalled": [],
        "proc": None,
        "next_calls": [],
    }

    def fake_set(proc):
        state["installed"] += 1
        state["proc"] = proc
        return state["handle"]

    def fake_unset(hook):
        state["uninstalled"].append(hook)

    def fake_next(hook, ncode, wparam, lparam):
        state["next_calls"].append((hook, ncode, wparam))
        return 7

    monkeypatch.setattr(win32api, "set_mouse_hook", fake_set)
    monkeypatch.setattr(win32api, "unset_mouse_hook", fake_unset)
    monkeypatch.setattr(win32api, "call_next_mouse_hook", fake_next)
    return state


def _hook_lparam(x: int, y: int):
    """构造钩子 lParam：返回 (整型指针, MSLLHOOKSTRUCT) 便于读回改写结果。"""
    info = win32api.MSLLHOOKSTRUCT()
    info.pt.x, info.pt.y = x, y
    lparam = ctypes.cast(ctypes.pointer(info), ctypes.c_void_p).value
    return lparam, info


# ---- 状态机：开关 ------------------------------------------------------------


def test_enable_creates_region_window_and_registers_escape_hotkey(registered_ids):
    controller, notices, regions, _indicators, clip = _make_controller()
    controller.set_enabled(True)
    assert controller.enabled is True
    assert controller.engaged is False
    assert len(regions) == 1
    assert regions[0].shown == 1  # 范围窗口已显示等待拖拽
    assert callable(regions[0].on_lock)  # 双击回调已接线
    assert registered_ids == [MOUSE_LOCK_HOTKEY_ID]  # 逃脱热键已注册
    assert clip.calls == []  # 未锁定：不施加任何限制
    assert notices == []  # 需求只规定注册失败时提示，开启本身无提示
    assert MOUSE_LOCK_OFF_TEXT == "锁鼠标区域: 关"


def test_disable_destroys_windows_and_unregisters(registered_ids):
    controller, _notices, regions, indicators, clip = _make_controller()
    controller.set_enabled(True)
    controller._engage(regions[0])  # 先进入锁定态
    controller.set_enabled(False)
    assert controller.enabled is False
    assert controller.engaged is False
    assert clip.calls[-1] is None  # 锁定态下关闭：必须解除限制
    assert regions[0].closed == 1 and regions[0].deleted == 1  # 范围窗销毁
    assert indicators[0].hidden_indicator == 1 and indicators[0].deleted == 1
    assert registered_ids == []  # 逃脱热键已注销


def test_set_enabled_is_idempotent(registered_ids):
    controller, _notices, regions, _indicators, clip = _make_controller()
    controller.set_enabled(True)
    controller.set_enabled(True)  # 重复开启无额外副作用
    controller.set_enabled(False)
    controller.set_enabled(False)  # 重复关闭无额外副作用
    assert len(regions) == 1
    assert regions[0].closed == 1
    assert registered_ids == []
    assert clip.calls == []


def test_toggle_flips_enabled(registered_ids):
    controller, _notices, _regions, _indicators, _clip = _make_controller()
    controller.toggle()
    assert controller.enabled is True
    controller.toggle()
    assert controller.enabled is False


# ---- 状态机：双击锁定生效 ------------------------------------------------------


def test_engage_clips_region_and_swaps_indicator_for_region(registered_ids):
    controller, notices, regions, indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()  # 模拟双击（走真实回调链路）
    assert clip.calls == [_REGION_RECT]  # 限制矩形取 GetWindowRect（物理像素）
    assert controller.engaged is True
    assert regions[0].hidden == 1  # 范围窗口已隐藏
    assert indicators[0].shown == 1
    assert indicators[0].region == _LOGICAL_RECT  # 提示窗取 geometry()（Qt 逻辑）
    assert notices == [escape_message(DEFAULT_MOUSE_ESCAPE_HOTKEY)]


def test_engage_twice_reuses_indicator(registered_ids):
    controller, _notices, regions, indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    controller._escape()
    regions[0].double_click()  # 解锁后可再次双击锁定
    assert clip.calls == [_REGION_RECT, None, _REGION_RECT]
    assert len(indicators) == 2  # 逃脱销毁旧提示窗后重建
    assert controller.engaged is True


def test_engage_clip_failure_aborts_silently(registered_ids):
    controller, notices, regions, indicators, clip = _make_controller(clip_result=False)
    controller.set_enabled(True)
    regions[0].double_click()
    assert controller.engaged is False  # 施加失败：不进入锁定态
    assert regions[0].hidden == 0  # 范围窗口保持可拖拽
    assert indicators == []  # 不创建提示窗
    assert notices == []  # 失败仅记日志（窗口仍可再次双击重试）


def test_engage_ignored_when_disabled_or_foreign_window(registered_ids):
    controller, _notices, regions, _indicators, clip = _make_controller()
    controller._engage(_StubRegionWindow(None))  # 未开启
    controller.set_enabled(True)
    controller._engage(_StubRegionWindow(None))  # 非自己的范围窗口
    assert clip.calls == []
    assert regions[0].hidden == 0


def test_engage_splits_physical_and_logical_coordinate_spaces(registered_ids):
    """坐标分流防回归：物理喂 ClipCursor、Qt 逻辑喂提示窗，不得混用。

    缺陷实测（logs/renpy_overlay_20260922_211428.log）：把 GetWindowRect
    物理值当 Qt 逻辑值喂 setGeometry，提示窗在 DPR≠1 屏幕上放大 DPR 倍
    并右下偏移；而 ClipCursor 实际限制在原范围窗内，与提示框视觉错位，
    看起来像“鼠标能移出显示的框”。
    """
    controller, _notices, regions, indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    assert clip.calls == [_REGION_RECT]  # ClipCursor：始终用 GetWindowRect 物理像素
    assert indicators[0].region == _LOGICAL_RECT  # 提示窗：始终用 geometry() 逻辑坐标
    assert indicators[0].region != _REGION_RECT  # DPR=1.25 stub 下两套坐标必然不同


def test_reassert_logs_warning_when_clip_overwritten(registered_ids, caplog):
    """ClipCursor 被外部改写（游戏 SDL 抓取等）：重申恢复并留告警。"""
    controller, _notices, regions, _indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    clip.calls.clear()
    controller._get_clip_cursor = lambda: (0, 0, 100, 100)  # 模拟外部改写后的生效值
    with caplog.at_level("WARNING", logger="renpy_overlay.screenshot.mouse_lock"):
        controller._reassert()
    assert clip.calls == [_REGION_RECT]  # 重申把预期矩形重新施加
    assert any("不一致" in record.getMessage() for record in caplog.records)


def test_reassert_silent_when_readback_matches(registered_ids, caplog):
    """读回与预期一致（未被改写）：不产生告警。"""
    controller, _notices, regions, _indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    clip.calls.clear()
    with caplog.at_level("WARNING", logger="renpy_overlay.screenshot.mouse_lock"):
        controller._reassert()
    assert clip.calls == [_REGION_RECT]
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# ---- 状态机：逃脱快捷键 -------------------------------------------------------


def test_escape_hotkey_releases_and_restores_region(registered_ids):
    controller, notices, regions, indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    controller._dispatch(MOUSE_LOCK_HOTKEY_ID)  # 模拟 WM_HOTKEY
    assert clip.calls[-1] is None  # 限制已解除
    assert controller.engaged is False
    assert indicators[0].hidden_indicator == 1 and indicators[0].deleted == 1
    assert regions[0].shown == 2  # 恢复显示（enable 1 次 + 逃脱后 1 次）
    assert notices == [escape_message(DEFAULT_MOUSE_ESCAPE_HOTKEY)]  # 解锁无新提示（需求）


def test_escape_without_engagement_is_noop(registered_ids):
    controller, notices, regions, _indicators, clip = _make_controller()
    controller.set_enabled(True)
    controller._dispatch(MOUSE_LOCK_HOTKEY_ID)  # 未锁定：幂等无操作
    assert clip.calls == []
    assert controller.engaged is False
    assert regions[0].shown == 1
    assert notices == []


def test_dispatch_ignored_when_disabled(registered_ids):
    controller, notices, _regions, _indicators, clip = _make_controller()
    controller._dispatch(MOUSE_LOCK_HOTKEY_ID)  # 功能未开启：任何热键不生效
    assert clip.calls == []
    assert notices == []


def test_dispatch_ignores_foreign_hotkey_ids(registered_ids):
    controller, notices, regions, _indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    controller._dispatch(MOUSE_LOCK_HOTKEY_ID + 1)  # 快捷键模式的窗口键等无关 id
    assert controller.engaged is True  # 不影响锁定态


# ---- 注册失败与非法快捷键降级 ---------------------------------------------------


def test_register_failure_notifies_once_and_feature_still_works(monkeypatch):
    monkeypatch.setattr(win32api, "register_hotkey", lambda *_args: False)  # 被占用
    controller, notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    assert controller.enabled is True  # 注册失败不阻碍功能开启
    assert notices == [MSG_HOTKEY_REGISTER_FAILED]  # 仅提示一次统一文案
    regions[0].double_click()  # 双击锁定照常生效（仅逃脱热键不可用）
    assert controller.engaged is True
    controller.shutdown()


def test_invalid_escape_hotkey_notifies(monkeypatch):
    monkeypatch.setattr(win32api, "register_hotkey", lambda *_args: True)
    ids: list[int] = []
    monkeypatch.setattr(win32api, "unregister_hotkey", ids.remove)
    controller, notices, _regions, _indicators, _clip = _make_controller(
        escape_hotkey="not a key"
    )
    controller.set_enabled(True)
    assert notices == [MSG_HOTKEY_REGISTER_FAILED]  # 解析失败同样提示
    assert controller._registered is False
    controller.shutdown()


# ---- shutdown：宿主退出清理（幂等） ---------------------------------------------


def test_shutdown_releases_clip_and_unregisters_idempotent(registered_ids):
    controller, _notices, regions, indicators, clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    controller.shutdown()
    assert clip.calls == [_REGION_RECT, None]  # 锁定态下退出必须解除限制
    assert controller.engaged is False and controller.enabled is False
    assert registered_ids == []  # 逃脱热键已注销
    assert regions[0].closed == 1 and regions[0].deleted == 1
    assert indicators[0].deleted == 1
    controller.shutdown()  # 幂等：重复调用无额外副作用
    assert clip.calls == [_REGION_RECT, None]


# ---- 与截图布局锁定的隔离（需求约束） -------------------------------------------


class _FakeOverlay:
    """框选提示窗口组 stub（QuickMenu 布局锁定用）。"""

    def show_frames(self, frames) -> None:
        self.frames = frames

    def hide_overlay(self) -> None:
        pass

    def restore(self) -> None:
        pass

    def destroy(self) -> None:
        pass


def test_quick_menu_layout_lock_does_not_touch_mouse_lock_windows():
    """lock_layout / unlock_layout / locked_windows 不得隐藏或捕获本功能窗口。"""
    menu = QuickMenu(
        on_capture_click=lambda w: None,
        on_open_history=lambda: None,
        frame_overlay_factory=_FakeOverlay,
    )
    controller, _notices, regions, _indicators, _clip = _make_controller()
    menu.attach_mouse_lock(controller)
    controller.set_enabled(True)
    regions[0].double_click()  # 进入锁定态（范围窗隐藏、由提示窗替换）
    menu.lock_layout()
    menu.unlock_layout()
    assert regions[0].shown == 1  # 布局锁定/解锁未让范围窗重新显示
    assert regions[0].closed == 0  # 也未销毁
    assert menu.locked_windows() == []  # 控制器窗口不出现在截图窗口池
    assert MOUSE_LOCK_ON_TEXT == "锁鼠标区域: 开"


# ---- 范围窗口命中性防回归（静态断言，不构造真实 QWidget） -----------------------


def test_mask_color_has_hittable_alpha():
    """内部遮罩 alpha 必须大于 0：Windows layered window 对 alpha=0 像素穿透。

    范围窗口内部完全透明时，双击锁定与内部拖动全部失效（仅边缘因边框
    像素可拉伸）；遮罩色与截图窗口同惯例（QColor(0, 0, 0, 20)），观感
    足够低不压暗游戏画面。
    """
    assert MASK_COLOR.alpha() > 0
    assert MASK_COLOR == QColor(0, 0, 0, 20)


def test_region_window_paints_mask_and_never_clickthrough():
    """防回归：paintEvent 必须绘制遮罩；范围窗口不得带任何整窗穿透标志。

    整窗穿透（WindowTransparentForInput / set_clickthrough）是锁定态提示
    窗口的专利，范围窗口必须全程可命中才能双击锁定与内部整体拖动。
    """
    paint_source = inspect.getsource(MouseLockRegionWindow.paintEvent)
    assert "MASK_COLOR" in paint_source
    assert "drawRect" in paint_source
    init_source = inspect.getsource(MouseLockRegionWindow.__init__)
    assert "WindowTransparentForInput" not in init_source
    assert "set_clickthrough" not in init_source


# ---- 低级鼠标钩子：纯函数决策 ---------------------------------------------------


def test_lowlevel_mouse_proc_signature_matches_win32():
    """WH_MOUSE_LL 原型必须与 LowLevelMouseProc 三参完全一致。

    缺 nCode 时 ctypes 按两参解栈调用 bound method，每次鼠标事件都抛
    TypeError（missing 1 required positional argument: 'lparam'）并被
    ctypes 忽略（只打 stderr），钩子钳回/拦截逻辑静默失效（实测缺陷）。
    """
    argtypes = getattr(win32api.LOWLEVEL_MOUSE_PROC, "_argtypes_", None)
    assert argtypes is not None and len(argtypes) == 3  # nCode + wParam + lParam


def test_on_mouse_hook_exception_degrades_to_pass_through(
    mouse_hook, registered_ids, caplog
):
    """回调体内（含转发）异常必须就地捕获：返回 0 放行并记日志，不外传播。"""
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    lparam, _info = _hook_lparam(600, 300)  # 矩形外（右）

    def broken_outcome(*_args):
        raise RuntimeError("钩子决策异常")

    import renpy_overlay.screenshot.mouse_lock as ml

    original_outcome = ml.mouse_hook_outcome
    ml.mouse_hook_outcome = broken_outcome  # 模拟决策路径抛异常
    try:
        with caplog.at_level("ERROR", logger="renpy_overlay.screenshot.mouse_lock"):
            result = controller._on_mouse_hook(0, win32api.WM_LBUTTONDOWN, lparam)
    finally:
        ml.mouse_hook_outcome = original_outcome
    assert result == 0  # “未处理”语义：系统继续正常分发，不向 ctypes 传播
    assert mouse_hook["next_calls"] == []  # 异常路径不再转发钩子链
    assert any("钩子回调异常" in r.getMessage() for r in caplog.records)


def test_call_next_mouse_hook_accepts_wide_lparam():
    """回归：CallNextHookEx 已声明 64 位 argtypes，指针宽 lparam 不再溢出。

    缺陷实测：无 argtypes 时 ctypes 按 C int（32 位）转换 lparam 指针值，
    抛 OverflowError 且钩子链不被转发（终端刷屏、其它低级钩子被跳过）。
    """
    result = win32api.call_next_mouse_hook(0x1234, 0, 0x0201, 0x7FFF_FFFF_FFFF)
    assert isinstance(result, int)


def test_clamp_point_to_rect_nearest_inside():
    rect = (100, 200, 500, 400)
    assert clamp_point_to_rect((50, 300), rect) == (100, 300)  # 左出界钳到左边缘
    assert clamp_point_to_rect((600, 300), rect) == (499, 300)  # 右出界钳到右缘内
    assert clamp_point_to_rect((300, 100), rect) == (300, 200)  # 上出界
    assert clamp_point_to_rect((300, 500), rect) == (300, 399)  # 下出界
    assert clamp_point_to_rect((0, 0), rect) == (100, 200)  # 双向出界取最近内点
    assert clamp_point_to_rect((300, 300), rect) == (300, 300)  # 界内不变


def test_mouse_hook_outcome_decisions():
    rect = (100, 200, 500, 400)
    # 出界移动：钳回；出界点击/滚轮：吞掉；界内任意事件：放行
    assert mouse_hook_outcome(win32api.WM_MOUSEMOVE, (50, 300), rect) == OUTCOME_CLAMP
    assert (
        mouse_hook_outcome(win32api.WM_LBUTTONDOWN, (50, 300), rect) == OUTCOME_SWALLOW
    )
    assert mouse_hook_outcome(win32api.WM_MOUSEWHEEL, (600, 300), rect) == OUTCOME_SWALLOW
    assert mouse_hook_outcome(win32api.WM_RBUTTONDOWN, (600, 300), rect) == OUTCOME_SWALLOW
    assert mouse_hook_outcome(win32api.WM_LBUTTONDOWN, (300, 300), rect) == OUTCOME_PASS
    assert mouse_hook_outcome(win32api.WM_MOUSEMOVE, (300, 300), rect) == OUTCOME_PASS


# ---- 低级鼠标钩子：控制器联动（随锁定装/卸 + 回调三分支） ------------------------


def test_engage_installs_mouse_hook_and_escape_uninstalls(mouse_hook, registered_ids):
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    assert mouse_hook["installed"] == 0  # 仅开启未锁定：不装钩子
    regions[0].double_click()
    assert mouse_hook["installed"] == 1
    assert controller._mouse_hook == 1234
    assert mouse_hook["proc"] is not None  # 回调已由实例持有（防垃圾回收）
    controller._dispatch(MOUSE_LOCK_HOTKEY_ID)  # 逃脱热键
    assert mouse_hook["uninstalled"] == [1234]
    assert controller._mouse_hook is None


def test_teardown_uninstalls_mouse_hook(mouse_hook, registered_ids):
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    controller.shutdown()
    assert mouse_hook["uninstalled"] == [1234]


def test_on_mouse_hook_clamps_outside_move(mouse_hook, registered_ids):
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    lparam, info = _hook_lparam(50, 400)  # 矩形 (100,200,500,400) 外（左下）
    result = controller._on_mouse_hook(0, win32api.WM_MOUSEMOVE, lparam)
    assert (info.pt.x, info.pt.y) == (100, 399)  # 已改写为矩形内最近点
    assert result == 7  # 修正后仍传入钩子链（系统按修正后位置处理）


def test_on_mouse_hook_swallows_outside_click(mouse_hook, registered_ids):
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    lparam, info = _hook_lparam(600, 300)  # 矩形外（右侧）
    result = controller._on_mouse_hook(0, win32api.WM_LBUTTONDOWN, lparam)
    assert result == 1  # 直接吞掉：不进入系统输入（范围外点击无效）
    assert mouse_hook["next_calls"] == []  # 未传给钩子链
    assert (info.pt.x, info.pt.y) == (600, 300)  # 结构体未被修改


def test_on_mouse_hook_passes_inside_event(mouse_hook, registered_ids):
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    regions[0].double_click()
    lparam, info = _hook_lparam(300, 300)  # 矩形内
    result = controller._on_mouse_hook(0, win32api.WM_LBUTTONDOWN, lparam)
    assert result == 7  # 放行给钩子链
    assert mouse_hook["next_calls"] == [(1234, 0, win32api.WM_LBUTTONDOWN)]
    assert (info.pt.x, info.pt.y) == (300, 300)  # 界内坐标不被改写


def test_on_mouse_hook_ignored_when_not_engaged(mouse_hook, registered_ids):
    controller, _notices, regions, _indicators, _clip = _make_controller()
    controller.set_enabled(True)
    lparam, _info = _hook_lparam(0, 0)  # 矩形外
    result = controller._on_mouse_hook(0, win32api.WM_LBUTTONDOWN, lparam)
    assert result == 7  # 未锁定：不拦截任何鼠标事件
