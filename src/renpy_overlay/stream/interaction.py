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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget

from .. import win32api
from .choice_log_window import CURSOR_BY_EDGE
from .constants import CLICK_MOVE_TOLERANCE, CLICK_SUPPRESS_SECONDS
from .geometry import (
    MIN_STREAM_HEIGHT,
    MIN_STREAM_WIDTH,
    apply_resize_delta,
    edge_at,
    finish_resize_dims,
    pair_layout,
    pair_offset_from_body,
    window_resize_edge,
)

logger = logging.getLogger("renpy_overlay.stream_window")


class MouseInteractionMixin:
    """窗口拖动、锁定态拖拽滚动喂入、八向拉伸、单双击判定与右键菜单转发。

    八向拉伸（未锁定专属）：按下落在任一窗的边缘/角热区（6px → 12px）
    即进入拉伸手势——两窗整体几何按边/角增量调整（标题窗管北向/上角、
    正文窗管南向/下角、东西两边两窗皆可拖，总宽同步），跟随循环经
    ``_dragging`` 短路不干扰；结束把新尺寸写回宿主运行时尺寸（跟随以
    此重摆，不回写 config）并按拖动移动同款语义记录相对偏移。锁定态不
    响应拉伸（仅拖拽滚动/无边缘提示）。
    """

    # ------------------------------------------------------------ 鼠标拖动

    def _physical_mouse(self, window: QWidget, event) -> tuple[int, int]:
        """Qt 逻辑全局坐标 → 屏幕物理像素（与 win32 坐标系对齐）。"""
        point = event.globalPosition() * window.dpr()
        return int(point.x()), int(point.y())

    def on_window_press(self, window: QWidget, event) -> None:
        """左键按下：未锁定先判边缘/角热区（命中 → 八向拉伸）；否则记录
        点击候选并开始整体拖动（两窗联动）。"""
        if self._closed:
            return
        pos = self._physical_mouse(window, event)
        if not self._locked:
            local = event.position().toPoint()
            edge = edge_at(local.x(), local.y(), window.width(), window.height())
            edge = window_resize_edge(
                edge, "title" if window is self._title_window else "body"
            )
            if edge is not None:
                # 拉伸激活：置 _dragging 短路跟随循环（200ms tick 会按配置
                # 尺寸重摆两窗，会把拉伸结果覆盖）。
                # 拉伸基准必须取「整体物理矩形」（标题窗左上 + 正文窗右下
                # 合成）——正文窗矩形不含标题带，用它当整体基准会让北/上向
                # 拉伸首帧整体高度塌缩一个标题带。
                # 坐标系说明：window_rect/move_window/compute_geometry 同处
                # win32 物理像素体系，增量直接用物理鼠标坐标相减——不引入
                # dpr 换算（实测多除一次 dpr 会让窗口尺寸每次拉伸减半）。
                title_rect = win32api.window_rect(self._title_hwnd)
                body_rect = win32api.window_rect(self._body_hwnd)
                left = min(title_rect[0], body_rect[0])
                top = min(title_rect[1], body_rect[1])
                right = max(title_rect[2], body_rect[2])
                bottom = max(title_rect[3], body_rect[3])
                self._dragging = True
                self._resizing = True
                self._resize_edge = edge
                self._drag_last = (pos[0], pos[1])
                self._drag_origin = (left, top)
                self._drag_size = (max(1, right - left), max(1, bottom - top))
                # 最小尺寸钳制与 compute/move_window 同体系（win32 物理像素）
                self._resize_min = (
                    MIN_STREAM_WIDTH,
                    self._title_h + self._title_gap + MIN_STREAM_HEIGHT,
                )
                logger.info("开始拉伸流式悬浮窗：%s 边/角", edge)
                return
            # 观测点（DEBUG）：排查「边缘按下去无拉伸」时打开——记录热区判
            # 定的输入与结果（本地坐标/角色/命中边），未命中即落入整体拖动
            logger.debug(
                "拉伸热区判定未命中：role=%s local=(%d,%d) edge=%r → 整体拖动",
                "title" if window is self._title_window else "body",
                local.x(),
                local.y(),
                edge,
            )
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
        if self._resizing:
            # 八向拉伸：以按下时的整体物理几何为基准，叠加「自按下起的累计
            # 位移」换算（每帧重算，钳制含最小尺寸）。
            # _drag_last 保持 press 时的坐标不更新——若每帧重写 _drag_last，
            # delta 退化为帧间增量，与冻结基准混用后每帧输出只在基准附近
            # 抖动（实测「向外拉不放大」根因）。
            pos = self._physical_mouse(window, event)
            x, y, w, h = apply_resize_delta(
                self._drag_origin[0],
                self._drag_origin[1],
                self._drag_size[0],
                self._drag_size[1],
                self._resize_edge,
                pos[0] - self._drag_last[0],
                pos[1] - self._drag_last[1],
                self._resize_min[0],
                self._resize_min[1],
            )
            title_rect, body_rect = pair_layout((x, y, w, h), self._title_h, self._title_gap)
            # 观测点（DEBUG，约 200ms 节流）：定位「越拉越小/几何未生效」——
            # 对比输出几何与窗口实际尺寸即可区分「换算/基准缺陷」与「move 失败」
            now = time.time()
            if now - self._resize_debug_last >= 0.2:
                self._resize_debug_last = now
                logger.debug(
                    "拉伸中：edge=%s 输出=(%d,%d,%d,%d) min=(%d,%d) 基准=(%d,%d,%d,%d)",
                    self._resize_edge,
                    x,
                    y,
                    w,
                    h,
                    MIN_STREAM_WIDTH,
                    self._title_h + self._title_gap + MIN_STREAM_HEIGHT,
                    self._drag_origin[0],
                    self._drag_origin[1],
                    self._drag_size[0],
                    self._drag_size[1],
                )
            try:
                win32api.move_window(self._title_hwnd, *title_rect, topmost=True)
                win32api.move_window(self._body_hwnd, *body_rect, topmost=True)
            except Exception as exc:  # pragma: no cover - 窗口在拉伸中销毁
                # 观测点（WARNING）：move 失败 = 几何未生效的静默盲区，必须留痕
                logger.warning("拉伸 move_window 失败：%r", exc)
                self._dragging = False
                self._resizing = False
            return
        if not self._dragging:
            # 空闲悬停（未按下）：边缘/角热区切换拉伸光标与边缘高亮（仅未
            # 锁定；锁定态不可拉伸，保持默认箭头）。状态变化才打 DEBUG 观测
            # 日志（进入/离开热区各一条，不刷屏）
            if not self._locked:
                local = event.position().toPoint()
                edge = edge_at(local.x(), local.y(), window.width(), window.height())
                edge = window_resize_edge(
                    edge, "title" if window is self._title_window else "body"
                )
                window.setCursor(CURSOR_BY_EDGE.get(edge, Qt.CursorShape.ArrowCursor))
                window.set_hover_highlight(edge)
                state = (window is self._title_window, edge)
                if state != self._resize_hover_state:
                    self._resize_hover_state = state
                    logger.debug(
                        "拉伸悬停：window=%s local=(%d,%d) edge=%r",
                        "title" if window is self._title_window else "body",
                        local.x(),
                        local.y(),
                        edge,
                    )
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
        if self._resizing:
            self._finish_resize()
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

    def _finish_resize(self) -> None:
        """结束拉伸：新尺寸写回宿主运行时尺寸（跟随循环以此重摆两窗，
        被游戏窗口约束的钳制仍会在后续 tick 生效；不回写 config，重启恢复
        配置默认），并按拖动移动同款语义记录相对偏移（涉及西/北方向时原
        点已平移，位置与尺寸一并保持）。

        坐标系：window_rect 与 move_window/compute_geometry 同为 win32 物理
        像素体系——直接写回，**不得再除以 dpr**（多除会把物理值降为逻辑值，
        而跟随重摆又以物理消费，实测窗口尺寸每次拉伸减半、单调递减）。
        高度口径：读回的是正文窗矩形（**不含标题带**），正文高直接写回
        self.height（跟随以 self.height + title_h + gap 还原总高）——不得
        再减 title_h + gap（重复扣减会让每次手势固定少一个标题带高度）。
        换算抽为纯函数 finish_resize_dims 便于离线单测。"""
        self._resizing = False
        self._resize_edge = None
        try:
            rect = win32api.window_rect(self._body_hwnd)
        except Exception:  # pragma: no cover - 窗口已销毁
            self._dragging = False
            return
        self.width, self.height = finish_resize_dims(
            rect[2] - rect[0], rect[3] - rect[1], MIN_STREAM_WIDTH
        )
        logger.info(
            "拉伸结束：整体尺寸调整为 %dx%d 逻辑像素（不持久化，重启恢复配置默认）",
            self.width,
            self.height + self._title_h + self._title_gap,
        )
        self._dragging = False
        self._finish_drag()  # 记相对偏移（西/北方向拉伸平移过原点，位置需保持）

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
        # 双击的第二次按下若落在拉伸热区也会顺带激活拉伸，同样取消（避免
        # 它的 release 产生尺寸副作用）；锁定切换后边缘不再响应拉伸
        self._resizing = False
        self._resize_edge = None
        self._abort_translation("双击")
        self._abort_song_recognition("双击")
        self._toggle_lock()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        # 观测：锁定状态切换无其他痕迹——复测报告需据此区分「未锁定操作」
        # 与「锁定态操作」（图标拖拽滚动等锁定专属分支依赖该状态）
        logger.info("位置%s（双击切换）", "已锁定" if self._locked else "已解锁")
        # 锁定态不可拉伸：清两窗的悬停高亮与拉伸光标（解锁恢复时由悬停
        # 重新点亮）；进行中的拉伸手势已被 on_window_double_click 取消
        self._body_window.set_hover_highlight(None)
        self._title_window.set_hover_highlight(None)
        self._body_window.setCursor(Qt.CursorShape.ArrowCursor)
        self._title_window.setCursor(Qt.CursorShape.ArrowCursor)
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
