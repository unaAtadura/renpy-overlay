"""单个截图窗口：半透明识别框风格的可拖拽 / 八向拉伸悬浮窗（PyQt6）。

视觉与交互移植自参考实现 ``screen_translaor_4.0/recognize_window.py``：
半透明黑色遮罩 + 彩色边框（创建顺序分配红橙黄绿青蓝紫黑）+ 鼠标靠近可
拉伸边缘时亮白反馈 + 四角手柄 + 拉伸中的实时尺寸标签。按需求裁剪：
无识别按钮、不显示识别框 ID。

交互约定（与流式悬浮窗一致）：
- 未锁定：左键按住内部拖动 / 边缘八向拉伸；
- 双击：仅切换自身锁定（不通知宿主 —— 双击解锁截图窗口不打断截图翻译）；
- 锁定：禁止拖动，内部单击经延迟判定（等一个系统双击间隔）后回调
  ``on_click``，由 :class:`~renpy_overlay.quick_menu.QuickMenu` 转发宿主
  触发截图翻译。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .. import win32api
from .window_visual import draw_edge_highlight, draw_grip_handles, draw_size_label, edge_at

logger = logging.getLogger("renpy_overlay.screenshot.window")

#: 单击延迟判定：等一个系统双击间隔再认定“单击”（双击会取消），与宿主同模式
CLICK_DELAY_MS = win32api.double_click_time_ms() + 60
#: 判定“点击”的最大位移（像素）：超过视为拖动/拉伸
CLICK_MOVE_TOLERANCE = 4

#: 边框颜色（需求：创建截图窗口时依次分配，红橙黄绿青蓝紫黑）。
#: 定义在窗口模块（边框色是窗口的固有属性）：快捷键模块以色找窗也用它
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


def needs_reshow_after_flags_change(was_visible: bool, now_visible: bool) -> bool:
    """窗口标志变更后是否需要补 ``show()``（纯函数，便于离线单测）。

    对已显示的顶层窗口修改窗口标志（如 ``WindowTransparentForInput``）
    会被 Qt 隐藏；仅当“原本可见且现在不可见”时需要补显示，其余组合
    （原本就隐藏、标志变更后仍可见）都不动，避免多余的 show 造成闪烁。
    """
    return was_visible and not now_visible


class ScreenshotWindow(QWidget):
    """一个截图区域框：彩色边框 + 八向拉伸 + 锁定态单击触发截图翻译。"""

    def __init__(
        self,
        index: int,
        border_color: QColor,
        on_click=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._border_color = QColor(border_color)
        self._on_click = on_click

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self.resize(300, 200)
        self.setMinimumSize(80, 60)

        self._locked = False
        self._dragging = False
        self._resizing = False
        self._resize_edge: str | None = None
        self._drag_offset = QPoint()
        self._drag_pos = QPoint()
        self._hovered_edge: str | None = None
        self._press_pos: QPoint | None = None
        self._press_global: QPoint | None = None

        # 启用鼠标追踪：悬停（未按下）状态下也触发 mouseMoveEvent 做边缘检测
        self.setMouseTracking(True)
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(CLICK_DELAY_MS)
        self._click_timer.timeout.connect(self._on_delayed_click)

    @property
    def hwnd(self) -> int:
        """原生窗口句柄（宿主用 ``win32api.window_rect`` 取物理像素截图区域）。"""
        return int(self.winId())

    @property
    def index(self) -> int:
        return self._index

    @property
    def border_color(self) -> QColor:
        return QColor(self._border_color)

    @property
    def is_locked(self) -> bool:
        """双击锁定状态（QuickMenu.locked_windows 以此筛“可识别”窗口）。"""
        return self._locked

    # ---- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        # 半透明黑色遮罩（与参考实现一致：轻微压暗 + 不完全遮挡截图区域）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 20))
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        # 彩色边框（alpha 180：与参考实现一致的可视强度）
        border = QColor(self._border_color)
        border.setAlpha(180)
        pen = QPen(border)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

        if self._locked:
            return  # 锁定态：不绘制手柄与边缘高亮，保持画面干净

        draw_grip_handles(painter, rect)
        if self._hovered_edge is not None and not self._resizing:
            draw_edge_highlight(painter, rect, self._hovered_edge)
        if self._resizing:
            draw_size_label(painter, rect, self.width(), self.height())

    # ---- 鼠标事件：拖拽 / 拉伸 / 延迟单击 / 双击自锁 -------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        pos = event.position().toPoint()
        self._press_pos = QPoint(pos)
        self._press_global = QPoint(event.globalPosition().toPoint())
        if self._locked:
            # 锁定态：边缘仍可拉伸，内部点击交给延迟判定触发截图翻译
            edge = edge_at(self, pos)
            if edge:
                self._start_resize(edge, event.globalPosition().toPoint())
            event.accept()
            return
        edge = edge_at(self, pos)
        if edge:
            self._start_resize(edge, event.globalPosition().toPoint())
        else:
            self._dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.geometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self._drag_pos = event.globalPosition().toPoint()
            self._do_resize(delta)
            event.accept()
            return
        if self._dragging:
            # 顶层窗口的 move() 即屏幕坐标：保持抓取点相对位置不变整体移动
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        if self._locked:
            event.accept()
            return
        self._sync_hover_edge(event.position().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_resizing = self._resizing
        self._dragging = False
        self._resizing = False
        self._resize_edge = None
        press_global = self._press_global
        self._press_pos = None
        self._press_global = None
        if was_resizing:
            # 拉伸结束后重算悬停边缘并重绘（清除尺寸标签）
            self._sync_hover_edge(self.mapFromGlobal(event.globalPosition().toPoint()))
            event.accept()
            return
        if press_global is None:
            event.accept()
            return
        moved = event.globalPosition().toPoint() - press_global
        if abs(moved.x()) > CLICK_MOVE_TOLERANCE or abs(moved.y()) > CLICK_MOVE_TOLERANCE:
            event.accept()
            return  # 移动过 = 拖动，不算单击
        if self._locked and not self._click_timer.isActive():
            self._click_timer.start()  # 延迟判定：双击会取消该任务
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._click_timer.stop()  # 双击取消单击判定
        if self._dragging:
            self._dragging = False
        self._toggle_lock()
        event.accept()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        if self._locked:
            self._dragging = False
            self._resizing = False
            self._resize_edge = None
            self._hovered_edge = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            logger.info("截图窗口 #%d 已锁定（单击触发截图翻译；双击解锁）", self._index)
        else:
            logger.info("截图窗口 #%d 已解锁（恢复拖动与拉伸）", self._index)
        self.update()

    def _on_delayed_click(self) -> None:
        if not self._locked:
            return
        logger.debug("截图窗口 #%d 锁定态单击：请求截图翻译", self._index)
        if callable(self._on_click):
            self._on_click(self)

    # ---- 拉伸与边缘检测（视觉细节在 window_visual 模块） ---------------------

    def _start_resize(self, edge: str, global_pos: QPoint) -> None:
        self._resizing = True
        self._resize_edge = edge
        self._drag_pos = QPoint(global_pos)

    def _sync_hover_edge(self, pos: QPoint) -> None:
        edge = edge_at(self, pos)
        self._update_cursor_for_edge(edge)
        if edge != self._hovered_edge:
            self._hovered_edge = edge
            self.update()

    def _update_cursor_for_edge(self, edge: str | None) -> None:
        cursors = {
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
            "n": Qt.CursorShape.SizeVerCursor,
            "s": Qt.CursorShape.SizeVerCursor,
            "w": Qt.CursorShape.SizeHorCursor,
            "e": Qt.CursorShape.SizeHorCursor,
        }
        self.setCursor(cursors.get(edge, Qt.CursorShape.ArrowCursor))

    def _do_resize(self, delta: QPoint) -> None:
        """按拖动增量执行八向拉伸（参考实现 _do_resize 的等价移植）。"""
        edge = self._resize_edge
        if edge is None:
            return
        geo = self.geometry()
        new_x, new_y = geo.x(), geo.y()
        new_w, new_h = geo.width(), geo.height()
        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        dx, dy = delta.x(), delta.y()
        if "e" in edge:
            new_w = max(min_w, geo.width() + dx)
        if "s" in edge:
            new_h = max(min_h, geo.height() + dy)
        if "w" in edge:
            new_w = max(min_w, geo.width() - dx)
            new_x = geo.x() + geo.width() - new_w
        if "n" in edge:
            new_h = max(min_h, geo.height() - dy)
            new_y = geo.y() + geo.height() - new_h
        self.setGeometry(new_x, new_y, new_w, new_h)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._hovered_edge is not None:
            self._hovered_edge = None
            self.update()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._locked:
            self._sync_hover_edge(self.mapFromGlobal(event.globalPosition().toPoint()))
        super().enterEvent(event)

    # ---- 供 QuickMenu 管理的辅助 -------------------------------------------

    def set_clickthrough(self, enable: bool) -> None:
        """设置/取消鼠标穿透（通用能力；当前快捷键模式已改为整体隐藏窗口
        方案、不再经此路径，保留供未来需要系统级穿透的功能使用）。

        调用方需自行管理 Qt/Win32 双层状态一致性（见下方三层协同与顺序
        说明）——这是实测中难以收敛的根源，慎用。

        三层协同（方向一致，互不冲突），确保穿透真实生效：
        - Qt ``WindowTransparentForInput`` 窗口标志（主力）：Qt 据此维护
          原生扩展样式 ``WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE``，
          系统级点击穿透到下层；由 Qt 管理可避免手动 SetWindowLong 被
          窗口状态变化时的样式重算覆盖（实测手动路径对 Qt 顶层窗口不可靠）。
        - ``WA_TransparentForMouseEvents`` 属性：Qt 事件层兜底，全部鼠标
          事件不分发（单击/双击/拖动/拉伸均不触发），属性方式无副作用。
        - win32 手动扩展样式 + ``SWP_FRAMECHANGED`` 刷新（win32api 层）再兜底。

        注意：修改顶层窗口标志会令 Qt 隐藏窗口（内部按重父化处理），因此
        标志变更后按 :func:`needs_reshow_after_flags_change` 对原本可见的
        窗口补一次 ``show()``——与截图翻译流程 hide_all()/show_all() 的先
        隐藏再显示同模式，同步执行不产生闪烁，且窗口已带
        ``WA_ShowWithoutActivating``，不会抢焦点。

        执行顺序关键：Qt 标志/属性变更（含补 show）必须在前，win32 手动
        样式 + ``SWP_FRAMECHANGED`` 收尾——命中测试缓存只在 FRAMECHANGED
        时刷新，若刷新发生在 Qt 清除穿透样式之前（如先 win32 后 Qt），
        退出时缓存里残留的还是穿透态，窗口将无法恢复交互（实测缺陷）。
        收尾刷新永远基于最终样式状态，进入/退出两侧都正确。

        取消穿透的确定性还原：Qt 清除窗口标志对 DWM 合成窗口的输入布局
        不总是即时生效（实测退出后穿透残留，且随原生窗口实例存在——重建
        窗口即恢复）。因此 enable=False 时在全部清除动作之后调用
        :meth:`recreate_native_window` 强制销毁重建原生窗口，按已无穿透
        标志的当前状态全新创建，任何形式的残留都物理消失。

        解锁后全部同时还原，不影响正常鼠标交互，也不改变双击锁定状态。
        单窗口失败（如销毁竞态）只记日志不抛：调用方是批量循环，不能让
        一个窗口拖垮整体。
        """
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, bool(enable))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, bool(enable))
        if needs_reshow_after_flags_change(was_visible, self.isVisible()):
            self.show()  # 修改标志被 Qt 隐藏：补显示，窗口持续可见且不闪烁
        try:
            # 收尾执行：SetWindowLong 后的 FRAMECHANGED 刷新基于最终样式，
            # 保证命中测试缓存与真实状态一致（进入与退出都生效）
            win32api.set_clickthrough(self.hwnd, enable)
        except Exception:  # pragma: no cover - 窗口销毁竞态
            logger.warning(
                "截图窗口 #%d 设置鼠标穿透失败（enable=%r）", self._index, enable
            )
        if not enable:
            self.recreate_native_window()

    def recreate_native_window(self) -> None:
        """销毁并重建本窗口的原生窗口（Qt 公开 API ``create()``）。

        用于取消穿透后的确定性还原：穿透相关残留（扩展样式位、DWM 输入
        布局、命中测试缓存）都绑定在原生窗口实例上，Qt 清标志/手动
        SetWindowLong + FRAMECHANGED 均无法解除（实测）；而原生窗口重建
        会按当前窗口标志全新创建——穿透标志已清，残留必然消失。

        几何、窗口标志与可见状态原样保留；不改变双击锁定状态；新原生
        窗口句柄由各调用方动态获取（winId()），无长期缓存可失效。
        """
        self.create(0, True, True)  # destroyOldWindow=True：旧原生窗口随之销毁

    def reposition_cascade(self, center: QPoint, cascade_step: int = 30) -> None:
        """把窗口摆到 ``center`` 为中心的级联位置（第 index 个偏移一步）。"""
        w, h = self.width(), self.height()
        offset = self._index * cascade_step
        self.move(center.x() - w // 2 + offset, center.y() - h // 2 + offset)
