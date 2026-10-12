"""跟随定位的门面 Mixin：周期定位游戏窗口、win32 物理像素成对摆放与置顶重申。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``_screenshot_suppress`` / ``_context_menu_open`` /
``_tray`` /
``skip_injection`` / ``_target_hwnd`` / ``target_pid`` / ``_locked`` /
``_dragging`` / ``width`` / ``height`` / ``_title_h`` / ``_title_gap`` /
``dock`` / ``_user_offset`` / ``_visible`` / ``_body_hwnd`` /
``_title_hwnd`` / ``_body_window`` / ``_last_topmost_log`` /
``_quick_menu`` / ``_remote_screenshot``，以及 ``_update_title`` /
``_set_visible``。
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtWidgets import QApplication

from .. import win32api
from .geometry import compute_geometry, pair_layout

logger = logging.getLogger("renpy_overlay.stream_window")


class WindowFollowMixin:
    """游戏窗口跟随、跳过注入的虚拟屏幕摆放、锁定态周期置顶重申。"""

    # ------------------------------------------------------------ 跟随与定位

    def _resolve_target(self) -> int:
        hwnd = self._target_hwnd
        if hwnd and win32api.is_window_valid(hwnd):
            return hwnd
        hwnd = win32api.find_main_window(self.target_pid) or 0
        if hwnd != self._target_hwnd:
            logger.info("已定位游戏窗口：HWND=0x%X", hwnd)
        self._target_hwnd = hwnd
        return hwnd

    def _follow(self) -> None:
        if self._closed:
            return
        try:
            self._follow_once()
        except Exception:  # pragma: no cover - 窗口在跟随过程中关闭
            logger.exception("跟随游戏窗口时出错")

    def _follow_once(self) -> None:
        if self._screenshot_suppress or self._context_menu_open:
            # 截图瞬间：不定位不重显示，防跟随循环把隐藏窗口拉回；
            # 右键菜单打开期间（无论锁定/解锁）：解锁态的周期定位也带
            # move_window(HWND_TOPMOST)，会把流式窗重新提到菜单之上遮住菜单
            return
        if self._tray is not None and self._tray.hidden:
            # 托盘「隐藏窗口」期间：不定位不重显示，防解锁态每 tick 的
            # _set_visible(True) 把流式窗拉回；锁定态置顶重申同被短路
            # （窗口全隐时无意义），托盘恢复后周期自然接续
            return
        if self.skip_injection:
            self._follow_once_skip_injection()
            return
        hwnd = self._resolve_target()
        if not hwnd:
            self._update_title(f"等待 pid={self.target_pid} 的游戏窗口…")
            self._hide_if_visible()
            return
        if win32api.is_minimized(hwnd):
            self._hide_if_visible()
            return
        if self._locked or self._dragging:
            # 位置已锁定 / 拖动中：跟随逻辑不移动窗口，但锁定态必须周期性对两窗
            # 重申 TOPMOST（独占全屏的游戏窗口被激活时会盖住其它 topmost 窗口）。
            if self._locked and not self._dragging:
                self._reassert_topmost()
            return

        rect = win32api.window_rect(hwnd)
        total = compute_geometry(
            rect,
            (self.width, self.height + self._title_h + self._title_gap),
            self.dock,
            self._user_offset,
        )
        title_rect, body_rect = pair_layout(total, self._title_h, self._title_gap)
        if not self._visible:
            # 显示必须走 Qt show()：win32 的 SWP_SHOWWINDOW 只能在 OS 层面把原生
            # 窗口标记为可见，绕过 Qt 的 show 流程后 Qt 不会启动渲染管线，
            # 对全透明窗口来说等于不存在（实测表现为"窗口从未出现"）。
            self._set_visible(True)
        win32api.move_window(self._body_hwnd, *body_rect, topmost=True)
        win32api.move_window(self._title_hwnd, *title_rect, topmost=True)

    def _hide_if_visible(self) -> None:
        if self._visible:
            self._set_visible(False)

    def _follow_once_skip_injection(self) -> None:
        """跳过注入模式的跟随：无游戏窗口可定位，以主屏为虚拟游戏窗口摆放。

        需求（future_跳过注入.txt）：保持置顶，用户手动以窗口化运行游戏。
        以主屏可用区域为虚拟游戏窗口走 compute_geometry 同款停靠/钳制与
        pair_layout 拆窗，首次定位后显示（无窗口时不像常规模式那样隐藏）。
        锁定/拖动时不移动（与常规模式一致）；置顶由每次定位的
        ``move_window(topmost=True)`` 建立，锁定态的周期重申沿用常规路径。
        """
        if self._tray is not None and self._tray.hidden:
            # 托盘「隐藏窗口」期间同常规路径短路（防御显式化：当前仅经
            # _follow_once 进入，未来直接调用路径同样受守卫）
            return
        if self._locked or self._dragging:
            if self._locked and not self._dragging:
                self._reassert_topmost()
            return
        rect = self._virtual_screen_rect()
        total = compute_geometry(
            rect,
            (self.width, self.height + self._title_h + self._title_gap),
            self.dock,
            self._user_offset,
        )
        title_rect, body_rect = pair_layout(total, self._title_h, self._title_gap)
        if not self._visible:
            self._set_visible(True)
        win32api.move_window(self._body_hwnd, *body_rect, topmost=True)
        win32api.move_window(self._title_hwnd, *title_rect, topmost=True)

    def _virtual_screen_rect(self) -> tuple[int, int, int, int]:
        """主屏可用区域（避开任务栏）的物理像素 rect（跳过注入的虚拟游戏窗口）。"""
        dpr = max(1.0, self._body_window.dpr())
        screen = QApplication.primaryScreen()
        if screen is None:  # pragma: no cover - 无显示环境兜底
            return (0, 0, 1920, 1080)
        geo = screen.availableGeometry()  # Qt 逻辑像素，进程 DPI Aware 与物理一致比例换算
        return (
            round(geo.x() * dpr),
            round(geo.y() * dpr),
            round((geo.x() + geo.width()) * dpr),
            round((geo.y() + geo.height()) * dpr),
        )

    def _reassert_topmost(self) -> None:
        """把两窗重新压回最顶层（不改位置/尺寸/焦点）；日志节流避免刷屏。"""
        if self._context_menu_open:
            return  # 右键菜单打开期间不与菜单竞争 TOPMOST 层组的顶部位置
        try:
            win32api.set_topmost(self._body_hwnd)
            win32api.set_topmost(self._title_hwnd)
            self._quick_menu.reassert_topmost()  # 截图窗锁定态同样对抗独占全屏覆盖
            self._remote_screenshot.reassert_topmost()  # 遥控截图 A/B 随周期一并重申
        except Exception:  # pragma: no cover - pywin32 缺失等
            return
        now = time.time()
        if now - self._last_topmost_log > 10.0:
            self._last_topmost_log = now
            logger.debug("锁定态周期性重申置顶（应对独占全屏被激活时的覆盖）")

    def _reset_to_dock(self) -> None:
        """清除手动拖动的位置，回到自动停靠。"""
        if self._user_offset is None:
            logger.info("当前已是自动停靠位置，无需恢复。")
            return
        self._user_offset = None
        logger.info("已恢复自动停靠位置（dock=%s）", self.dock)
        try:
            self._follow_once()
        except Exception:  # pragma: no cover
            logger.debug("恢复停靠时立即定位失败", exc_info=True)
