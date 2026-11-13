"""遥控截图控制器：快捷键模式期间的 A/B 小圆窗生命周期与"装填"状态机。

需求（future_遥控截图功能）：点击截图遮挡屏幕、快捷键截图无法用鼠标，
本功能提供折中的第三种截图方式：

- 进入快捷键模式（配置开关开启时）生成窗口 A（触发器）与 B（瞄准器），
  退出即销毁；与快捷键模式主开关相互独立（主开关关闭时功能正常工作）；
- 窗口 B 拖入"已双击锁定的截图区域"变为正方形（边框色 = 该区域窗口的
  边框色）即装填完成；拖入未锁定区域不变换；与多个区域相交时第一个进入
  的驻留生效、不响应后续进入的区域，离开后恢复圆形；
- 窗口 A 左键单击 → B 已装填则以装填的目标截图窗口触发截图翻译（走宿主
  入口：在途互斥、既有提示、隐藏/抓屏/恢复时序全部复用）；未装填忽略。

遥控器自动化（future_截图翻译功能补充_遥控截图自动化）：宿主经
attach_automation 注入自动化控制器后，本控制器把 A 单击与 B 装填变化
转发给它（自动化开启时接管 A 单击的首轮/重启轮语义），并管理「隐藏
遥控器」的用户级显隐（show_windows 尊重，防手动抓屏把 A/B 放出来）。
显隐初值取配置 ``remote_screenshot_hide``：true 时进入快捷键模式生成
A/B 后保持隐藏（右键菜单初始「显示遥控器」），此后以用户操作为准。

与截图窗口池的隔离（同 mouse_lock 约定）：A/B 由本控制器自持，不进
QuickMenu 截图窗口池 —— lock_layout / unlock_layout / locked_windows /
close_all 均不触及本功能窗口。

坐标系：相交检测在 Qt 全局逻辑坐标（B 的 geometry() vs 截图窗口
geometry()，快捷键模式下窗口隐藏但几何保留）。

可测逻辑（相交判定、装填决策）抽为模块级纯函数；控制器测试经构造注入
stub 窗口工厂与屏幕中心源，不构造真实 QWidget（见
tests/test_remote_screenshot.py）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from PyQt6.QtGui import QColor

from .. import layout

logger = logging.getLogger("renpy_overlay.screenshot.remote_screenshot")

#: A/B 初始摆放间隔（像素）：主屏可用区中心左右并排（需求未指定位置，默认值）
INITIAL_GAP = 20

Rect = tuple[int, int, int, int]  # (x, y, w, h)，Qt 全局逻辑坐标


def window_rect(window) -> Rect:
    """窗口对象的 Qt 全局逻辑矩形（鸭子类型：geometry() 的 x/y/width/height）。"""
    geo = window.geometry()
    return (geo.x(), geo.y(), geo.width(), geo.height())


def rects_intersect(a: Rect, b: Rect) -> bool:
    """两个 (x, y, w, h) 矩形是否相交（纯函数；仅贴边相触不算相交）。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def resolve_armed(
    b_rect: Rect,
    candidates: Sequence[tuple[Rect, object]],
    current: object | None,
) -> object | None:
    """窗口 B 的装填决策（纯函数，需求"只选择第一个进入的截图区域"）。

    - ``current``（当前装填目标）仍在候选中且与 B 相交 → 驻留返回
      ``current``（不响应后续进入的区域）；
    - 否则返回第一个与 B 相交的候选（按锁定序，即截图窗口创建序）；
    - 无命中返回 None（恢复圆形；离开后允许进入下一个区域）。
    """
    if current is not None:
        for rect, window in candidates:
            if window is current:
                if rects_intersect(b_rect, rect):
                    return current
                break  # 已离开驻留区域：解除，进入下方重新选择
    for rect, window in candidates:
        if rects_intersect(b_rect, rect):
            return window
    return None


