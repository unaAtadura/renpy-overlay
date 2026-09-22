"""锁鼠标区域控制器：快捷菜单开关 + 范围锁定状态机 + 逃脱全局热键。

需求（future_限制鼠标范围）：窗口化运行游戏时把鼠标活动范围限制在框选
区域内，防止鼠标点出游戏窗口。交互链：

- 快捷菜单「锁鼠标区域: 关/开」（默认关）→ 创建范围窗口，用户拖拽/
  拉伸到合适大小；
- 范围窗口内双击 → 隐藏范围窗口，用等大小的鼠标穿透提示窗口替换
  （见 :mod:`mouse_lock_indicator`），并对该区域施加 Win32 ``ClipCursor``
  限制，提示逃脱快捷键（展示配置文件的实际值）；
- 逃脱快捷键（默认 ctrl+alt+o，配置键 ``hotkey_mouse_escape``）为保底
  退出方式：窗口隐藏后无法双击解锁，且流式窗可能在限制范围外导致快捷
  菜单不可达。按下后解除限制、销毁提示窗口、恢复范围窗口的可拖拽状态
  （无提示，需求）；
- 菜单再点「开」→ 彻底销毁两个窗口并解除限制（无提示，需求退出方式 1）。

与截图布局锁定的边界（互不影响，需求约束）：本模块的窗口由自己持有，
绝不进入 QuickMenu 截图窗口池 —— ``lock_layout`` / ``unlock_layout`` /
``locked_windows`` 三组接口只遍历池内截图窗口，天然不会隐藏、恢复或
捕获本功能的任何窗口。

鲁棒性：``ClipCursor`` 是系统级临时限制，会被其它进程覆盖、被 Alt+Tab
等系统行为清除，提示窗口也可能被独占全屏激活盖住 —— 锁定态由 QTimer
周期（``REASSERT_INTERVAL_MS``）重申限制与置顶双保险，重申后经
``GetClipCursor`` 回读校验，被外部改写时留告警日志便于定位。

坐标系约定（缺陷实测教训，见 logs/renpy_overlay_20260922_211428.log）：
``ClipCursor`` 只认物理像素（``GetWindowRect``），Qt ``setGeometry`` 只认
Qt 全局逻辑坐标 —— 进程虽为 Per-Monitor DPI Aware，DPR≠1 的屏幕上两者
数值并不相等。提示窗几何取范围窗口 ``geometry()``（Qt 逻辑），限制矩形
取 ``win32api.window_rect``（物理），各喂各的才能精确重合于同一物理区域
（历史上把物理值当逻辑值喂提示窗，导致其放大 DPR 倍并右下偏移，而限制
实际生效在原范围窗，看起来像“鼠标能移出显示的框”）。

可测逻辑（提示文案）抽为模块级纯函数；状态机测试经构造注入 stub 工厂
与 clip 函数，不构造真实 QWidget（见 tests/test_mouse_lock.py）。
"""

from __future__ import annotations

import ctypes
import logging
import time

from PyQt6.QtCore import QAbstractNativeEventFilter, QPoint, QTimer

from .. import win32api
from .hotkeys import (
    MSG_HOTKEY_REGISTER_FAILED,
    StuckModifierReleaser,
    parse_hotkey,
)
from .mouse_lock_indicator import MouseLockIndicatorWindow
from .mouse_lock_window import MouseLockRegionWindow

logger = logging.getLogger("renpy_overlay.screenshot.mouse_lock")

# ---- 文案（菜单 + 标题窗提示，需求逐条对应） ---------------------------------

MOUSE_LOCK_OFF_TEXT = "锁鼠标区域: 关"
MOUSE_LOCK_ON_TEXT = "锁鼠标区域: 开"

#: 注册失败提示与快捷键模式共用同一文案（需求给定文本一致，不复制字符串）

# ---- 默认逃脱快捷键 ------------------------------------------------------------

