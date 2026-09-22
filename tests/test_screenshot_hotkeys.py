"""截图翻译快捷键模式的离线验证：纯函数 + stub 注入，不构造真实 QWidget。

覆盖：快捷键串解析、窗口键分发决策三分支、模式开关联动（lock_layout /
locked_windows / unlock_layout）、热键注册与注销、主开关状态切换与全部
标题窗提示分支、模式重开时记录表刷新与主开关复位，以及注册失败、配置
数量错误等边界。WM_HOTKEY 到 _dispatch 的映射逻辑经 _dispatch 直接驱动。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication

from renpy_overlay import win32api
from renpy_overlay.screenshot.hotkeys import (
    DEFAULT_WINDOW_HOTKEYS,
    FRAME_COLORS,
    MSG_CTRL_HINT,
    MSG_HOTKEY_REGISTER_FAILED,
    MSG_MAIN_OFF,
    MSG_MAIN_ON,
    MSG_MODE_OFF,
    MSG_MODE_ON,
    MSG_WINDOW_NOT_LOCKED,
    OUTCOME_MAIN_OFF,
    OUTCOME_NOT_LOCKED,
    OUTCOME_TRIGGER,
    HotkeyMode,
    StuckModifierReleaser,
    hotkey_contains_ctrl,
    hotkey_id,
    hotkey_outcome,
    parse_hotkey,
    pending_stuck_modifiers,
)

# ---- parse_hotkey：快捷键串解析（纯函数） --------------------------------------


def test_parse_hotkey_plain_key():
    assert parse_hotkey("1") == (0, ord("1"))
    assert parse_hotkey("p") == (0, ord("P"))
    assert parse_hotkey("P") == (0, ord("P"))  # 大小写不敏感


def test_parse_hotkey_modifiers_any_order_and_case():
    expect = (win32api.MOD_CONTROL | win32api.MOD_ALT, ord("P"))
    assert parse_hotkey("ctrl+alt+p") == expect
    assert parse_hotkey("Ctrl+Alt+P") == expect
    assert parse_hotkey("alt+ctrl+p") == expect  # 顺序无关
    assert parse_hotkey("ctrl+alt+ctrl+p") == expect  # 重复修饰符合并


def test_parse_hotkey_f_keys():
    assert parse_hotkey("f1") == (0, 0x70)
    assert parse_hotkey("F12") == (0, 0x7B)


def test_parse_hotkey_invalid():
    assert parse_hotkey("") is None
    assert parse_hotkey("ctrl+") is None  # 尾随分隔符 → 空段
    assert parse_hotkey("ctrl") is None  # 纯修饰符，无主键
    assert parse_hotkey("1+2") is None  # 多个主键
    assert parse_hotkey("foo") is None  # 未知键名
    assert parse_hotkey(None) is None  # 非字符串


# ---- hotkey_outcome：窗口键分发决策（纯函数） ----------------------------------


def test_hotkey_outcome_three_branches():
    assert hotkey_outcome(False, True) == OUTCOME_MAIN_OFF  # 主开关关：无视目标
    assert hotkey_outcome(False, False) == OUTCOME_MAIN_OFF
    assert hotkey_outcome(True, True) == OUTCOME_TRIGGER
    assert hotkey_outcome(True, False) == OUTCOME_NOT_LOCKED


# ---- HotkeyMode：状态机（stub 注入） -------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _qt_core_app():
    """补发器的 QTimer 依赖事件派发器：无显示环境下用 QCoreApplication 兜底。"""
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    yield app


class _StubWindow:
    """鸭子接口 stub：只暴露控制/快捷键模块用到的只读属性。"""

    def __init__(self, color) -> None:
        self._color = color

    @property
    def is_locked(self) -> bool:
        return True

    @property
    def border_color(self):
        return self._color


class _StubController:
    """控制模块鸭子接口 stub：记录布局锁定调用与当前双击锁定窗口。"""

    def __init__(self, locked=()) -> None:
        self.locked = list(locked)
        self.lock_calls = 0
        self.unlock_calls = 0

    def lock_layout(self) -> None:
        self.lock_calls += 1

    def unlock_layout(self) -> None:
        self.unlock_calls += 1

    def locked_windows(self) -> list:
        return list(self.locked)


def _make_mode(locked=(), main_hotkey: str | None = None):
    """构造 (mode, controller, notices, triggered) 四元组（默认快捷键）。"""
    controller = _StubController(locked)
    notices: list[str] = []
    triggered: list = []
    kwargs = {} if main_hotkey is None else {"main_hotkey": main_hotkey}
    mode = HotkeyMode(
        controller,
        notices.append,
        triggered.append,
        **kwargs,
    )
    return mode, controller, notices, triggered


@pytest.fixture()
def registered_ids(monkeypatch):
    """假热键注册表：记录注册 id，unregister 同步移除；修饰键补发离线化。"""
    ids: list[int] = []
    monkeypatch.setattr(
        win32api, "register_hotkey", lambda key_id, _mod, _vk: ids.append(key_id) or True
    )
    monkeypatch.setattr(win32api, "unregister_hotkey", ids.remove)
    monkeypatch.setattr(win32api, "get_async_key_state", lambda _vk: False)
    monkeypatch.setattr(win32api, "send_key_up", lambda _vk, **_kw: None)
    return ids


def test_hotkey_contains_ctrl():
    """含 Ctrl 判定（Ren'Py 快进冲突提示的纯函数）。"""
    assert hotkey_contains_ctrl("ctrl+alt+t") is True
    assert hotkey_contains_ctrl("control+p") is True
    assert hotkey_contains_ctrl("alt+f9") is False
    assert hotkey_contains_ctrl("1") is False
    assert hotkey_contains_ctrl("not a key") is False  # 解析失败视为不含


