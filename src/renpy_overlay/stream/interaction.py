"""鼠标交互的门面 Mixin：整体拖动、位置锁定、单击/双击延迟判定、右键转发。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``_locked`` / ``_dragging`` / ``_drag_grab`` / ``_drag_size`` /
``_drag_origin`` / ``_press_pos`` / ``_click_suppress_until`` /
``_body_window`` / ``_body_hwnd`` / ``_title_hwnd`` / ``_title_h`` /
``_title_gap`` / ``_user_offset`` / ``skip_injection`` / ``_click_timer`` /
``_context_menu_open`` / ``_quick_menu`` / ``_remote_screenshot`` /
``_last_topmost_log``，以及 ``_resolve_target`` / ``_reassert_topmost`` /
``_abort_translation`` / ``_abort_song_recognition`` / ``_start_translation`` /
``_virtual_screen_rect``。
"""

from __future__ import annotations

import logging
import time

from PyQt6.QtWidgets import QWidget

from .. import win32api
from .constants import CLICK_MOVE_TOLERANCE, CLICK_SUPPRESS_SECONDS
from .geometry import pair_offset_from_body

logger = logging.getLogger("renpy_overlay.stream_window")


class MouseInteractionMixin:
    """窗口拖动、锁定态拖拽滚动喂入、单双击判定与右键菜单转发。"""

    # ------------------------------------------------------------ 鼠标拖动

    def _physical_mouse(self, window: QWidget, event) -> tuple[int, int]:
        """Qt 逻辑全局坐标 → 屏幕物理像素（与 win32 坐标系对齐）。"""
        point = event.globalPosition() * window.dpr()
        return int(point.x()), int(point.y())

    def on_window_press(self, window: QWidget, event) -> None:
        """左键按下：记录点击候选；未锁定时开始整体拖动（两窗联动）。"""
        if self._closed:
            return
        pos = self._physical_mouse(window, event)
        self._press_pos = pos
        if self._locked:
            if window is self._body_window:
                # 锁定态按住正文文字拖动 = 拖拽滚动（仅锁定时生效，避免与
                # 未锁定时的整体拖动冲突）；单击判定照旧由 release 处理
                self._body_window.begin_drag_scroll(event.position().y())
            else:
                logger.debug("位置已锁定，忽略拖动按下")
            return
        left, top, right, bottom = win32api.window_rect(self._body_hwnd)
        self._dragging = True
        self._drag_grab = (pos[0] - left, pos[1] - top)
        self._drag_size = (max(1, right - left), max(1, bottom - top))
        self._drag_origin = (left, top)
        logger.info(
            "开始拖动流式悬浮窗：正文窗位于 (%d, %d)，鼠标抓取点 (%d, %d)",
            left,
            top,
            pos[0] - left,
            pos[1] - top,
        )

    def on_window_motion(self, window: QWidget, event) -> None:
        """拖动中：两窗跟随鼠标整体移动（保持抓取点相对位置不变）。"""
        if self._body_window.drag_scroll_active:
            # 锁定态拖拽滚动进行中：纵坐标喂给正文窗更新速度档位
            self._body_window.update_drag_scroll(event.position().y())
            return
        if not self._dragging:
            return
        pos = self._physical_mouse(window, event)
        body_x = pos[0] - self._drag_grab[0]
        body_y = pos[1] - self._drag_grab[1]
        try:
            win32api.move_window(
                self._body_hwnd,
                body_x,
                body_y,
                self._drag_size[0],
                self._drag_size[1],
                topmost=True,
                resize=False,
            )
            win32api.move_window(
                self._title_hwnd,
                body_x,
                body_y - self._title_h - self._title_gap,
                self._drag_size[0],
                self._title_h,
                topmost=True,
                resize=False,
            )
        except Exception:  # pragma: no cover - 窗口在拖动中销毁
            self._dragging = False

    def on_window_release(self, window: QWidget, event) -> None:
        if self._closed:
            return
        self._body_window.end_drag_scroll()  # 未在拖拽滚动时为无害调用
        if self._dragging:
            self._finish_drag()
        press_pos = self._press_pos
        self._press_pos = None
        if press_pos is None:
            return
        pos = self._physical_mouse(window, event)
        if (
            abs(pos[0] - press_pos[0]) > CLICK_MOVE_TOLERANCE
            or abs(pos[1] - press_pos[1]) > CLICK_MOVE_TOLERANCE
        ):
            return  # 移动过 = 拖动，不算单击
        if time.time() < self._click_suppress_until:
            return  # 双击/三击的余波，不调度单击
        self._schedule_click()

    def _finish_drag(self) -> None:
        """结束拖动：把当前位置换算成相对游戏窗口的偏移并锁定（供跟随逻辑使用）。"""
        self._dragging = False
        try:
            rect = win32api.window_rect(self._body_hwnd)
        except Exception:  # pragma: no cover - 窗口已销毁
            return
        if (rect[0], rect[1]) == self._drag_origin:
            logger.debug("拖动结束：位置未变化，保持原有跟随方式")
            return
        hwnd = self._resolve_target()
        if hwnd:
            game = list(win32api.window_rect(hwnd))
        elif self.skip_injection:
            # 跳过注入模式：无游戏窗口可定位，改以主屏可用区域（虚拟游戏
            # 窗口）左上为参照 —— 与 _follow_once_skip_injection 同一参照系。
            # 否则 _user_offset 永远写不进去，follow 每 tick 按默认停靠把
            # 窗口弹回屏幕顶部（实测缺陷：拖动结果不被保持）
            game = list(self._virtual_screen_rect())
            logger.debug("拖动结束：以虚拟屏幕为参照记录偏移（跳过注入模式）")
        else:
            logger.warning("拖动结束：未能定位游戏窗口，本次位置不参与跟随")
            return
        # user_offset 必须与跟随循环的整体几何顶点（标题窗左上角）同参照系，
        # 否则松手后首次重定位会整体下移「标题高 + 间距」（见 pair_offset_from_body）。
        # 绑定入口横向追加在标题窗右侧且由标题内容宽驱动让位（同一渲染帧内绘制），
        # 不参与本换算
        offset = pair_offset_from_body(
            (rect[0], rect[1]),
            (game[0], game[1]),
            self._title_h,
            self._title_gap,
        )
        self._user_offset = offset
        logger.info(
            "拖动结束：位置已锁定为相对游戏窗口的偏移 (%+d, %+d)（控制台输入 d 可恢复自动停靠）",
            offset[0],
            offset[1],
        )

    # ------------------------------------------------------------ 双击锁定与单击翻译

    def on_window_double_click(self, window: QWidget, event) -> None:
        """双击：中止在途翻译并切换位置锁定。"""
        if self._closed:
            return
        self._cancel_pending_click()
        self._click_suppress_until = time.time() + CLICK_SUPPRESS_SECONDS
        if self._dragging:
            # 双击的第二次按下可能顺带开了拖动，直接取消，避免它的 release 产生副作用
            self._dragging = False
        self._abort_translation("双击")
        self._abort_song_recognition("双击")
        self._toggle_lock()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        if self._locked:
            self._lock_started_at = time.time()
            if self._auto_translate_on:
                logger.info("已锁定悬浮窗位置（自动翻译开启：等一个轮询间隔后开始）")
                self._update_title("已锁定，开始自动翻译")
            else:
                logger.info("已锁定悬浮窗位置（双击解锁；锁定状态下单击触发翻译）")
                self._update_title("已锁定，单击启动翻译")
        else:
            logger.info("已解锁悬浮窗位置（恢复鼠标拖动与自动跟随；自动翻译停用）")
            self._update_title("已解锁，中止翻译")

    def _schedule_click(self) -> None:
        """延迟判定单击：等一个系统双击间隔，双击会取消该任务。"""
        self._cancel_pending_click()
        self._click_timer.start()

    def _cancel_pending_click(self) -> None:
        self._click_timer.stop()

    def _on_delayed_click(self) -> None:
        if self.skip_injection:
            # 跳过注入模式：无游戏文本捕获（需求：单击翻译不可用）
            logger.info("跳过注入模式：单击翻译不可用（需注入捕获游戏文本）")
            return
        if not self._locked:
            logger.debug("未锁定状态，单击不触发翻译")
            return
        logger.debug("锁定状态下的单击：请求翻译当前对话原文")
        self._start_translation()

    def on_window_context_menu(self, window: QWidget, event) -> None:
        """右键转发终点：菜单构建与动作分发全部在 QuickMenu 内。

        exec 阻塞期间置位 ``_context_menu_open``，暂停锁定态每 200ms 的
        周期性置顶重申 —— 否则流式窗会被 reassert 到 TOPMOST 层组顶部，
        反过来盖住刚弹出的菜单。
        """
        self._context_menu_open = True
        try:
            self._quick_menu.show_context_menu(event.globalPos())
        finally:
            self._context_menu_open = False
            self._reassert_topmost()  # 菜单关闭后立即恢复两窗的置顶层级