DEFAULT_MOUSE_ESCAPE_HOTKEY = "ctrl+alt+o"

#: 进程内热键 id：独立段，避开快捷键模式的 0xC000..0xC008（槽位 0-8）
MOUSE_LOCK_HOTKEY_ID = 0xC100

#: 限制重申周期（毫秒）：ClipCursor 与置顶都可能被外部行为破坏，定时双保险
REASSERT_INTERVAL_MS = 200

#: 低级鼠标钩子日志节流间隔（秒）：钩子事件高频触发，避免刷屏
HOOK_LOG_INTERVAL_S = 2.0
#: 锁定维持心跳日志间隔（秒）：证明重申定时器在跑、回读一致
HEARTBEAT_LOG_INTERVAL_S = 10.0

#: 低级钩子事件决策结果
OUTCOME_SWALLOW = "swallow"  # 出界点击/滚轮：直接吞掉（不进入系统输入）
OUTCOME_CLAMP = "clamp"  # 出界移动：改写坐标到矩形内最近点
OUTCOME_PASS = "pass"  # 界内事件与其它消息：放行

#: 范围外需要吞掉的点击类消息（含滚轮：范围外滚动会影响范围外窗口）
_SWALLOW_MOUSE_EVENTS = frozenset(
    {
        win32api.WM_LBUTTONDOWN,
        win32api.WM_LBUTTONUP,
        win32api.WM_RBUTTONDOWN,
        win32api.WM_RBUTTONUP,
        win32api.WM_MBUTTONDOWN,
        win32api.WM_MBUTTONUP,
        win32api.WM_XBUTTONDOWN,
        win32api.WM_XBUTTONUP,
        win32api.WM_MOUSEWHEEL,
        win32api.WM_MOUSEHWHEEL,
    }
)

#: 双击锁定后的提示模板：逃脱快捷键展示配置文件的实际值（需求）
_MSG_MOUSE_LOCKED_TEMPLATE = "鼠标活动范围已被限制，逃脱快捷键{}"


def escape_message(hotkey: str) -> str:
    """双击锁定成功后的提示文案（纯函数，快捷键展示配置实际值）。"""
    return _MSG_MOUSE_LOCKED_TEMPLATE.format(hotkey)