def test_enable_mode_locks_layout_and_registers_all_hotkeys(registered_ids):
    mode, controller, notices, _triggered = _make_mode()
    mode.set_enabled(True)
    assert controller.lock_calls == 1  # 联动控制模块锁定截图布局
    assert registered_ids == [hotkey_id(slot) for slot in range(9)]  # 主开关 + 8 窗口键
    # 默认主开关键 ctrl+alt+p 含 Ctrl：追加 Ren'Py 快进冲突提示
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT]
    assert mode.enabled is True
    assert mode.main_on is False  # 主开关默认关闭


def test_enable_mode_warns_when_main_hotkey_has_ctrl(registered_ids):
    """主开关键含 Ctrl（如实测配置 ctrl+alt+t）：提示更换以规避快进。"""
    mode, _controller, notices, _triggered = _make_mode(main_hotkey="ctrl+alt+t")
    mode.set_enabled(True)
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT]
    mode.set_enabled(False)


def test_enable_mode_silent_when_main_hotkey_without_ctrl(registered_ids):
    """主开关键不含 Ctrl：无冲突提示。"""
    mode, _controller, notices, _triggered = _make_mode(main_hotkey="alt+f9")
    mode.set_enabled(True)
    assert notices == [MSG_MODE_ON]


def test_disable_mode_unlocks_layout_and_unregisters(registered_ids):
    mode, controller, notices, _triggered = _make_mode()
    mode.set_enabled(True)
    mode.set_enabled(False)
    assert controller.lock_calls == 1 and controller.unlock_calls == 1
    assert registered_ids == []  # 热键全部注销
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT, MSG_MODE_OFF]
    assert mode.enabled is False


def test_set_enabled_is_idempotent(registered_ids):
    mode, controller, notices, _triggered = _make_mode()
    mode.set_enabled(True)
    mode.set_enabled(True)  # 重复开启无额外副作用
    mode.set_enabled(False)
    mode.set_enabled(False)  # 重复关闭无额外副作用
    assert controller.lock_calls == 1 and controller.unlock_calls == 1
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT, MSG_MODE_OFF]


def test_main_switch_toggle_notifies_only_on_enable(registered_ids):
    mode, _controller, notices, _triggered = _make_mode()
    mode.set_enabled(True)
    mode._dispatch(hotkey_id(0))
    assert mode.main_on is True
    assert notices[-1] == MSG_MAIN_ON
    mode._dispatch(hotkey_id(0))
    assert mode.main_on is False
    assert notices[-1] == MSG_MAIN_ON  # 主开关关闭无需提示（需求）


def test_window_key_with_main_off_only_notifies_main_off(registered_ids):
    red = _StubWindow(FRAME_COLORS[0])
    mode, _controller, notices, triggered = _make_mode([red])
    mode.set_enabled(True)
    mode._dispatch(hotkey_id(1))  # 红：即使已锁定，主开关关也无效
    assert notices[-1] == MSG_MAIN_OFF
    assert triggered == []


