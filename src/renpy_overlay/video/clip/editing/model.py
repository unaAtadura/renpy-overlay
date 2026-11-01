"""时间轴片段模型：播放序列 + 分割/删除/重排/合并 + 非线性时间映射。

语义（future_*.txt + 已定案决策）：

- 片段有序列表即播放序列；每段携带 ``timeline_start_ms``（序列位置），
  拖拽重排可产生间隙，:meth:`TimelineModel.join_all` 按当前顺序紧凑
  重排消除全部间隙（决策 1：c 键=紧凑重排，不物理合并相邻段）；
- :meth:`TimelineModel.delete` 移除选中段并收紧（后续段前移）——
  「视频裁剪后声波图与时间轴的非线性对应」即源于此：删除后序列时间
  ≠ 源时间，全部换算收敛到 :meth:`timeline_to_source` /
  :meth:`source_to_timeline` 两个纯函数；
- 音视频一体：切片/删除均针对「音视频同源同区间」的一体轨，天然满足
  「选中音频轨时同时删除对应的视频轨」的联动要求；
- 分割边界保护：进度标记为该段起始帧或末尾帧时不生效（文档原文）。

纯逻辑 + Qt 信号（QObject 信号发射不依赖 QApplication），离线单测覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PyQt6.QtCore import QObject, pyqtSignal


@dataclass
class TrackClip:
    """时间轴片段：源视频内区间 + 播放序列位置。"""

    clip_id: int
    source_path: str
    source_start_ms: int
    source_end_ms: int
    timeline_start_ms: int

    @property
    def duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms

    @property
    def timeline_end_ms(self) -> int:
        return self.timeline_start_ms + self.duration_ms

    def with_timeline_start(self, timeline_start_ms: int) -> TrackClip:
        return replace(self, timeline_start_ms=timeline_start_ms)


def _sorted_by_timeline(clips: list[TrackClip]) -> list[TrackClip]:
    return sorted(clips, key=lambda c: (c.timeline_start_ms, c.clip_id))


class TimelineModel(QObject):
    """播放序列模型：多段片段的分割/删除/移动/合并与时间映射。"""

    clips_changed = pyqtSignal()
    selection_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._clips: list[TrackClip] = []  # 按 timeline_start 升序维护
        self._selected_id: int | None = None
        self._next_id = 1

    # ---- 查询 ---------------------------------------------------------------

    def clips(self) -> list[TrackClip]:
        """当前片段（按播放序列位置升序的拷贝）。"""
        return list(self._clips)

    def get_clip(self, clip_id: int) -> TrackClip | None:
        for clip in self._clips:
            if clip.clip_id == clip_id:
                return clip
        return None

    def selected_id(self) -> int | None:
        return self._selected_id

    def total_duration_ms(self) -> int:
        """播放序列总时长（最右段终点；空序列为 0）。"""
        if not self._clips:
            return 0
        return max(c.timeline_end_ms for c in self._clips)

    def has_gaps(self) -> bool:
        """是否存在间隙（相邻段不衔接；拖拽后、合并前为 True）。"""
        ordered = self._clips
        return any(
            later.timeline_start_ms > prev.timeline_end_ms
            for prev, later in zip(ordered, ordered[1:], strict=False)
        )

    # ---- 载入与选中 -----------------------------------------------------------

    def load_source(self, source_path: str, duration_ms: int) -> TrackClip:
        """载入媒体为单一整段（替换现有序列：资源窗口双击载入语义）。"""
        clip = TrackClip(
            clip_id=self._next_id,
            source_path=source_path,
            source_start_ms=0,
            source_end_ms=max(0, int(duration_ms)),
            timeline_start_ms=0,
        )
        self._next_id += 1
        self._clips = [clip]
        self._selected_id = clip.clip_id
        self.clips_changed.emit()
        self.selection_changed.emit()
        return clip

    def set_selected(self, clip_id: int | None) -> None:
        if self._selected_id != clip_id:
            self._selected_id = clip_id
            self.selection_changed.emit()

    def selected_clip(self) -> TrackClip | None:
        return self.get_clip(self._selected_id) if self._selected_id is not None else None

    # ---- 编辑操作（命令层经快照调用；模型不发撤销语义） ----------------------------

    def split(self, clip_id: int, at_source_ms: int) -> None:
        """段内分割：at 为源时间，须严格落在段内（起始/末尾帧不生效）。"""
        clip = self.get_clip(clip_id)
        if clip is None:
            return
        if not (clip.source_start_ms < at_source_ms < clip.source_end_ms):
            return  # 边界保护：进度标记为该段起始帧或末尾帧时分割无效
        left_end = at_source_ms
        right = TrackClip(
            clip_id=self._next_id,
            source_path=clip.source_path,
            source_start_ms=at_source_ms,
            source_end_ms=clip.source_end_ms,
            timeline_start_ms=clip.timeline_start_ms + (left_end - clip.source_start_ms),
        )
        self._next_id += 1
        left = replace(clip, source_end_ms=at_source_ms)
        idx = self._clips.index(clip)
        self._clips[idx : idx + 1] = [left, right]
        self.clips_changed.emit()

    def delete(self, clip_id: int) -> None:
        """删除选中段并收紧：后续段整体前移被删段时长。"""
        clip = self.get_clip(clip_id)
        if clip is None:
            return
        idx = self._clips.index(clip)
        removed = clip.duration_ms
        rest = [
            c.with_timeline_start(
                c.timeline_start_ms - removed if c.timeline_start_ms >= clip.timeline_end_ms
                else c.timeline_start_ms
            )
            for c in self._clips[:idx] + self._clips[idx + 1 :]
        ]
        self._clips = _sorted_by_timeline(rest)
        if self._selected_id == clip_id:
            self._selected_id = None
            self.selection_changed.emit()
        self.clips_changed.emit()

    def move(self, clip_id: int, target_timeline_start_ms: int) -> bool:
        """拖拽移动段到目标序列位置（防重叠钳制到最近合法位置）。

        返回是否发生移动（钳制后与原位相同则为 False）。
        """
        clip = self.get_clip(clip_id)
        if clip is None:
            return False
        pos = self._clamp_position(clip, int(target_timeline_start_ms))
        if pos == clip.timeline_start_ms:
            return False
        idx = self._clips.index(clip)
        self._clips[idx] = clip.with_timeline_start(pos)
        self._clips = _sorted_by_timeline(self._clips)
        self.clips_changed.emit()
        return True

    def join_all(self) -> bool:
        """合并（c）：按当前顺序紧凑重排，消除全部间隙。

        返回是否存在变化（已紧凑则为 False）。
        """
        if not self.has_gaps():
            return False
        cursor = 0
        ordered = []
        for clip in self._clips:  # self._clips 已按 timeline 升序
            ordered.append(clip.with_timeline_start(cursor))
            cursor += clip.duration_ms
        self._clips = ordered
        self.clips_changed.emit()
        return True

    def restore(self, clips: list[TrackClip]) -> None:
        """全量恢复（命令 revert 的统一出口；外部须传快照拷贝）。"""
        self._clips = _sorted_by_timeline([replace(c) for c in clips])
        if self._selected_id is not None and self.get_clip(self._selected_id) is None:
            self._selected_id = None
            self.selection_changed.emit()
        self.clips_changed.emit()

    def snapshot(self) -> list[TrackClip]:
        """当前序列快照（命令 apply 前记录）。"""
        return [replace(c) for c in self._clips]

    # ---- 时间映射（非线性对应唯一入口） -------------------------------------------

    def timeline_to_source(self, t_ms: int) -> int | None:
        """序列时间 → 源时间；落在间隙/越界时返回 ``None``（范围外不跳转）。"""
        for clip in self._clips:
            if clip.timeline_start_ms <= t_ms < clip.timeline_end_ms:
                return clip.source_start_ms + (t_ms - clip.timeline_start_ms)
            if t_ms < clip.timeline_start_ms:
                break  # 升序：后续更不可能命中
        return None

    def source_to_timeline(self, s_ms: int) -> int | None:
        """源时间 → 序列时间；不在任何段源区间内返回 ``None``。

        播放位置回报（引擎为源时间）到时间轴显示的唯一换算路径。
        """
        for clip in self._clips:
            if clip.source_start_ms <= s_ms < clip.source_end_ms:
                return clip.timeline_start_ms + (s_ms - clip.source_start_ms)
        return None

    # ---- 内部 ---------------------------------------------------------------

    def _clamp_position(self, moving: TrackClip, target: int) -> int:
        """防重叠钳制：目标合法直接采用；被挡则跳到移动方向上最近的合法
        位置（解析式跳过障碍物边缘），无可行位置保持原位。"""
        target = max(0, int(target))
        others = [c for c in self._clips if c.clip_id != moving.clip_id]
        if not others:
            return target

        def legal(pos: int) -> bool:
            cand = moving.with_timeline_start(pos)
            return not any(
                cand.timeline_start_ms < o.timeline_end_ms
                and o.timeline_start_ms < cand.timeline_end_ms
                for o in others
            )

        if legal(target):
            return target
        origin = moving.timeline_start_ms
        duration = moving.duration_ms
        if target > origin:  # 向右：推到全部障碍物右缘之后（迭代防级联重叠）
            pos = target
            while not legal(pos):
                blockers = [
                    o
                    for o in others
                    if pos < o.timeline_end_ms
                    and o.timeline_start_ms < pos + duration
                ]
                if not blockers:
                    return origin
                pos = max(o.timeline_end_ms for o in blockers)
            return pos
        # 向左：拉到全部障碍物左缘之前
        pos = target
        while pos >= 0 and not legal(pos):
            blockers = [
                o
                for o in others
                if pos < o.timeline_end_ms and o.timeline_start_ms < pos + duration
            ]
            if not blockers:
                break
            pos = min(o.timeline_start_ms for o in blockers) - duration
        return pos if pos >= 0 and legal(pos) else origin