def clamp_point_to_rect(pt: tuple[int, int], rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """把点钳制到矩形内的最近内点（纯函数，低级钩子移动修正用）。"""
    left, top, right, bottom = rect
    x = min(max(int(pt[0]), left), right - 1)
    y = min(max(int(pt[1]), top), bottom - 1)
    return (x, y)


def mouse_hook_outcome(
    event: int, pt: tuple[int, int], rect: tuple[int, int, int, int]
) -> str:
    """低级鼠标钩子事件决策（纯函数）。

    - ``OUTCOME_SWALLOW``：点击/滚轮类事件落在矩形外 → 吞掉（范围外
      点击无效，需求设计初衷）；
    - ``OUTCOME_CLAMP``：移动落在矩形外 → 改写坐标到矩形内最近点
      （光标视觉钳制，对抗绕过 ClipCursor 的远程/驱动注入路径）；
    - ``OUTCOME_PASS``：界内事件与其它消息 → 放行。
    """
    inside = rect[0] <= pt[0] < rect[2] and rect[1] <= pt[1] < rect[3]
    if inside:
        return OUTCOME_PASS
    if event in _SWALLOW_MOUSE_EVENTS:
        return OUTCOME_SWALLOW
    if event == win32api.WM_MOUSEMOVE:
        return OUTCOME_CLAMP
    return OUTCOME_PASS


class MouseLockController(QAbstractNativeEventFilter):
    """锁鼠标区域状态机 + WM_HOTKEY 分发（回调仅在 Qt 主线程发生）。

    构造注入（同 QuickMenu/HotkeyMode 的宿主能力注入约定，不依赖宿主
    类型）：``notify`` 为标题窗提示回调（如 StreamWindow._update_title）；
    ``escape_hotkey`` 为逃脱快捷键描述串（config ``hotkey_mouse_escape``）；
    其余工厂/函数参数仅供离线测试注入 stub，默认接真实实现。
    """

    def __init__(
        self,
        notify,
        *,
        escape_hotkey: str = DEFAULT_MOUSE_ESCAPE_HOTKEY,
        region_window_factory=None,
        indicator_factory=None,
        clip_cursor=None,
        window_rect_fn=None,
        get_clip_cursor=None,
        set_mouse_hook_fn=None,
        unset_mouse_hook_fn=None,
        call_next_mouse_hook_fn=None,
    ) -> None:
        super().__init__()
        self._notify = notify
        self._escape_hotkey = escape_hotkey
        self._escape_modifiers = 0  # 逃脱热键修饰符掩码（注册成功后记录）
        self._region_window_factory = region_window_factory or MouseLockRegionWindow
        self._indicator_factory = indicator_factory or MouseLockIndicatorWindow
        self._clip_cursor = clip_cursor or win32api.clip_cursor
        self._window_rect = window_rect_fn or win32api.window_rect
        self._get_clip_cursor = get_clip_cursor or win32api.get_clip_cursor
        self._set_mouse_hook = set_mouse_hook_fn or win32api.set_mouse_hook
        self._unset_mouse_hook = unset_mouse_hook_fn or win32api.unset_mouse_hook
        self._call_next_mouse_hook = (
            call_next_mouse_hook_fn or win32api.call_next_mouse_hook
        )

        self._enabled = False  # 菜单开关状态（决定热键注册与范围窗口存在）
        self._engaged = False  # 双击锁定已生效（鼠标当前被限制）
        self._region_window = None
        self._indicator_window = None
        self._registered = False  # 逃脱热键是否已注册
        self._modifier_releaser = StuckModifierReleaser()  # 吞键 key-up 补发
        self._region_rect: tuple[int, int, int, int] | None = None  # 锁定矩形快照
        self._mouse_hook: int | None = None  # WH_MOUSE_LL 钩子句柄
        self._mouse_hook_proc = None  # 回调引用防垃圾回收
        self._last_hook_log = 0.0  # 钩子活动日志节流
        self._reassert_count = 0  # 重申计数（心跳日志用）
        self._last_heartbeat_log = 0.0

        self._reassert_timer = QTimer()
        self._reassert_timer.setInterval(REASSERT_INTERVAL_MS)
        self._reassert_timer.timeout.connect(self._reassert)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def engaged(self) -> bool:
        return self._engaged

    # ---- 开关（右键菜单项调用） -----------------------------------------------

    def set_enabled(self, enable: bool) -> None:
        """切换功能开关：创建/销毁范围窗口，注册/注销逃脱热键。

        幂等：重复调用无额外副作用。关闭时无论是否处于锁定态都彻底解除
        限制并销毁两窗口（需求退出方式 1，无提示）。
        """
        if enable == self._enabled:
            return
        self._enabled = enable
        if enable:
            self._region_window = self._region_window_factory(on_lock=self._engage)
            self._region_window.show()
            self._move_region_to_screen_center()
            self._register_escape_hotkey()
            self._install_filter()
            logger.info(
                "锁鼠标区域已开启（逃脱快捷键 %r），请拖拽范围后双击锁定",
                self._escape_hotkey,
            )
        else:
            self._teardown()
            logger.info("锁鼠标区域已关闭（限制已解除，窗口已销毁）")

    def toggle(self) -> None:
        self.set_enabled(not self._enabled)

    def shutdown(self) -> None:
        """宿主退出时调用：解除限制 + 注销热键 + 销毁窗口（幂等）。

        必须解除 ClipCursor —— 该限制是系统级的，工具退出后不清除会把
        鼠标永久困在矩形内。
        """
        self._teardown()

    def _teardown(self) -> None:
        """彻底关闭公共清理：停定时器、解除限制、销毁两窗、注销热键。"""
        self._enabled = False
        self._reassert_timer.stop()
        if self._engaged:
            self._clip_cursor(None)
        self._uninstall_mouse_hook()
        self._engaged = False
        self._region_rect = None
        if self._indicator_window is not None:
            self._indicator_window.hide_indicator()
            self._indicator_window.deleteLater()
            self._indicator_window = None
        if self._region_window is not None:
            self._region_window.close()
            self._region_window.deleteLater()
            self._region_window = None
        self._remove_filter()
        self._unregister_escape_hotkey()

    # ---- 锁定生效与逃脱 -------------------------------------------------------

    def _engage(self, window) -> None:
        """范围窗口双击：锁定生效（隐藏窗口、显示穿透提示、限制鼠标）。

        坐标分流（缺陷实测：物理值喂 Qt 会放大 DPR 倍并右下偏移）：

        - 限制矩形：``GetWindowRect`` 物理像素 → ``ClipCursor``（系统物理坐标）；
        - 提示窗几何：``geometry()`` Qt 全局逻辑坐标 → ``setGeometry``。

        两者指向同一物理区域，任何 DPR 下提示框与限制范围精确重合。
        """
        if not self._enabled or window is not self._region_window:
            return
        try:
            physical = self._window_rect(window.hwnd)
            geo = window.geometry()
            dpr = float(window.devicePixelRatioF())
        except Exception:  # pragma: no cover - 窗口销毁竞态
            logger.warning("无法获取锁鼠标区域矩形，取消本次锁定")
            return
        logical = (geo.x(), geo.y(), geo.x() + geo.width(), geo.y() + geo.height())
        if not self._clip_cursor(physical):
            logger.warning("ClipCursor 施加失败（区域 %r），取消本次锁定", physical)
            return
        self._region_rect = physical
        if self._indicator_window is None:
            self._indicator_window = self._indicator_factory()
        self._indicator_window.show_region(logical)  # 先显提示窗再隐范围窗，防闪烁
        window.hide()
        self._engaged = True
        self._reassert_timer.start()
        self._install_mouse_hook()
        self._notify(escape_message(self._escape_hotkey))
        # 双矩形 + DPR + 回读全部留痕：提示窗错位/限制被改写时可直接定位
        logger.info(
            "鼠标活动范围已限制：物理 %r（ClipCursor），Qt 逻辑 %r（提示窗），"
            "DPR %.2f，回读 %r（逃脱快捷键 %r）",
            physical,
            logical,
            dpr,
            self._get_clip_cursor(),
            self._escape_hotkey,
        )

    def _escape(self) -> None:
        """逃脱快捷键：解除限制、销毁提示窗、恢复范围窗口可编辑状态。

        需求：解锁后不需提示。范围窗口恢复显示即恢复未锁定态（可拖拽
        位置与八向拉伸），可再次双击锁定。
        """
        if not self._enabled or not self._engaged:
            return
        self._reassert_timer.stop()
        self._clip_cursor(None)
        self._uninstall_mouse_hook()
        self._engaged = False
        self._region_rect = None
        if self._indicator_window is not None:
            self._indicator_window.hide_indicator()
            self._indicator_window.deleteLater()
            self._indicator_window = None
        if self._region_window is not None:
            self._region_window.show()
        logger.info("鼠标活动范围限制已解除（逃脱快捷键），范围窗口恢复可拖拽")

    def _reassert(self) -> None:
        """周期重申：重新施加 ClipCursor + 提示窗置顶（对抗覆盖/清除）。

        重申后经 ``GetClipCursor`` 回读校验：生效矩形与预期不一致说明被
        游戏（SDL 抓取鼠标）等外部程序改写，除自动恢复外留下告警，便于
        定位“鼠标能移出范围”类反馈的真正来源；周期性心跳日志证明限制
        链路在持续维持。
        """
        if not self._engaged or self._region_rect is None:
            return
        self._reassert_count += 1
        if not self._clip_cursor(self._region_rect):
            logger.debug("ClipCursor 重申失败（区域 %r）", self._region_rect)
        effective = self._get_clip_cursor()
        if effective is not None and tuple(effective) != tuple(self._region_rect):
            logger.warning(
                "鼠标限制矩形与预期不一致（生效 %r，预期 %r），可能被游戏或"
                "其它程序改写，已按周期重申",
                tuple(effective),
                self._region_rect,
            )
        now = time.monotonic()
        if now - self._last_heartbeat_log >= HEARTBEAT_LOG_INTERVAL_S:
            self._last_heartbeat_log = now
            logger.debug(
                "锁定维持中：已重申 %d 次，回读 %r",
                self._reassert_count,
                effective,
            )
        if self._indicator_window is not None:
            try:
                win32api.set_topmost(self._indicator_window.hwnd)
            except Exception:  # pragma: no cover - 窗口销毁竞态
                pass

    # ---- 低级鼠标钩子（WH_MOUSE_LL 双保险） ------------------------------------

    def _install_mouse_hook(self) -> None:
        """安装 WH_MOUSE_LL 双保险钩子（对抗绕过 ClipCursor 的输入路径）。

        ClipCursor 是软限制：远程桌面/驱动级注入可能绕过它（实测日志
        回读一致但光标仍能出界）。低级钩子位于系统输入分发链，对注入
        产生的输入事件同样生效：出界移动改写坐标钳回、出界点击直接吞掉。
        必须在拥有消息循环的线程安装（Qt 主线程）；回调引用由实例持有
        防垃圾回收。
        """
        if self._mouse_hook is not None or not win32api.IS_WINDOWS:
            return
        proc = win32api.LOWLEVEL_MOUSE_PROC(self._on_mouse_hook)
        hook = self._set_mouse_hook(proc)
        if hook is None:
            logger.warning(
                "低级鼠标钩子安装失败（ClipCursor 仍生效），范围外拦截能力降级"
            )
            return
        self._mouse_hook = hook
        self._mouse_hook_proc = proc
        self._last_hook_log = time.monotonic()
        logger.info("低级鼠标钩子已安装（范围外移动钳回 + 点击拦截双保险）")

    def _uninstall_mouse_hook(self) -> None:
        """卸载低级鼠标钩子（未安装时静默忽略）。"""
        if self._mouse_hook is None:
            return
        self._unset_mouse_hook(self._mouse_hook)
        self._mouse_hook = None
        self._mouse_hook_proc = None
        logger.info("低级鼠标钩子已卸载")

    def _on_mouse_hook(self, ncode, wparam, lparam):
        """WH_MOUSE_LL 回调：出界移动钳回矩形，出界点击/滚轮直接吞掉。

        回调在安装线程（Qt 主线程）执行，必须快速返回 —— 仅做几何判断
        与结构体坐标改写，日志经节流避免刷屏；逃脱热键走键盘链路，
        不受本钩子影响。整个回调体（含钩子链转发）都包在异常兑底内：
        ctypes 回调抛出的异常会被忽略（只打 stderr 不进日志）且返回零，
        任何异常都就地记入日志并返回安全值，绝不向外层传播。
        """
        try:
            if ncode >= 0 and self._engaged and self._region_rect is not None:
                info = ctypes.cast(
                    ctypes.c_void_p(lparam), ctypes.POINTER(win32api.MSLLHOOKSTRUCT)
                ).contents
                pt = (int(info.pt.x), int(info.pt.y))
                outcome = mouse_hook_outcome(int(wparam), pt, self._region_rect)
                if outcome == OUTCOME_SWALLOW:
                    self._log_hook_throttled("已拦截范围外鼠标事件 %r", pt)
                    return 1  # 不传给钩子链下一环：范围外点击不进入系统输入
                if outcome == OUTCOME_CLAMP:
                    clamped = clamp_point_to_rect(pt, self._region_rect)
                    info.pt.x, info.pt.y = clamped
                    self._log_hook_throttled(
                        "范围外移动已钳回 %r -> %r", pt, clamped
                    )
            return self._call_next_mouse_hook(self._mouse_hook, ncode, wparam, lparam)
        except Exception:
            logger.exception("低级鼠标钩子回调异常，本次事件已放行")
            return 0  # “未处理”语义：系统继续正常分发，事件流不中断

    def _log_hook_throttled(self, msg: str, *args) -> None:
        """钩子活动日志节流（高频事件下每 HOOK_LOG_INTERVAL_S 最多一条）。"""
        now = time.monotonic()
        if now - self._last_hook_log < HOOK_LOG_INTERVAL_S:
            return
        self._last_hook_log = now
        logger.debug(msg, *args)

    # ---- 逃脱热键（全局注册 + WM_HOTKEY 分发，模式同 hotkeys.HotkeyMode） ------

    def _register_escape_hotkey(self) -> None:
        """注册逃脱全局热键；失败提示一次（详细原因在日志）。"""
        self._unregister_escape_hotkey()  # 防御：先清理残留
        parsed = parse_hotkey(self._escape_hotkey)
        if parsed is None:
            logger.warning(
                "逃脱快捷键 %r 解析失败（热键 id=%#x），跳过注册",
                self._escape_hotkey,
                MOUSE_LOCK_HOTKEY_ID,
            )
            self._notify(MSG_HOTKEY_REGISTER_FAILED)
            return
        modifiers, vk = parsed
        if win32api.register_hotkey(MOUSE_LOCK_HOTKEY_ID, modifiers, vk):
            self._registered = True
            self._escape_modifiers = modifiers
            if modifiers & win32api.MOD_CONTROL:
                # 不进标题窗提示链（避免覆盖锁定生效时的逃脱键提示），
                # 仅日志留档：Ren'Py 内触发后将持续快进（引擎级行为，
                # 与注入无关），建议更换为不含 Ctrl 的组合
                logger.warning(
                    "逃脱快捷键 %r 含 Ctrl：Ren'Py 内触发后将持续快进"
                    "（引擎级行为，与注入无关），建议更换为不含 Ctrl 的组合",
                    self._escape_hotkey,
                )
        else:
            logger.warning(
                "全局热键注册失败（%r，热键 id=%#x），可能已被其它程序占用",
                self._escape_hotkey,
                MOUSE_LOCK_HOTKEY_ID,
            )
            self._notify(MSG_HOTKEY_REGISTER_FAILED)

    def _unregister_escape_hotkey(self) -> None:
        if self._registered:
            win32api.unregister_hotkey(MOUSE_LOCK_HOTKEY_ID)
            self._registered = False
        self._escape_modifiers = 0

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
        if key_id == MOUSE_LOCK_HOTKEY_ID:
            # RegisterHotKey 触发后系统吞掉组合键的 key-up，前台应用的
            # Ctrl/Alt 会停在“按下”；分发后立即安排补发复位
            self._modifier_releaser.release(self._escape_modifiers)
            self._escape()

    # ---- 内部 ---------------------------------------------------------------

    def _move_region_to_screen_center(self) -> None:
        """范围窗口初始摆到主屏可用区域中心（与截图窗口初始摆放同参照点）。"""
        from PyQt6.QtWidgets import QApplication  # noqa: PLC0415 - 惰性导入

        if self._region_window is None:
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            center = QPoint(geo.center().x(), geo.center().y())
        else:  # pragma: no cover - 无屏环境兜底
            center = QPoint(400, 300)
        w, h = self._region_window.width(), self._region_window.height()
        self._region_window.move(center.x() - w // 2, center.y() - h // 2)