def test_window_key_unlocked_notifies_exit_hint(registered_ids):
    mode, _controller, notices, triggered = _make_mode()  # 没有任何双击锁定窗口
    mode.set_enabled(True)
    mode._dispatch(hotkey_id(0))  # 打开主开关
    mode._dispatch(hotkey_id(1))  # 红：未创建/未双击锁定
    assert notices[-1] == MSG_WINDOW_NOT_LOCKED
    assert triggered == []


def test_window_key_locked_triggers_translation(registered_ids):
    red = _StubWindow(FRAME_COLORS[0])
    mode, _controller, notices, triggered = _make_mode([red])
    mode.set_enabled(True)
    mode._dispatch(hotkey_id(0))  # 打开主开关
    mode._dispatch(hotkey_id(1))  # 红：已双击锁定
    assert triggered == [red]  # 等同锁定态左键：走宿主翻译入口
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT, MSG_MAIN_ON]  # 后续提示由翻译流程自身负责


def test_reenable_resets_main_switch_and_rerecords_windows(registered_ids):
    red = _StubWindow(FRAME_COLORS[0])
    orange = _StubWindow(FRAME_COLORS[1])
    mode, controller, notices, triggered = _make_mode([red])
    mode.set_enabled(True)
    mode._dispatch(hotkey_id(0))  # 主开关开
    mode.set_enabled(False)
    controller.locked = [orange]  # 期间锁定窗口发生了变化
    mode.set_enabled(True)
    assert mode.main_on is False  # 主开关复位为默认关闭
    mode._dispatch(hotkey_id(0))
    mode._dispatch(hotkey_id(2))  # 橙：新一轮记录表已命中
    assert triggered == [orange]
    mode._dispatch(hotkey_id(1))  # 红：旧记录不再响应
    assert notices[-1] == MSG_WINDOW_NOT_LOCKED


def test_register_failure_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(win32api, "register_hotkey", lambda *_args: False)  # 全部占用
    mode, _controller, notices, _triggered = _make_mode()
    mode.set_enabled(True)  # 注册失败不阻碍模式开启
    assert mode.enabled is True
    # Ctrl 冲突提示在失败提示之前：覆盖式出口下失败提示（最重要）胜出
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT, MSG_HOTKEY_REGISTER_FAILED]
    mode._dispatch(hotkey_id(0))  # 状态机照常工作
    assert mode.main_on is True
    mode.shutdown()


def test_register_partial_failure_notifies_once(monkeypatch):
    # 仅主开关注册成功（8 个窗口键全被占用）：仍然只提示一次
    monkeypatch.setattr(
        win32api, "register_hotkey", lambda key_id, _mod, _vk: key_id == hotkey_id(0)
    )
    mode, _controller, notices, _triggered = _make_mode()
    mode.set_enabled(True)
    assert notices == [MSG_MODE_ON, MSG_CTRL_HINT, MSG_HOTKEY_REGISTER_FAILED]
    assert mode._registered == [hotkey_id(0)]  # 成功的主开关照常生效


def test_shutdown_unregisters_all(registered_ids):
    mode, _controller, _notices, _triggered = _make_mode()
    mode.set_enabled(True)
    mode.shutdown()
    assert registered_ids == []


def test_dispatch_ignored_when_disabled(registered_ids):
    mode, _controller, notices, triggered = _make_mode()
    mode._dispatch(hotkey_id(0))  # 模式未开启：任何热键都不生效
    mode._dispatch(hotkey_id(1))
    assert notices == [] and triggered == []
    assert mode.main_on is False


def test_window_hotkeys_wrong_count_raises():
    with pytest.raises(ValueError, match="8"):
        HotkeyMode(_StubController(), lambda _t: None, lambda _w: None, window_hotkeys=("1",))
    assert len(DEFAULT_WINDOW_HOTKEYS) == 8


# ---- 修饰键 key-up 补发（RegisterHotKey 触发后吞 key-up 的根因修复） ------------


