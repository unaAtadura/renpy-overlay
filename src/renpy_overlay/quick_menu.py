"""流式悬浮窗右键快捷菜单管理器：菜单入口的唯一扩展点。

管理右键菜单的构建与动作分发，并托管截图窗口池（最多 8 个，创建顺序依次
分配红橙黄绿青蓝紫黑边框，销毁按堆栈优先最新）。设计约束：

- 不依赖宿主类型：宿主能力（截图翻译、打开历史）全部经构造回调注入，
  后续新功能 = 在此加菜单项 + 注入对应回调，宿主零改动；
- 所有方法仅在 Qt 主线程调用（右键事件本就在主线程，菜单动作就地分发，
  不经过宿主的消息队列）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QMenu

from .screenshot.window import ScreenshotWindow

logger = logging.getLogger("renpy_overlay.quick_menu")

#: 截图窗口数量上限（需求：最多创建 8 个）
MAX_WINDOWS = 8
#: 边框颜色（需求：创建截图窗口时依次分配，红橙黄绿青蓝紫黑）
FRAME_COLORS: tuple[QColor, ...] = (
    QColor(255, 50, 50),  # 红
    QColor(255, 165, 0),  # 橙
    QColor(255, 255, 0),  # 黄
    QColor(50, 205, 50),  # 绿
    QColor(0, 206, 209),  # 青
    QColor(30, 144, 255),  # 蓝
    QColor(160, 32, 240),  # 紫
    QColor(30, 30, 30),  # 黑
)
#: 级联创建时后一个窗口相对前一个的偏移（像素），避免完全重叠
CASCADE_STEP = 30


class QuickMenu:
    """右键快捷菜单 + 截图窗口池（仅 Qt 主线程使用）。"""

    def __init__(self, on_capture_click, on_open_history, on_recognize_song=None) -> None:
        self._on_capture_click = on_capture_click
        self._on_open_history = on_open_history
        self._on_recognize_song = on_recognize_song
        self._windows: list[ScreenshotWindow] = []  # 栈序 = 创建序，栈顶最新

    # ---- 右键菜单 -----------------------------------------------------------

    def show_context_menu(self, global_pos: QPoint) -> None:
        """构建并弹出右键菜单（标题窗 / 正文窗文字处右键触发）。

        菜单带 ``WindowStaysOnTopHint``：与流式双窗同属 TOPMOST 层组且后显示，
        保证菜单始终完整可见（配合宿主在菜单打开期间暂停周期性置顶重申，
        避免流式窗被 reassert 后反盖住菜单）。
        """
        menu = QMenu()
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        count = len(self._windows)
        create_action = menu.addAction(f"创建截图窗口（{count}/{MAX_WINDOWS}）")
        create_action.setEnabled(count < MAX_WINDOWS)
        destroy_action = menu.addAction("销毁截图窗口")
        destroy_action.setEnabled(count > 0)
        menu.addSeparator()
        history_action = menu.addAction("查看截图历史")
        menu.addSeparator()
        song_action = menu.addAction("听歌识曲")
        chosen = menu.exec(global_pos)
        if chosen is create_action:
            self.create_window()
        elif chosen is destroy_action:
            self.destroy_latest()
        elif chosen is history_action and callable(self._on_open_history):
            self._on_open_history()
        elif chosen is song_action and callable(self._on_recognize_song):
            self._on_recognize_song()

    # ---- 截图窗口池 ---------------------------------------------------------

    def create_window(self) -> ScreenshotWindow | None:
        """新建一个截图窗口（颜色按创建顺序分配）；已满返回 None。"""
        if len(self._windows) >= MAX_WINDOWS:
            logger.info("截图窗口已达上限 %d 个，忽略创建请求", MAX_WINDOWS)
            return None
        index = len(self._windows)
        window = ScreenshotWindow(index, FRAME_COLORS[index], on_click=self._on_capture_click)
        window.show()  # 先 show 再定位（Qt 渲染管线要求），随后级联摆放
        center = self._screen_center()
        w, h = window.width(), window.height()
        offset = index * CASCADE_STEP
        window.move(center.x() - w // 2 + offset, center.y() - h // 2 + offset)
        self._windows.append(window)
        logger.info(
            "已创建截图窗口 #%d（边框色 %s），当前 %d/%d 个",
            index,
            FRAME_COLORS[index].name(),
            len(self._windows),
            MAX_WINDOWS,
        )
        return window

    def destroy_latest(self) -> bool:
        """堆栈式销毁：优先销毁最新创建的窗口；无窗口返回 False。"""
        if not self._windows:
            logger.info("没有可销毁的截图窗口")
            return False
        window = self._windows.pop()
        logger.info("已销毁截图窗口 #%d（剩余 %d 个）", window.index, len(self._windows))
        window.close()
        window.deleteLater()
        return True

    def hide_all(self) -> None:
        """截图时短暂隐藏全部截图窗口（宿主负责流式窗口的隐藏）。"""
        for window in self._windows:
            window.hide()

    def show_all(self) -> None:
        """截图结束后恢复显示全部截图窗口。"""
        for window in self._windows:
            window.show()

    def close_all(self) -> None:
        """销毁全部截图窗口（宿主退出时调用）。"""
        for window in self._windows:
            window.close()
            window.deleteLater()
        self._windows.clear()

    def reassert_topmost(self) -> None:
        """把全部截图窗口重新压回最顶层（对抗独占全屏被激活时的覆盖）。"""
        from . import win32api  # 局部导入：仅 Windows 存在

        for window in self._windows:
            try:
                win32api.set_topmost(window.hwnd)
            except Exception:  # pragma: no cover - 窗口销毁竞态
                return

    @property
    def window_count(self) -> int:
        return len(self._windows)

    # ---- 内部 ---------------------------------------------------------------

    @staticmethod
    def _screen_center() -> QPoint:
        """主屏可用区域中心（避开任务栏），窗口初始摆放参照点。"""
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            return QPoint(geo.center().x(), geo.center().y())
        return QPoint(400, 300)
