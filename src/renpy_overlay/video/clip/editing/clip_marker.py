"""切片标记状态机：f 键的起点/终点标记与清除规则（独立于轨道片段语义）。

文档原文规则（future_*.txt 快捷按钮-标记切片）：

- f 在切片外 → 以进度标记为起点；再次按 f → 标记终点成段；
- 在已标记切片范围内（不含端点）按 f → 清除该切片；
- 新切片允许与旧切片相接，不允许重叠；
- 候选切片与已有切片重叠（起点或终点落入旧切片范围）→ 本次按键
  只清除旧切片，起点保持挂起（一次按键只做一步变更：清除与成段
  分两次 f 完成，下一次 f 才标记终点成段）；
- 若新切片覆盖了旧切片范围（旧切片 ⊆ 候选）→ 同样只清除、不标记
  新切片终点（起点保持挂起，等待下一次 f 重新标终点）。

端点位置按 f 不触发清除（「不包括端点位置」），走正常标记（相接允许，
:attr:`ClipSegment.overlaps` 为严格不等号语义）。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from .interfaces import ClipSegment


class ClipMarkerModel(QObject):
    """切片标记集合 + f 键状态机（挂起起点 → 成段/清除）。"""

    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._segments: list[ClipSegment] = []  # 按起点升序
        self._pending_start: int | None = None

    # ---- 查询 ---------------------------------------------------------------

    def segments(self) -> list[ClipSegment]:
        """当前全部切片（按起点升序的拷贝）。"""
        return list(self._segments)

    def pending_start(self) -> int | None:
        """挂起中的切片起点（无挂起为 ``None``；UI 提示用）。"""
        return self._pending_start

    def containing_segment(self, position_ms: int) -> ClipSegment | None:
        """命中位置（不含端点）的切片；无命中返回 ``None``。"""
        for seg in self._segments:
            if seg.contains(position_ms):
                return seg
        return None

    # ---- f 键状态机 -----------------------------------------------------------

    def mark(self, position_ms: int) -> None:
        """以进度标记为准执行一次 f 键（状态机主入口）。"""
        position_ms = max(0, int(position_ms))
        if self._pending_start is None:
            hit = self.containing_segment(position_ms)
            if hit is not None:
                # 规则：范围内（不含端点）按 f → 清除该切片（纯清除意图）
                self._remove(hit)
                return
            self._pending_start = position_ms  # 切片外：标记起点
            self.changed.emit()
            return
        self._finish_segment(position_ms)  # 有挂起起点：标终点（含重叠/覆盖规则）

    def cancel_pending(self) -> None:
        """取消挂起起点（不产生切片；换片/加载字幕等场景调用）。"""
        if self._pending_start is not None:
            self._pending_start = None
            self.changed.emit()

    def clear(self) -> None:
        """清空全部切片与挂起起点（加载字幕替换语义 / 换片）。"""
        if self._segments or self._pending_start is not None:
            self._segments = []
            self._pending_start = None
            self.changed.emit()

    def replace_all(self, segments: list[ClipSegment]) -> None:
        """整体替换（加载字幕：ASS 时间轴 → 切片映射）。"""
        self._segments = sorted(
            (ClipSegment(s.start_ms, s.end_ms, s.origin) for s in segments),
            key=lambda s: s.start_ms,
        )
        self._pending_start = None
        self.changed.emit()

    # ---- 内部 ---------------------------------------------------------------

    def _remove(self, seg: ClipSegment) -> None:
        self._segments.remove(seg)
        self.changed.emit()

    def _finish_segment(self, end_ms: int) -> None:
        """挂起起点 + 当前进度 → 新切片（重叠清除 / 覆盖不标终点规则）。"""
        start = self._pending_start
        if end_ms == start:
            return  # 零长度切片：忽略（起点保持挂起）
        lo, hi = (start, end_ms) if start < end_ms else (end_ms, start)
        candidate = ClipSegment(lo, hi)
        overlapping = [s for s in self._segments if s.overlaps(candidate)]
        if overlapping:
            # 分步时序：候选与旧切片重叠（含覆盖）→ 本次只清除旧切片，
            # 起点保持挂起，下一次 f 才完成成段（一次按键只做一步变更）
            for seg in overlapping:
                self._segments.remove(seg)
            self.changed.emit()
            return
        self._segments.append(candidate)
        self._segments.sort(key=lambda s: s.start_ms)
        self._pending_start = None
        self.changed.emit()
