"""时间轴控件：自适应刻度（时间→帧过渡）+ 片段区带 + 纵贯进度指针。

刻度「从时间到帧位置的过渡变化」（future_*.txt）：按可视窗口密度自动
切换——窗口粗时显示时间刻度（1s/5s/10s…自适应），放大到每帧占像素
足够时切换为帧刻度（显示帧号）。

交互：ctrl+滚轮缩放（与声波图共享 :class:`ViewState`）、鼠标滚轮平移、
单击/拖拽进度条跳转（节流由上层 seek 链路统一处理）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .editing.model import TimelineModel
from .player.sync_clock import DEFAULT_FPS
from .player_pane import format_duration, frame_duration_ms
from .view_state import ZOOM_IN_FACTOR, ZOOM_OUT_FACTOR, ViewState

logger = logging.getLogger("renpy_overlay.video.clip.timeline_view")

#: 候选刻度间隔（毫秒）：自小到大取首个像素间距达标的间隔
TICK_STEPS_MS = (100, 200, 500, 1_000, 2_000, 5_000, 10_000, 30_000, 60_000)
MIN_TICK_PX = 60
#: 帧刻度模式的最小帧距（像素）：低于该值保持时间刻度
FRAME_TICK_MIN_PX = 10
BG = QColor(28, 28, 32)
FG = QColor(190, 190, 195)
CLIP_BAR = QColor(70, 110, 170)
POINTER = QColor(240, 170, 60)


class TimelineView(QWidget):
    """时间轴：刻度 + 片段区带 + 进度指针。"""

    seek_requested = pyqtSignal(int)  # 序列时间（毫秒）

    def __init__(
        self, view_state: ViewState, model: TimelineModel, *, parent=None
    ) -> None:
        super().__init__(parent)
        self._view = view_state
        self._model = model
        self._fps = DEFAULT_FPS
        self._position_ms = 0  # 序列时间（引擎源时间经映射，None 时保持旧值）
        self._dragging = False
        self.setMinimumHeight(44)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        view_state.changed.connect(self.update)

    def set_fps(self, fps: float) -> None:
        self._fps = fps if fps and fps > 0 else DEFAULT_FPS

    def set_position(self, timeline_ms: int | None) -> None:
        """进度指针（序列时间；None=超出片段范围，指针隐藏）。"""
        self._position_ms = timeline_ms
        self.update()

    # ---- 坐标换算 ---------------------------------------------------------------

    def _px_per_ms(self) -> float:
        return max(1e-6, self.width() / max(1, self._view.span_ms))

    def _t_to_x(self, t_ms: int) -> float:
        return (t_ms - self._view.start_ms) * self._px_per_ms()

    def _x_to_t(self, x: float) -> int:
        return int(self._view.start_ms + x / self._px_per_ms())

    # ---- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), BG)
        self._draw_clips(painter)
        self._draw_ticks(painter)
        self._draw_pointer(painter)
        painter.end()

    def _draw_clips(self, painter: QPainter) -> None:
        height = self.height()
        for clip in self._model.clips():
            x0 = self._t_to_x(clip.timeline_start_ms)
            x1 = self._t_to_x(clip.timeline_end_ms)
            painter.fillRect(
                int(x0), height - 16, max(2, int(x1 - x0)), 12, CLIP_BAR
            )
            if clip.clip_id == self._model.selected_id():
                painter.setPen(QPen(POINTER, 2))
                painter.drawRect(int(x0), height - 16, int(x1 - x0), 12)

    def _draw_ticks(self, painter: QPainter) -> None:
        """自适应刻度：时间模式（粗粒度）/ 帧模式（细粒度过渡）。"""
        px_per_ms = self._px_per_ms()
        frame_ms = frame_duration_ms(self._fps)
        frame_mode = px_per_ms * frame_ms >= FRAME_TICK_MIN_PX
        painter.setPen(QPen(FG))
        if frame_mode:
            step_frames = max(1, int(FRAME_TICK_MIN_PX / (px_per_ms * frame_ms)))
            first_frame = int(self._view.start_ms // frame_ms)
            last_frame = int((self._view.start_ms + self._view.span_ms) // frame_ms) + 1
            for frame in range(first_frame - first_frame % step_frames, last_frame + 1, step_frames):
                x = self._t_to_x(frame * frame_ms)
                if x < -40 or x > self.width() + 40:
                    continue
                painter.drawLine(int(x), 0, int(x), 10)
                painter.drawText(int(x) + 3, 12, f"f{frame}")
        else:
            step_ms = TICK_STEPS_MS[-1]
            for candidate in TICK_STEPS_MS:
                if candidate * px_per_ms >= MIN_TICK_PX:
                    step_ms = candidate
                    break
            first = (self._view.start_ms // step_ms) * step_ms
            end = self._view.start_ms + self._view.span_ms
            tick = first
            while tick <= end:
                x = self._t_to_x(tick)
                if 0 <= x <= self.width():
                    painter.drawLine(int(x), 0, int(x), 10)
                    painter.drawText(int(x) + 3, 12, format_duration(tick))
                tick += step_ms

    def _draw_pointer(self, painter: QPainter) -> None:
        if self._position_ms is None:
            return
        x = self._t_to_x(self._position_ms)
        if not (-2 <= x <= self.width() + 2):
            return
        painter.setPen(QPen(POINTER, 2))
        painter.drawLine(int(x), 0, int(x), self.height())

    # ---- 交互 ---------------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802
        anchor = self._x_to_t(event.position().x())
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = ZOOM_IN_FACTOR if event.angleDelta().y() > 0 else ZOOM_OUT_FACTOR
            self._view.zoom(factor, anchor)
        else:
            delta = int(self._view.span_ms * 0.15) * (1 if event.angleDelta().y() > 0 else -1)
            self._view.set(self._view.start_ms + delta, self._view.span_ms)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.seek_requested.emit(self._x_to_t(event.position().x()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self.seek_requested.emit(self._x_to_t(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        super().mouseReleaseEvent(event)