def _default_window_factory(kind: str, diameter: int, on_event) -> object:
    """真实窗口工厂：kind 为 "A" 触发器 / "B" 瞄准器（惰性导入便于离线测试）。"""
    from .remote_screenshot_window import RemoteArmWindow, RemoteTriggerWindow

    if kind == "A":
        return RemoteTriggerWindow(diameter, on_click=on_event)
    return RemoteArmWindow(diameter, on_drag_move=on_event)


def _default_screen_center() -> tuple[int, int]:
    """主屏可用区域中心（避开任务栏）；无屏回退 (400, 300)（同 QuickMenu 约定）。"""
    from PyQt6.QtWidgets import QApplication  # noqa: PLC0415 - 惰性导入

    screen = QApplication.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        return geo.center().x(), geo.center().y()
    return 400, 300


def _default_screen_rects() -> list[Rect]:
    """全部屏幕可用区域（Qt 全局逻辑坐标，与 layout 恢复坐标同一口径）。"""
    from PyQt6.QtWidgets import QApplication  # noqa: PLC0415 - 惰性导入

    screens = QApplication.screens()
    if not screens:
        return [(0, 0, 1920, 1080)]
    return [
        (geo.x(), geo.y(), geo.width(), geo.height())
        for screen in screens
        for geo in (screen.availableGeometry(),)
    ]