def test_pending_stuck_modifiers_releases_physically_released_keys(monkeypatch):
    """物理已松开的修饰键：立即按左右变体补发 key-up，无延后。"""
    pressed: set[int] = set()
    released: list[int] = []
    monkeypatch.setattr(win32api, "get_async_key_state", lambda vk: vk in pressed)
    monkeypatch.setattr(win32api, "send_key_up", lambda vk, **_kw: released.append(vk))
    ctrl_alt = win32api.MOD_CONTROL | win32api.MOD_ALT
    assert pending_stuck_modifiers(ctrl_alt) == 0
    assert released == [
        win32api.VK_LCONTROL,
        win32api.VK_RCONTROL,
        win32api.VK_LMENU,
        win32api.VK_RMENU,
    ]


def test_pending_stuck_modifiers_defers_physically_held_keys(monkeypatch):
    """任一变体仍物理按住：整个修饰符不补发（避免干扰热键再匹配）。"""
    pressed = {win32api.VK_LCONTROL}
    released: list[int] = []
    monkeypatch.setattr(win32api, "get_async_key_state", lambda vk: vk in pressed)
    monkeypatch.setattr(win32api, "send_key_up", lambda vk, **_kw: released.append(vk))
    ctrl_alt = win32api.MOD_CONTROL | win32api.MOD_ALT
    assert pending_stuck_modifiers(ctrl_alt) == win32api.MOD_CONTROL
    assert released == [win32api.VK_LMENU, win32api.VK_RMENU]  # 松开的 Alt 照常补发


def test_pending_stuck_modifiers_no_op_without_modifiers(monkeypatch):
    """数字键等无修饰符热键：无需补发。"""
    released: list[int] = []
    monkeypatch.setattr(win32api, "get_async_key_state", lambda _vk: False)
    monkeypatch.setattr(win32api, "send_key_up", lambda vk, **_kw: released.append(vk))
    assert pending_stuck_modifiers(0) == 0
    assert released == []


def test_stuck_modifier_releaser_retries_until_released(monkeypatch):
    """补发器：按住时挂起待重试，物理松开后补发并停止排程。"""
    pressed = {win32api.VK_LCONTROL}
    released: list[int] = []
    monkeypatch.setattr(win32api, "get_async_key_state", lambda vk: vk in pressed)
    monkeypatch.setattr(win32api, "send_key_up", lambda vk, **_kw: released.append(vk))
    releaser = StuckModifierReleaser()
    ctrl_alt = win32api.MOD_CONTROL | win32api.MOD_ALT
    releaser.release(ctrl_alt)
    assert released == [win32api.VK_LMENU, win32api.VK_RMENU]  # Alt 立即补发
    assert releaser._pending == win32api.MOD_CONTROL  # Ctrl 挂起
    assert releaser._timer.isActive()  # 周期复查已排程
    pressed.clear()  # 用户松开 Ctrl
    releaser._flush()
    assert released == [
        win32api.VK_LMENU,
        win32api.VK_RMENU,
        win32api.VK_LCONTROL,
        win32api.VK_RCONTROL,
    ]
    assert releaser._pending == 0  # 全部补发完成，不再排程重试
    # （single-shot 触发后由 Qt 自动置 inactive；离线无事件循环不模拟）


def test_dispatch_releases_main_hotkey_modifiers(registered_ids, monkeypatch):
    """主开关键分发后按左右变体补发 Ctrl/Alt 的 key-up（Ctrl 卡住修复点）。"""
    mode, _controller, _notices, _triggered = _make_mode()
    mode.set_enabled(True)
    released: list[int] = []
    monkeypatch.setattr(win32api, "send_key_up", lambda vk, **_kw: released.append(vk))
    mode._dispatch(hotkey_id(0))  # 默认主开关键 ctrl+alt+p
    assert released == [
        win32api.VK_LCONTROL,
        win32api.VK_RCONTROL,
        win32api.VK_LMENU,
        win32api.VK_RMENU,
    ]
    released.clear()
    mode._dispatch(hotkey_id(1))  # 窗口键无修饰符：无补发
    assert released == []


def test_unregister_clears_modifier_map(registered_ids, monkeypatch):
    """模式关闭注销热键后：修饰符表清空，后续分发不再补发。"""
    mode, _controller, _notices, _triggered = _make_mode()
    mode.set_enabled(True)
    mode.set_enabled(False)
    assert mode._modifiers_by_id == {}
    released: list[int] = []
    monkeypatch.setattr(win32api, "send_key_up", lambda vk, **_kw: released.append(vk))
    mode._dispatch(hotkey_id(0))  # 已禁用：早退，无补发
    assert released == []
