"""截图翻译快捷键模式：全局热键（主开关 + 8 个窗口键）与状态机。

需求（future_截图翻译功能补充_快捷键）：右键快捷菜单切换「快捷键模式」，
开启时联动控制模块 ``lock_layout``（整体隐藏全部截图窗口并屏蔽创建/销毁
入口，从根本上不阻挡鼠标与屏幕交互），并经 ``locked_windows`` 记录当下
双击锁定的窗口（只有这些窗口能响应窗口快捷键，触发翻译时按窗口现取的
几何坐标截图）；关闭时 ``unlock_layout`` 还原显示。模式开启后才可使用全局热键：主开关（默认
Ctrl+Alt+P，默认关闭）+ 数字 1-8（对应红橙黄绿青蓝紫黑八色窗口）。主开关
关闭时窗口键仅提示主开关状态；主开关开启后命中已记录窗口等同该窗口锁定态
左键——直接走宿主的截图翻译入口（含既有提示规则）。

与控制模块的边界（互不影响）：本模块经构造注入 controller（鸭子类型，仅需
``lock_layout`` / ``unlock_layout`` / ``locked_windows`` 三组接口）与宿主
能力回调（标题窗提示、触发翻译），不 import 窗口池实现；快捷键模式自身的
布局锁定不改变任何窗口的双击锁定状态。可测逻辑（快捷键串解析、分发决策）
抽为模块级纯函数，状态机测试用 stub 注入，不构造真实 QWidget。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QAbstractNativeEventFilter, QTimer

from .. import win32api
from .window import FRAME_COLORS

logger = logging.getLogger("renpy_overlay.screenshot.hotkeys")

# ---- 文案（菜单 + 标题窗提示，需求逐条对应） ---------------------------------

HOTKEY_MODE_OFF_TEXT = "快捷键模式: 关"
HOTKEY_MODE_ON_TEXT = "快捷键模式: 开"

MSG_MODE_ON = "快捷键模式下截图窗口不可操作，请至少双击锁定了一个截图窗口"
MSG_MODE_OFF = "快捷键模式退出，截图窗口可操作"
MSG_MAIN_ON = "主开关状态：开启"
MSG_MAIN_OFF = "主开关状态：关闭"
MSG_WINDOW_NOT_LOCKED = "请退出快捷键模式，双击锁定相应的截图窗口"
MSG_HOTKEY_REGISTER_FAILED = "全局快捷键注册失败，请尝试更换快捷键，查阅日志获取详细信息"

#: 主开关键含 Ctrl 时的提示。背景（A/B 对照实测结论）：Ren'Py 引擎对
#: 「Ctrl 参与的组合键触发」不会因松开而停止快进（引擎输入栈行为，
#: 与是否注入/热键拦截无关，未注入时 key-up 本就完整到达仍复现），
#: 程序侧补发 key-up 无法治愈，只能提示用户更换快捷键规避
MSG_CTRL_HINT = "提示：主开关键含 Ctrl，Ren'Py 内触发后将持续快进，建议更换为不含 Ctrl 的组合"

# ---- 默认快捷键 ---------------------------------------------------------------

DEFAULT_MAIN_HOTKEY = "ctrl+alt+p"
#: 数字 1-8 依次对应 FRAME_COLORS（红橙黄绿青蓝紫黑）
DEFAULT_WINDOW_HOTKEYS: tuple[str, ...] = tuple(str(i) for i in range(1, 9))

#: 进程内热键 id 基址：槽位 0 = 主开关，1..8 = 窗口键（避开常用小 id 段）
HOTKEY_ID_BASE = 0xC000


def hotkey_id(slot: int) -> int:
    """槽位 → 热键 id（0 = 主开关，1..8 = 窗口键）。"""
    return HOTKEY_ID_BASE + slot


# ---- 纯函数：快捷键解析与分发决策 ---------------------------------------------

_MODIFIERS = {
    "ctrl": win32api.MOD_CONTROL,
    "control": win32api.MOD_CONTROL,
    "alt": win32api.MOD_ALT,
    "shift": win32api.MOD_SHIFT,
    "win": win32api.MOD_WIN,
    "windows": win32api.MOD_WIN,
}
_FKEY_VK = {f"f{index}": 0x70 + index - 1 for index in range(1, 13)}


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """解析快捷键描述串 → (修饰符, 虚拟键码)；非法返回 None。

    ``+`` 分隔修饰符与主键，如 ``ctrl+alt+p``、``1``；大小写不敏感、顺序
    无关。主键支持单字符（字母/数字，取大写 ord 值）与 ``f1``-``f12``；
    必须有且只有一个主键，纯修饰符串非法。
    """
    if not isinstance(text, str):
        return None
    parts = [part.strip() for part in text.split("+")]
    if not all(parts):
        return None
    modifiers = 0
    vk = 0
    for part in parts:
        key = part.lower()
        if key in _MODIFIERS:
            modifiers |= _MODIFIERS[key]
        elif key in _FKEY_VK:
            if vk:
                return None  # 出现第二个主键
            vk = _FKEY_VK[key]
        elif len(key) == 1 and key.isalnum():
            if vk:
                return None
            vk = ord(key.upper())
        else:
            return None
    if not vk:
        return None  # 纯修饰符，没有主键
    return modifiers, vk


def hotkey_contains_ctrl(spec: str) -> bool:
    """快捷键描述串是否含 Ctrl（Ren'Py 快进冲突提示的判定，纯函数）。

    背景：Ren'Py 的 Ctrl 快进在「Ctrl 参与的组合键触发」后不会因松开
    而停止（引擎输入栈行为，key-up 完整到达也不停，与注入无关），
    主开关键避开 Ctrl 即可规避。
    """
    parsed = parse_hotkey(spec)
    return parsed is not None and bool(parsed[0] & win32api.MOD_CONTROL)


#: 窗口快捷键分发决策结果（见 :func:`hotkey_outcome`）
OUTCOME_TRIGGER = "trigger"  # 主开关开且目标已记录：触发截图翻译
OUTCOME_NOT_LOCKED = "not_locked"  # 主开关开但目标未创建/未双击锁定
OUTCOME_MAIN_OFF = "main_off"  # 主开关关闭


def hotkey_outcome(main_on: bool, target_recorded: bool) -> str:
    """窗口快捷键分发决策（需求提示分支 5/6/7 的纯函数化）。

    主开关关闭 → 仅提示主开关状态；主开关开启且目标窗口在快捷键模式开启
    时已记录（双击锁定）→ 触发截图翻译（等同锁定态左键）；否则提示退出
    快捷键模式去锁定窗口。
    """
    if not main_on:
        return OUTCOME_MAIN_OFF
    return OUTCOME_TRIGGER if target_recorded else OUTCOME_NOT_LOCKED


# ---- 修饰键 key-up 补发（RegisterHotKey 吞 key-up 的根因修复） ------------------

#: 修饰符掩码 → 组合内需补发 key-up 的左右键变体 (vk, 是否扩展键)。
#: 按变体精确补发（SendInput+扫描码）：与按下时系统投递的 VK_LCONTROL
#: 等完全对称；通用 VK_CONTROL、扫描码 0 的合成 up 无法与之配对。
_MODIFIER_VARIANTS_BY_MOD = {
    win32api.MOD_CONTROL: ((win32api.VK_LCONTROL, False), (win32api.VK_RCONTROL, True)),
    win32api.MOD_ALT: ((win32api.VK_LMENU, False), (win32api.VK_RMENU, True)),
    win32api.MOD_SHIFT: ((win32api.VK_LSHIFT, False), (win32api.VK_RSHIFT, False)),
    win32api.MOD_WIN: ((win32api.VK_LWIN, True), (win32api.VK_RWIN, True)),
}

#: 用户仍物理按住时的补发重试周期（毫秒）
MODIFIER_RELEASE_RETRY_MS = 50


def pending_stuck_modifiers(modifiers: int) -> int:
    """按左右变体补发物理已松开修饰键的 key-up，返回仍按住需延后的掩码。

    RegisterHotKey 触发 WM_HOTKEY 后系统会吞掉组合键的 key-up（此前的
    key-down 已正常送达前台应用），前台应用的键状态从此停在“按下”——
    表现为 Ctrl/Alt 一直按住（Ren'Py 的 Ctrl 快进不停）。对 GetAsyncKeyState
    显示物理已松开的变体补发 scancode 形态的 key-up（见
    :func:`renpy_overlay.win32api.send_key_up`）；任一变体仍物理按住则
    整个修饰符延后（不打扰按住期间的热键再匹配，例如按住 Ctrl+Alt
    连按主键反复切换），交由 :class:`StuckModifierReleaser` 周期复查。
    """
    pending = 0
    for mask, variants in _MODIFIER_VARIANTS_BY_MOD.items():
        if not modifiers & mask:
            continue
        held = [vk for vk, _extended in variants if win32api.get_async_key_state(vk)]
        if held:
            pending |= mask
            logger.debug(
                "修饰键掩码 %#x：变体 %s 仍被物理按住，补发延后",
                mask,
                ", ".join(f"{vk:#06x}" for vk in held),
            )
            continue
        for vk, extended in variants:
            win32api.send_key_up(vk, extended=extended)
        logger.debug(
            "修饰键掩码 %#x：物理已全部松开，已补发变体 %s",
            mask,
            ", ".join(f"{vk:#06x}" for vk, _extended in variants),
        )
    return pending


class StuckModifierReleaser:
    """被全局热键吞掉的修饰键 key-up 补发器（纯组合，不依赖 QObject 父子）。

    宿主状态机是 QAbstractNativeEventFilter（非 QObject），无法作 parent，
    宿主须自行保持本实例引用防垃圾回收。WM_HOTKEY 分发后调用 :meth:`release`
    传入该热键的修饰符掩码：物理已松开的键立即补发 key-up；仍按住的经
    QTimer（``MODIFIER_RELEASE_RETRY_MS``）周期复查 GetAsyncKeyState，待
    物理松开再补发。只注入 key-up，对一直按住的用户无感知。
    """

    def __init__(self) -> None:
        self._pending = 0  # 尚未补发成功的修饰符掩码
        self._retries = 0  # 已执行的复查轮次（诊断日志用）
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(MODIFIER_RELEASE_RETRY_MS)
        self._timer.timeout.connect(self._flush)

    def release(self, modifiers: int) -> None:
        """登记一组修饰符并立即尝试补发（多次调用按掩码合并）。"""
        self._pending |= modifiers
        self._flush()

    def _flush(self) -> None:
        self._pending = pending_stuck_modifiers(self._pending)
        if self._pending:  # 用户仍按住：周期复查直至物理松开
            self._retries += 1
            logger.debug(
                "修饰键补发第 %d 次复查仍有按住（掩码=%#x），%d ms 后重试",
                self._retries,
                self._pending,
                MODIFIER_RELEASE_RETRY_MS,
            )
            self._timer.start()
        elif self._retries:
            logger.debug("修饰键 key-up 补发完成（经 %d 轮复查）", self._retries)
            self._retries = 0


# ---- 状态机：模式开关 + 全局热键分发 -------------------------------------------


class HotkeyMode(QAbstractNativeEventFilter):
    """快捷键模式状态机 + WM_HOTKEY 分发（回调仅在 Qt 主线程发生）。

    构造注入（同 QuickMenu 的宿主能力注入约定，不依赖宿主类型）：

    - ``controller``：截图翻译控制模块，需 ``lock_layout`` / ``unlock_layout``
      / ``locked_windows`` 三组接口（鸭子类型，通常为 QuickMenu）；
    - ``notify``：标题窗提示回调（如 StreamWindow._update_title）；
    - ``trigger_translation``：截图翻译入口（等同锁定态左键，如
      StreamWindow.request_screenshot_translation，内部含既有提示规则）。
    """

    def __init__(
        self,
        controller,
        notify,
        trigger_translation,
        *,
        main_hotkey: str = DEFAULT_MAIN_HOTKEY,
        window_hotkeys: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._notify = notify
        self._trigger = trigger_translation
        self._main_hotkey = main_hotkey
        self._window_hotkeys = tuple(window_hotkeys or DEFAULT_WINDOW_HOTKEYS)
        if len(self._window_hotkeys) != len(FRAME_COLORS):
            raise ValueError(
                f"窗口快捷键应为 {len(FRAME_COLORS)} 个，收到 {len(self._window_hotkeys)} 个"
            )
        self._enabled = False  # 快捷键模式开关（菜单项显示状态）
        self._main_on = False  # 主开关：模式开启后才可用，默认关闭
        self._registered: list[int] = []  # 已成功注册的热键 id
        self._window_by_id: dict[int, object] = {}  # 窗口键 id → 已锁定窗口/None
        self._modifiers_by_id: dict[int, int] = {}  # 已注册热键 id → 修饰符掩码
        self._modifier_releaser = StuckModifierReleaser()  # 吞键 key-up 补发

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def main_on(self) -> bool:
        return self._main_on

    # ---- 模式开关（右键菜单项调用） -------------------------------------------

    def set_enabled(self, enable: bool) -> None:
        """切换快捷键模式：联动布局锁定、记录可响应窗口、注册/注销热键。"""
        if enable == self._enabled:
            return
        self._enabled = enable
        if enable:
            # 控制模块：锁定布局（整体隐藏全部截图窗口）+ 记录当下双击锁定
            # 的窗口。此后窗口不可操作，双击锁定状态与记录表在模式存续期间
            # 保持一致；触发翻译时按窗口现取几何坐标截图（隐藏不影响
            # GetWindowRect）。
            self._controller.lock_layout()
            self._main_on = False  # 主开关默认关闭（每次进入模式重置）
            self._record_locked_windows()
            register_failed = self._register_all()
            self._install_filter()
            self._notify(MSG_MODE_ON)
            if hotkey_contains_ctrl(self._main_hotkey):
                # 提示放失败提示之前：覆盖式出口下注册失败提示（最重要）
                # 最终胜出；仅主开关键检查——窗口键是纯数字无修饰符
                self._notify(MSG_CTRL_HINT)
            if register_failed:
                # 同一轮多个失败只提示一次（详细原因逐条在日志）；提示放在
                # 模式开启提示之后发出，覆盖式出口最终展示的是失败提示
                self._notify(MSG_HOTKEY_REGISTER_FAILED)
            logger.info(
                "快捷键模式已开启（可响应窗口键 %d 个，主开关键 %r）",
                sum(window is not None for window in self._window_by_id.values()),
                self._main_hotkey,
            )
        else:
            self._remove_filter()
            self._unregister_all()
            self._window_by_id = {}
            self._controller.unlock_layout()
            self._notify(MSG_MODE_OFF)
            logger.info("快捷键模式已关闭（布局已解锁，热键已注销）")

    def toggle(self) -> None:
        self.set_enabled(not self._enabled)

    def shutdown(self) -> None:
        """宿主退出时调用：注销热键（模式状态随对象销毁，无需还原布局）。"""
        self._unregister_all()

    def _record_locked_windows(self) -> None:
        """经控制模块 locked_windows 记录可响应窗口键的窗口（以边框色找窗）。"""
        locked = list(self._controller.locked_windows())
        self._window_by_id = {}
        for slot in range(1, len(FRAME_COLORS) + 1):
            color = FRAME_COLORS[slot - 1]
            self._window_by_id[hotkey_id(slot)] = next(
                (window for window in locked if window.border_color == color), None
            )

    def _register_all(self) -> bool:
        """注册全部热键；返回是否存在失败（存在时由调用方统一提示一次）。"""
        self._unregister_all()  # 防御：先清理残留
        entries = [(hotkey_id(0), self._main_hotkey)] + [
            (hotkey_id(slot), self._window_hotkeys[slot - 1])
            for slot in range(1, len(FRAME_COLORS) + 1)
        ]
        failed = False
        for key_id, spec in entries:
            parsed = parse_hotkey(spec)
            if parsed is None:
                logger.warning(
                    "快捷键 %r 解析失败（热键 id=%#x），跳过注册", spec, key_id
                )
                failed = True
                continue
            modifiers, vk = parsed
            if win32api.register_hotkey(key_id, modifiers, vk):
                self._registered.append(key_id)
                self._modifiers_by_id[key_id] = modifiers
            else:
                # 详细信息（哪个快捷键、id、常见原因）留给日志，标题窗只
                # 提示一次统一文案
                logger.warning(
                    "全局热键注册失败（%r，热键 id=%#x），可能已被其它程序占用",
                    spec,
                    key_id,
                )
                failed = True
        return failed

    def _unregister_all(self) -> None:
        for key_id in self._registered:
            win32api.unregister_hotkey(key_id)
        self._registered = []
        self._modifiers_by_id = {}

    def _install_filter(self) -> None:
        from PyQt6.QtWidgets import QApplication  # noqa: PLC0415 - 惰性导入

        app = QApplication.instance()
        if app is None:  # 离线测试等无 app 场景：分发不可用但不报错
            logger.warning("无 QApplication 实例，WM_HOTKEY 分发不可用")
            return
        app.installNativeEventFilter(self)

    def _remove_filter(self) -> None:
        from PyQt6.QtWidgets import QApplication  # noqa: PLC0415 - 惰性导入

        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)

    # ---- WM_HOTKEY 分发 -------------------------------------------------------

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        """Qt 原生事件过滤器：把 WM_HOTKEY 转入 :meth:`_dispatch`（主线程）。"""
        if event_type == b"windows_generic_MSG":
            import ctypes.wintypes  # noqa: PLC0415 - 仅 Windows 存在

            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == win32api.WM_HOTKEY:
                self._dispatch(int(msg.wParam))
        return False, 0

    def _dispatch(self, key_id: int) -> None:
        if not self._enabled:
            return
        modifiers = self._modifiers_by_id.get(key_id)
        logger.debug(
            "WM_HOTKEY 分发：热键 id=%#x，修饰符掩码=%#x",
            key_id,
            modifiers or 0,
        )
        if modifiers is not None:
            # RegisterHotKey 触发后系统吞掉组合键的 key-up，前台应用的
            # Ctrl/Alt 会停在“按下”；分发后立即安排补发复位
            self._modifier_releaser.release(modifiers)
        if key_id == hotkey_id(0):
            self._main_on = not self._main_on
            if self._main_on:
                self._notify(MSG_MAIN_ON)  # 主开关关闭无需提示（需求）
            return
        window = self._window_by_id.get(key_id)
        outcome = hotkey_outcome(self._main_on, window is not None)
        if outcome == OUTCOME_TRIGGER:
            self._trigger(window)  # 等同锁定态左键：走宿主入口（含既有提示规则）
        elif outcome == OUTCOME_MAIN_OFF:
            self._notify(MSG_MAIN_OFF)
        elif outcome == OUTCOME_NOT_LOCKED:
            self._notify(MSG_WINDOW_NOT_LOCKED)
