"""声波图控件：分段波形绘制 + 切片蒙版 + 片段拖拽吸附 + 进度指针。

语义（future_*.txt 声波图章节 + 已定案决策）：
- 「抽取音频轨生成声波图，对声波图编辑即为对整个视频编辑」——点击/
  拖拽经 :class:`~renpy_overlay.video.clip.editing.model.TimelineModel` 非线性映射；
- 切片蒙版：半透明淡蓝覆盖切片区间（源时间与片段求交映射到序列），
  点击蒙版跳到切片起点播放至终点自动暂停（多切片互不干扰）；
- 裁剪后的多段可拖拽横向移动（按住段拖动），接近邻居边缘自动吸附；
- 进度指针：引擎源时间经映射，落在片段范围外（被删区间）时隐藏——
  「进度标记指针落在声波图外视为超出视频范围」。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from .editing.model import TimelineModel
from .player_pane import frame_duration_ms
from .view_state import ZOOM_IN_FACTOR, ZOOM_OUT_FACTOR, ViewState
from .waveform import WaveformData

logger = logging.getLogger("renpy_overlay.video.clip.waveform_view")

BG = QColor(24, 24, 28)
GAP_BG = QColor(16, 16, 19)
WAVE = QColor(90, 160, 220)
MASK = QColor(80, 160, 255, 70)  # 半透明淡蓝（文档指定）
SELECTED_EDGE = QColor(240, 170, 60)
POINTER = QColor(240, 170, 60)

#: 拖拽超过该位移（像素）判定为「移动段」而非点击 seek
DRAG_THRESHOLD_PX = 5
#: 吸附距离（像素）：目标位置接近邻居边缘时自动贴合
SNAP_PX = 8


def clip_visible_range(
    clip_start_ms: int, clip_end_ms: int, view_start_ms: int, view_end_ms: int
) -> tuple[int, int] | None:
    """片段区间与可视窗口的交集（毫秒）；无交集返回 ``None``。

    纯函数（离线单测口径）：波形绘制列数必须以「可视交集」为界——
    按整段 clip 像素宽度逐列绘制时，长视频放大视野会产生数十万次
    fillRect（27 分钟素材在 10s 视野实测单次重绘 ~310ms / 22.6 万列，
    塞满 UI 线程 GIL，饿死音频回调/渲染/解码线程，起播断供风暴）。
    """
    lo = max(clip_start_ms, view_start_ms)
    hi = min(clip_end_ms, view_end_ms)
    if hi <= lo:
        return None
    return lo, hi


class WaveformView(QWidget):
    """声波图：编辑区域主体（选中/拖拽/蒙版交互）。"""

    seek_requested = pyqtSignal(int)  # 源时间（毫秒）
    clip_play_requested = pyqtSignal(int, int)  # (src_start, src_end) 播放切片
    clip_move_committed = pyqtSignal(int, object)  # (clip_id, 拖拽前快照) 入撤销栈
    selection_made = pyqtSignal(bool)  # 声波图是否有选中段（x/del 门控）

    def __init__(
        self,
        view_state: ViewState,
        model: TimelineModel,
        markers,
        *,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._view = view_state
        self._model = model
        self._markers = markers
        self._wave: WaveformData | None = None
        self._position_ms: int | None = None
        self._press_x = 0.0
        self._press_clip_id: int | None = None
        self._dragging = False
        self._drag_origin = 0
        self._snapshot_before_drag: list | None = None  # 拖拽前快照（回退用）
        self.setMinimumHeight(110)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        view_state.changed.connect(self.update)
        model.clips_changed.connect(self.update)
        markers.changed.connect(self.update)

    # ---- 数据注入 ---------------------------------------------------------------

    def set_waveform(self, wave: WaveformData | None) -> None:
        self._wave = wave
        self.update()

    def set_position(self, timeline_ms: int | None) -> None:
        self._position_ms = timeline_ms
        self.update()

    # ---- 坐标换算 ---------------------------------------------------------------

    def _px_per_ms(self) -> float:
        return max(1e-6, self.width() / max(1, self._view.span_ms))

    def _t_to_x(self, t_ms: float) -> float:
        return (t_ms - self._view.start_ms) * self._px_per_ms()

    def _x_to_t(self, x: float) -> int:
        return int(self._view.start_ms + x / self._px_per_ms())

    def _snap_ms(self) -> float:
        return SNAP_PX / self._px_per_ms()

    # ---- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), BG)
        self._draw_segments(painter)
        self._draw_masks(painter)
        self._draw_pending_line(painter)
        self._draw_pointer(painter)
        painter.end()

    def _draw_pending_line(self, painter: QPainter) -> None:
        """切片起点白线：f 首按后 pending 位置画垂直白线提高辨识度；
        蒙版建立（终点标记完成）后 pending 清空，白线自然消失。"""
        pending = self._markers.pending_start()
        if pending is None:
            return
        # 源时间 → 输出轴：找包含该源点的片段换算；落在被删区间则画在
        # 前段终点（输出游标）处——与蒙版映射规则一致
        x = None
        for clip in self._model.clips():
            if clip.source_start_ms <= pending < clip.source_end_ms:
                t_out = clip.timeline_start_ms + (
                    pending - clip.source_start_ms
                )
                x = self._t_to_x(t_out)
                break
        if x is None:
            return
        painter.setPen(QPen(Qt.GlobalColor.white, 2))
        painter.drawLine(int(x), 0, int(x), self.height())

    def _draw_segments(self, painter: QPainter) -> None:
        """按片段非线性绘制波形（删除/重排后序列≠源时间）。

        绘制列裁剪到可视窗口（:func:`clip_visible_range`）：窗口外的
        整段此前也在全量逐列绘制（Qt 会裁掉离屏列，但 PyQt 调用开销
        照付），长视频放大视野下单次重绘数百毫秒，是起播断供风暴的
        根因（引擎指标链：断供→时钟重跳→丢帧→presented 减半）。
        """
        mid = self.height() / 2
        view_start = self._view.start_ms
        view_end = view_start + self._view.span_ms
        for clip in self._model.clips():
            x0 = self._t_to_x(clip.timeline_start_ms)
            x1 = self._t_to_x(clip.timeline_end_ms)
            width = int(x1 - x0)
            visible = clip_visible_range(
                clip.timeline_start_ms, clip.timeline_end_ms, view_start, view_end
            )
            if self._wave is None:
                # 间隙底色在无波形时也要可见（拖拽排序的视觉锚）；
                # 与可视窗口无交集则无可画区域
                if visible is None:
                    continue
                painter.fillRect(int(x0), 0, max(2, width), self.height(), GAP_BG)
                continue
            if visible is None:
                continue
            vis_lo, vis_hi = visible
            # 片段内 1ms 时间轴 = 1ms 源区间：可视交集线性映射回源区间，
            # 列数 = 可视像素宽（≤控件宽度）
            src_lo = clip.source_start_ms + (vis_lo - clip.timeline_start_ms)
            src_hi = clip.source_start_ms + (vis_hi - clip.timeline_start_ms)
            vis_x0 = self._t_to_x(vis_lo)
            vis_width = max(1, int(self._t_to_x(vis_hi) - vis_x0))
            buckets = self._wave.window_peaks(src_lo, src_hi, vis_width)
            for i, peak in enumerate(buckets):
                half = max(1.0, peak * (self.height() / 2 - 4))
                painter.fillRect(
                    int(vis_x0) + i, int(mid - half), 1, int(half * 2), WAVE
                )
            if clip.clip_id == self._model.selected_id():
                painter.setPen(QPen(SELECTED_EDGE, 2))
                painter.drawRect(int(x0), 1, max(2, width - 1), self.height() - 2)

    def _draw_masks(self, painter: QPainter) -> None:
        """切片蒙版：切片源区间与各片段源区间求交 → 序列区间覆盖淡蓝。"""
        for seg in self._markers.segments():
            for clip in self._model.clips():
                src_lo = max(seg.start_ms, clip.source_start_ms)
                src_hi = min(seg.end_ms, clip.source_end_ms)
                if src_lo >= src_hi:
                    continue
                t_lo = clip.timeline_start_ms + (src_lo - clip.source_start_ms)
                t_hi = clip.timeline_start_ms + (src_hi - clip.source_start_ms)
                painter.fillRect(
                    int(self._t_to_x(t_lo)),
                    0,
                    max(2, int(self._t_to_x(t_hi) - self._t_to_x(t_lo))),
                    self.height(),
                    MASK,
                )

    def _draw_pointer(self, painter: QPainter) -> None:
        if self._position_ms is None:
            return
        x = self._t_to_x(self._position_ms)
        if not (-2 <= x <= self.width() + 2):
            return
        painter.setPen(QPen(POINTER, 2))
        painter.drawLine(int(x), 0, int(x), self.height())

    # ---- 交互 ---------------------------------------------------------------

    def _segment_at_mask(self, t_ms: int):
        """命中切片蒙版 → 返回 (src_start, src_end)（播放用）。"""
        for seg in self._markers.segments():
            for clip in self._model.clips():
                src_lo = max(seg.start_ms, clip.source_start_ms)
                src_hi = min(seg.end_ms, clip.source_end_ms)
                if src_lo >= src_hi:
                    continue
                t_lo = clip.timeline_start_ms + (src_lo - clip.source_start_ms)
                t_hi = clip.timeline_start_ms + (src_hi - clip.source_start_ms)
                if t_lo <= t_ms < t_hi:
                    return seg.start_ms, seg.end_ms
        return None

    def _clip_at(self, t_ms: int):
        for clip in self._model.clips():
            if clip.timeline_start_ms <= t_ms < clip.timeline_end_ms:
                return clip
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        t = self._x_to_t(event.position().x())
        self._press_x = event.position().x()
        mask = self._segment_at_mask(t)
        clip = self._clip_at(t)
        if mask is not None:
            # 点击蒙版：跳到切片起点播放至终点暂停（editor 侧实现自动暂停）
            self.clip_play_requested.emit(*mask)
            event.accept()
            return
        if clip is not None:
            self._model.set_selected(clip.clip_id)
            self.selection_made.emit(True)
            self._press_clip_id = clip.clip_id
            self._drag_origin = clip.timeline_start_ms
            self._snapshot_before_drag = self._model.snapshot()  # 拖拽前快照
            self.update()
            event.accept()
            return
        # 空白：仅当落在片段序列范围内才 seek（映射回源时间；间隙视为范围外）
        src = self._model.timeline_to_source(t)
        self.selection_made.emit(False)
        self._model.set_selected(None)
        if src is not None:
            self.seek_requested.emit(src)
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._press_clip_id is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and abs(event.position().x() - self._press_x) > DRAG_THRESHOLD_PX
        ):
            self._dragging = True
            delta_ms = int(
                (event.position().x() - self._press_x) / self._px_per_ms()
            )
            target = self._apply_snap(self._press_clip_id, self._drag_origin + delta_ms)
            self._model.move(self._press_clip_id, target)  # 实时预览（含防重叠钳制）
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            clip_id, self._press_clip_id = self._press_clip_id, None
            snapshot, self._snapshot_before_drag = self._snapshot_before_drag, None
            self.clip_move_committed.emit(clip_id, snapshot)  # editor 入栈撤销命令
            event.accept()
            return
        if self._press_clip_id is not None:  # 未超阈值的段上点击：seek 到点击处
            self._press_clip_id = None
            self._snapshot_before_drag = None
            src = self._model.timeline_to_source(self._x_to_t(event.position().x()))
            if src is not None:
                self.seek_requested.emit(src)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        anchor = self._x_to_t(event.position().x())
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = ZOOM_IN_FACTOR if event.angleDelta().y() > 0 else ZOOM_OUT_FACTOR
            self._view.zoom(factor, anchor)
        else:
            delta = int(self._view.span_ms * 0.15) * (1 if event.angleDelta().y() > 0 else -1)
            self._view.set(self._view.start_ms + delta, self._view.span_ms)
        event.accept()

    def _apply_snap(self, clip_id: int, target: int) -> int:
        """拖拽吸附：目标接近邻居边缘（SNAP_PX 内）自动贴合。"""
        moving = self._model.get_clip(clip_id)
        if moving is None:
            return target
        snap = self._snap_ms()
        edges = []
        for other in self._model.clips():
            if other.clip_id == clip_id:
                continue
            edges.extend((other.timeline_start_ms, other.timeline_end_ms))
        for edge in edges:
            if abs(target - edge) < snap:
                return int(edge)
        return target

    # fps 透传（帧步进由 editor 计算即可，此处保留接口对称性）
    def frame_ms(self, fps: float) -> int:
        return frame_duration_ms(fps)