class RemoteScreenshotController:
    """遥控截图控制器：A/B 窗口生命周期 + B 的装填状态 + A 的触发分发。

    构造注入（同 QuickMenu/HotkeyMode 的宿主能力注入约定，不依赖宿主类型）：

    - ``locked_windows_provider``：返回全部双击锁定截图窗口
      （QuickMenu.locked_windows，含边框色可作目标标识）；
    - ``trigger``：截图翻译入口（宿主 request_screenshot_translation，含
      在途互斥与既有提示规则——本控制器不重复实现互斥）；
    - ``window_factory`` / ``screen_center_provider``：窗口工厂与屏幕中心
      源（离线测试注入 stub，缺省为真实实现）；
    - ``screen_rects_provider``：全部屏幕可用区域源（layout 恢复坐标的
      离屏校验用；离线测试注入 stub，缺省为真实实现）；
    - ``user_hidden``：「隐藏遥控器」用户级显隐初值（config.
      remote_screenshot_hide；true 时 A/B 生成后保持隐藏，此后以用户操作为准）。

    仅 Qt 主线程使用。
    """

    def __init__(
        self,
        enable: bool,
        diameter_a: int,
        diameter_b: int,
        locked_windows_provider: Callable[[], list],
        trigger: Callable[[object], None] | None = None,
        window_factory: Callable[[str, int, object], object] | None = None,
        screen_center_provider: Callable[[], tuple[int, int]] | None = None,
        screen_rects_provider: Callable[[], list[Rect]] | None = None,
        user_hidden: bool = False,
    ) -> None:
        self._enable = bool(enable)
        self._diameter_a = int(diameter_a)
        self._diameter_b = int(diameter_b)
        self._locked_windows_provider = locked_windows_provider
        self._trigger = trigger
        self._window_factory = window_factory or _default_window_factory
        self._screen_center = screen_center_provider or _default_screen_center
        self._screen_rects = screen_rects_provider or _default_screen_rects
        self._window_a: object | None = None
        self._window_b: object | None = None
        self._armed: object | None = None  # B 当前装填的截图窗口（None = 圆形）
        # 遥控器自动化控制器（宿主经 attach_automation 注入；未注入时全部旁路）
        self._automation = None
        # 「隐藏遥控器」的用户级显隐态：抓屏恢复 show_windows 时尊重（不放出）；
        # 初值取配置 remote_screenshot_hide（true 时生成后保持隐藏，此后以用户操作为准）
        self._user_hidden = bool(user_hidden)
        # layout 恢复基线（需求 future_恢复布局.txt 补充）：持久持有、每次
        # 创建 A/B 都消费（快捷键模式关闭再开启的重建同样按 layout 摆放）；
        # None = 无基线（默认屏幕中心并排，改动前行为）
        self._layout_restore: layout.RemoteScreenshotLayout | None = None

    def attach_automation(self, automation) -> None:
        """注入遥控器自动化控制器（A 单击接管与 B 装填变化转发；同宿主附着约定）。"""
        self._automation = automation

    def set_layout_restore(self, state: layout.RemoteScreenshotLayout | None) -> None:
        """注入 layout 恢复基线（宿主启动时调用；None = 清除）。

        基线持久持有、每次创建 A/B 都消费：启动恢复（快捷键模式开启时）与
        运行期关闭再开启快捷键模式的重建均按基线摆放与重绑装填（需求五）。
        是否生成仍由 config 的 remote_screenshot_enable 决定，生成后是否
        立即显示仍由 remote_screenshot_hide 决定——本基线只覆盖几何与
        装填状态，不写回 config（需求四.1）。
        """
        self._layout_restore = state

    @property
    def windows_created(self) -> bool:
        """A/B 是否已生成（自动化开启前提：快捷键模式开启中）。"""
        return self._window_a is not None or self._window_b is not None

    @property
    def user_hidden(self) -> bool:
        """A/B 是否被用户隐藏（「隐藏遥控器」菜单态，自动化控制器文案用）。"""
        return self._user_hidden

    def set_user_hidden(self, hidden: bool) -> None:
        """「隐藏遥控器/显示遥控器」：用户级显隐（自动化继续运行，需求决议 A6）。

        菜单点击后的显隐以本入口为准，配置初值仅在控制器构造时生效一次。
        """
        self._user_hidden = bool(hidden)
        if self._user_hidden:
            self.hide_windows()
        else:
            self.show_windows()

    # ---- 快捷键模式生命周期（HotkeyMode 模式开/关时通知） ---------------------

    def on_hotkey_mode_changed(self, enabled: bool) -> None:
        """快捷键模式开/关：生成/销毁 A/B（幂等；配置开关关闭时不生成）。

        生成受用户级显隐约束（remote_screenshot_hide=true 时不自动显示，
        见 :meth:`_create_windows`）；重建沿用最近用户态（以用户操作为准）。
        """
        if enabled:
            if not self._enable:
                logger.info("遥控截图开关已关闭，进入快捷键模式不生成窗口 A/B")
                return
            if self._window_a is not None or self._window_b is not None:
                return  # 幂等：已生成
            self._create_windows()
        else:
            if self._automation is not None:
                # 遥控器自动化：A/B 随模式退出销毁，自动化复位为关（用户决议 A3）
                self._automation.on_hotkey_mode_changed(False)
            self._destroy_windows()

    def _create_windows(self) -> None:
        self._window_a = self._window_factory(
            "A", self._diameter_a, self.on_trigger_requested
        )
        self._window_b = self._window_factory(
            "B", self._diameter_b, self.update_arm_state
        )
        if not self._user_hidden:
            for window in (self._window_a, self._window_b):
                window.show()  # 先 show 再定位（Qt 渲染管线要求，同 QuickMenu）
        if self._layout_restore is not None:
            self._apply_layout_restore()
        else:
            self._place_windows()
        logger.info(
            "遥控截图窗口 A/B 已生成（直径 %d/%d，%s）",
            self._diameter_a,
            self._diameter_b,
            "保持隐藏（remote_screenshot_hide）" if self._user_hidden else "已显示",
        )

    def _destroy_windows(self) -> None:
        self._armed = None
        if self._window_a is None and self._window_b is None:
            return
        for window in (self._window_a, self._window_b):
            if window is not None:
                window.close()
                window.deleteLater()
        self._window_a = None
        self._window_b = None
        logger.info("遥控截图窗口 A/B 已销毁")

    def _place_windows(self) -> None:
        """A/B 以主屏可用区中心为基准左右并排（A 左 B 右，间隔 INITIAL_GAP）。"""
        if self._window_a is None or self._window_b is None:
            return
        cx, cy = self._screen_center()
        a_rect = window_rect(self._window_a)
        b_rect = window_rect(self._window_b)
        total = a_rect[2] + INITIAL_GAP + b_rect[2]
        a_x = cx - total // 2
        self._window_a.move(a_x, cy - a_rect[3] // 2)
        self._window_b.move(a_x + a_rect[2] + INITIAL_GAP, cy - b_rect[3] // 2)

    # ---- layout 恢复基线（宿主启动时注入；创建时消费） ------------------------

    def _apply_layout_restore(self) -> None:
        """按 layout 基线摆放 A/B 并重绑装填（需求 future_恢复布局.txt 补充）。

        直径：经 ``setFixedSize`` 应用记录的运行时值（窗口绘制按 rect 自
        适应，几何语义不变）；离屏校验前先应用直径，回退摆放按新直径并排。
        位置：任一窗记录坐标落到当前任一屏幕之外（分辨率/显示器变化）→
        整体回退屏幕中心默认并排 + WARNING（避免 A/B 分居两处）。
        装填：按记录边框色在锁定窗口列表中匹配（同快捷键模块"以色找窗"
        模式），目标不存在或未锁定 → INFO 回退圆形待命；命中转发自动化
        （与 :meth:`update_arm_state` 同一转发语义）。
        """
        state = self._layout_restore
        if self._window_a is None or self._window_b is None:
            return
        screens = self._screen_rects()
        on_screen = all(
            layout.rect_intersects_any_screen(spec.x, spec.y, spec.diameter, spec.diameter, screens)
            for spec in (state.a, state.b)
        )
        for spec, window in ((state.a, self._window_a), (state.b, self._window_b)):
            window.setFixedSize(spec.diameter, spec.diameter)
        if on_screen:
            self._window_a.move(state.a.x, state.a.y)
            self._window_b.move(state.b.x, state.b.y)
        else:
            logger.warning("layout 记录的遥控器坐标落到屏幕之外，回退屏幕中心默认摆放")
            self._place_windows()
        if state.loaded_color is not None:
            self._restore_armed_by_color(state.loaded_color)
        logger.info(
            "遥控截图窗口 A/B 已按 layout 恢复（直径 %d/%d，装填=%s，%s）",
            state.a.diameter,
            state.b.diameter,
            state.loaded_color or "待命",
            "已显示" if not self._user_hidden else "保持隐藏（remote_screenshot_hide）",
        )

    def _restore_armed_by_color(self, color_name: str) -> None:
        """按 layout 记录的边框色重绑 B 装填目标（目标缺失回退待命，不异常）。"""
        wanted = color_name.strip().lower()
        target = next(
            (
                window
                for window in self._locked_windows_provider()
                if getattr(window, "border_color", None) is not None
                and QColor(window.border_color).name().lower() == wanted
            ),
            None,
        )
        if target is None:
            logger.info("layout 记录的装填目标截图区域不存在，窗口 B 保持圆形待命")
            return
        self._armed = target
        self._window_b.set_armed(target.border_color)
        if self._automation is not None:
            self._automation.on_armed_changed(target)
        logger.info("遥控截图窗口 B 已按 layout 恢复装填（边框色 %s）", wanted)

    # ---- B 的装填状态机（B 拖拽移动时回调） -----------------------------------

    def update_arm_state(self) -> None:
        """B 拖拽移动后重估装填状态：候选现取自锁定窗口列表（防御状态漂移）。"""
        if self._window_b is None:
            return
        candidates = [
            (window_rect(window), window) for window in self._locked_windows_provider()
        ]
        armed = resolve_armed(window_rect(self._window_b), candidates, self._armed)
        if armed is self._armed:
            return
        self._armed = armed
        color = armed.border_color if armed is not None else None
        self._window_b.set_armed(color)
        if self._automation is not None:
            # 装填变化转发：自动化控制器据此停止/恢复周期并压制提示框
            self._automation.on_armed_changed(armed)
        if armed is not None:
            logger.debug("遥控截图窗口 B 已装填截图窗口（边框色 %s）", color.name())
        else:
            logger.debug("遥控截图窗口 B 已解除装填，恢复圆形")

    # ---- A 的触发分发（A 左键单击回调） ---------------------------------------

    def on_trigger_requested(self) -> None:
        """窗口 A 左键单击：已装填 → 触发宿主截图翻译；未装填 → 忽略。

        自动化开启时单击交给自动化控制器（首轮/重启一轮语义，用户决议 A5）；
        关闭时保留原有单击遥控截图（走宿主入口：在途互斥、既有提示、隐藏/
        抓屏/恢复时序全部复用）。在途互斥由宿主 request_screenshot_translation
        负责（同一时刻只允许一个在途 API），本控制器不重复实现。
        """
        if self._armed is None:
            logger.info("遥控截图：窗口 B 未装填任何已锁定的截图区域，忽略截图翻译请求")
            return
        if self._automation is not None and self._automation.enabled:
            self._automation.on_trigger_clicked(self._armed)
            return
        if callable(self._trigger):
            self._trigger(self._armed)

    # ---- 截图期间隐藏/恢复与置顶/退出清理（宿主时序调用） -----------------------

    def hide_windows(self) -> None:
        """抓屏前隐藏 A/B（需求：截图时 A 和 B 都隐藏）；未生成时 no-op。"""
        if self._window_a is not None:
            self._window_a.hide()
        if self._window_b is not None:
            self._window_b.hide()

    def show_windows(self) -> None:
        """抓屏后恢复显示 A/B（不受布局锁定分支影响）；未生成时 no-op。

        用户已通过「隐藏遥控器」隐藏时不放出（尊重用户态，防止一次手动
        抓屏把用户刻意隐藏的 A/B 放出来）。
        """
        if self._user_hidden:
            return
        if self._window_a is not None:
            self._window_a.show()
        if self._window_b is not None:
            self._window_b.show()

    def managed_windows(self) -> list[object]:
        """A/B 窗口对象清单（已创建者，A 先 B 后；托盘显隐快照与恢复用）。

        返回真实窗口对象（鸭子协议 isVisible/hide/show），调用方逐窗
        处理显隐——尊重/修改 user_hidden 的语义不经由此处（托盘快照以
        isVisible 实测为准，user_hidden 的 A/B 本就不可见、不会被恢复）。
        未生成时返回空列表。
        """
        return [
            window
            for window in (self._window_a, self._window_b)
            if window is not None
        ]

    def layout_snapshot(self):
        """A/B 几何与装填目标色快照（宿主退出保存用）；未生成返回 None。

        返回 ``(a_x, a_y, a_diameter, b_x, b_y, b_diameter, loaded_color)``
        纯值元组。不读 isVisible：「隐藏遥控器」或托盘隐藏期间照常记录
        几何与装填（需求二.4/三.3）；颜色经 ``QColor.name()`` 规范化
        （#rrggbb，None = 圆形待命）。坐标与现有截图窗/日志窗同口径
        （Qt 全局逻辑坐标，保存 API = 恢复 API）。
        """
        if not self.windows_created:
            return None
        a = window_rect(self._window_a)
        b = window_rect(self._window_b)
        color = self._armed.border_color.name() if self._armed is not None else None
        return (a[0], a[1], a[2], b[0], b[1], b[2], color)

    def reassert_topmost(self) -> None:
        """把 A/B 重新压回最顶层（对抗独占全屏被激活时的覆盖）。"""
        from .. import win32api  # 局部导入：仅 Windows 存在

        for window in (self._window_a, self._window_b):
            if window is None:
                continue
            try:
                win32api.set_topmost(window.hwnd)
            except Exception:  # pragma: no cover - 窗口销毁竞态
                return

    def shutdown(self) -> None:
        """宿主退出时调用：级联停止自动化并销毁 A/B（幂等；未生成时无操作）。"""
        if self._automation is not None:
            self._automation.shutdown()
        self._destroy_windows()
