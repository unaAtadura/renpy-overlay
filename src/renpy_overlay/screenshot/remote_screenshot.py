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


class RemoteScreenshotController:
    """遥控截图控制器：A/B 窗口生命周期 + B 的装填状态 + A 的触发分发。

    构造注入（同 QuickMenu/HotkeyMode 的宿主能力注入约定，不依赖宿主类型）：

    - ``locked_windows_provider``：返回全部双击锁定截图窗口
      （QuickMenu.locked_windows，含边框色可作目标标识）；
    - ``trigger``：截图翻译入口（宿主 request_screenshot_translation，含
      在途互斥与既有提示规则——本控制器不重复实现互斥）；
    - ``window_factory`` / ``screen_center_provider``：窗口工厂与屏幕中心
      源（离线测试注入 stub，缺省为真实实现）。

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
    ) -> None:
        self._enable = bool(enable)
        self._diameter_a = int(diameter_a)
        self._diameter_b = int(diameter_b)
        self._locked_windows_provider = locked_windows_provider
        self._trigger = trigger
        self._window_factory = window_factory or _default_window_factory
        self._screen_center = screen_center_provider or _default_screen_center
        self._window_a: object | None = None
        self._window_b: object | None = None
        self._armed: object | None = None  # B 当前装填的截图窗口（None = 圆形）

    # ---- 快捷键模式生命周期（HotkeyMode 模式开/关时通知） ---------------------

    def on_hotkey_mode_changed(self, enabled: bool) -> None:
        """快捷键模式开/关：生成/销毁 A/B（幂等；配置开关关闭时不生成）。"""
        if enabled:
            if not self._enable:
                logger.info("遥控截图开关已关闭，进入快捷键模式不生成窗口 A/B")
                return
            if self._window_a is not None or self._window_b is not None:
                return  # 幂等：已生成
            self._create_windows()
        else:
            self._destroy_windows()

    def _create_windows(self) -> None:
        self._window_a = self._window_factory(
            "A", self._diameter_a, self.on_trigger_requested
        )
        self._window_b = self._window_factory(
            "B", self._diameter_b, self.update_arm_state
        )
        for window in (self._window_a, self._window_b):
            window.show()  # 先 show 再定位（Qt 渲染管线要求，同 QuickMenu）
        self._place_windows()
        logger.info(
            "遥控截图窗口 A/B 已生成（直径 %d/%d）", self._diameter_a, self._diameter_b
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
        if armed is not None:
            logger.debug("遥控截图窗口 B 已装填截图窗口（边框色 %s）", color.name())
        else:
            logger.debug("遥控截图窗口 B 已解除装填，恢复圆形")

    # ---- A 的触发分发（A 左键单击回调） ---------------------------------------

    def on_trigger_requested(self) -> None:
        """窗口 A 左键单击：已装填 → 触发宿主截图翻译；未装填 → 忽略。

        在途互斥由宿主 request_screenshot_translation 负责（同一时刻只允许
        一个在途 API），本控制器不重复实现。
        """
        if self._armed is None:
            logger.info("遥控截图：窗口 B 未装填任何已锁定的截图区域，忽略截图翻译请求")
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
        """抓屏后恢复显示 A/B（无条件恢复：不受布局锁定分支影响）；未生成时 no-op。"""
        if self._window_a is not None:
            self._window_a.show()
        if self._window_b is not None:
            self._window_b.show()

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
        """宿主退出时调用：销毁 A/B（幂等；未生成时无操作）。"""
        self._destroy_windows()
