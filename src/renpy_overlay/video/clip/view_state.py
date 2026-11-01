"""共享视图状态：时间轴与声波图同步缩放/滚动（future_*.txt：缩放同步进行）。

editor_window 持有单一实例，两控件共享引用：任何一方缩放改变可视窗口
后经 :attr:`changed` 信号驱动双方重绘，保持时间关系对应。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

#: 缩放边界：最小可视窗口（毫秒）——帧级观察；最大为序列总时长
MIN_SPAN_MS = 200
ZOOM_IN_FACTOR = 1.25
ZOOM_OUT_FACTOR = 0.8


class ViewState(QObject):
    """可视窗口状态：``[start, start + span)``（播放序列时间）。"""

    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._start_ms = 0
        self._span_ms = 10_000
        self._max_span_ms = 10_000

    @property
    def start_ms(self) -> int:
        return self._start_ms

    @property
    def span_ms(self) -> int:
        return self._span_ms

    @property
    def max_span_ms(self) -> int:
        return self._max_span_ms

    def set_bounds(self, total_duration_ms: int) -> None:
        """序列总时长变化（载入/删除/合并）时更新缩放上界并钳制窗口。"""
        self._max_span_ms = max(MIN_SPAN_MS, int(total_duration_ms))
        self.set(self._start_ms, self._span_ms)

    def set(self, start_ms: int, span_ms: int) -> None:
        span = int(min(max(MIN_SPAN_MS, span_ms), self._max_span_ms))
        start = int(min(max(0, start_ms), max(0, self._max_span_ms - span)))
        if start != self._start_ms or span != self._span_ms:
            self._start_ms, self._span_ms = start, span
            self.changed.emit()

    def zoom(self, factor: float, anchor_ms: int) -> None:
        """以 ``anchor``（序列时间，通常为光标处）为不动点缩放。"""
        new_span = self._span_ms * factor
        ratio = (anchor_ms - self._start_ms) / max(1, self._span_ms)
        new_start = anchor_ms - ratio * new_span
        self.set(int(new_start), int(new_span))
