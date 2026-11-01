"""TimelineModel 单测：分割/删除/移动/合并/非线性映射（离线，无 QApplication）。"""

import pytest

from renpy_overlay.video.clip.editing.model import TimelineModel


@pytest.fixture()
def model():
    m = TimelineModel()
    m.load_source("a.mp4", 10_000)  # 单段 [0, 10s)
    return m


class TestLoadAndSplit:
    def test_load_creates_single_clip(self, model):
        clips = model.clips()
        assert len(clips) == 1
        assert clips[0].source_end_ms == 10_000
        assert model.selected_id() == clips[0].clip_id

    def test_split_into_two(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 4_000)
        clips = model.clips()
        assert len(clips) == 2
        assert (clips[0].source_start_ms, clips[0].source_end_ms) == (0, 4_000)
        assert (clips[1].source_start_ms, clips[1].source_end_ms) == (4_000, 10_000)
        # 分割不改变总时长，两段紧凑衔接
        assert clips[1].timeline_start_ms == clips[0].timeline_end_ms
        assert model.total_duration_ms() == 10_000

    def test_split_boundary_protected(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 0)  # 起始帧
        model.split(clip.clip_id, 10_000)  # 末尾帧
        model.split(clip.clip_id, -5)  # 越界
        model.split(999, 4_000)  # 不存在的段
        assert len(model.clips()) == 1

    def test_split_middle_of_second(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 4_000)
        second = model.clips()[1]
        model.split(second.clip_id, 7_000)
        clips = model.clips()
        assert [(c.source_start_ms, c.source_end_ms) for c in clips] == [
            (0, 4_000),
            (4_000, 7_000),
            (7_000, 10_000),
        ]
        assert [c.timeline_start_ms for c in clips] == [0, 4_000, 7_000]


class TestDelete:
    def test_delete_middle_compacts(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 2_000)
        model.split(model.clips()[1].clip_id, 5_000)
        # 三段：[0,2k) [2k,5k) [5k,10k)
        middle = model.clips()[1]
        model.delete(middle.clip_id)
        clips = model.clips()
        assert len(clips) == 2
        # 收紧：后段前移 3s
        assert clips[1].timeline_start_ms == 2_000
        assert model.total_duration_ms() == 7_000

    def test_delete_clears_selection(self, model):
        clip = model.clips()[0]
        model.delete(clip.clip_id)
        assert model.selected_id() is None
        assert model.clips() == []


class TestMoveAndJoin:
    def _three_clips(self, model):
        first = model.clips()[0]
        model.split(first.clip_id, 3_000)
        model.split(model.clips()[1].clip_id, 6_000)
        return model.clips()  # [0,3k) [3k,6k) [6k,10k) timeline 0/3k/6k

    def test_move_compact_sequence_noop(self, model):
        self._three_clips(model)
        # 已紧凑：移到任何相邻无缝位置都被钳制为无变化或等价位置
        assert not model.move(model.clips()[1].clip_id, 3_000)
        assert not model.has_gaps()

    def test_join_after_manual_gap(self, model):
        clips = self._three_clips(model)
        # 直接构造间隙场景（模拟拖拽结果）：末段右移 2s
        model.move(clips[2].clip_id, clips[2].timeline_start_ms + 2_000)
        assert model.has_gaps()
        assert model.total_duration_ms() == 12_000
        assert model.join_all() is True
        assert not model.has_gaps()
        assert model.total_duration_ms() == 10_000  # 合并不改变总时长
        assert [c.timeline_start_ms for c in model.clips()] == [0, 3_000, 6_000]

    def test_join_no_gaps_is_noop(self, model):
        self._three_clips(model)
        assert model.join_all() is False


class TestTimeMapping:
    def test_timeline_to_source_compact(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 4_000)
        model.delete(model.clips()[0].clip_id)  # 删除 [0,4k)：剩 [4k,10k) 收紧到 0
        assert model.total_duration_ms() == 6_000
        assert model.timeline_to_source(0) == 4_000
        assert model.timeline_to_source(2_000) == 6_000
        assert model.timeline_to_source(6_000) is None  # 越界
        assert model.timeline_to_source(-1) is None

    def test_source_to_timeline(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 4_000)
        model.delete(model.clips()[0].clip_id)
        assert model.source_to_timeline(4_000) == 0
        assert model.source_to_timeline(9_999) == 5_999
        assert model.source_to_timeline(3_999) is None  # 已删除区间
        assert model.source_to_timeline(10_000) is None

    def test_mapping_roundtrip_after_delete(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 5_000)
        model.delete(model.clips()[0].clip_id)
        for t in (0, 1, 2_500, 4_999):
            src = model.timeline_to_source(t)
            assert model.source_to_timeline(src) == t


class TestSnapshotRestore:
    def test_restore_reverts_split(self, model):
        before = model.snapshot()
        clip = model.clips()[0]
        model.split(clip.clip_id, 5_000)
        assert len(model.clips()) == 2
        model.restore(before)
        assert len(model.clips()) == 1

    def test_restore_dangling_selection_cleared(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 5_000)
        model.set_selected(model.clips()[1].clip_id)
        model.restore(model.snapshot()[:1])  # 只恢复第一段
        assert model.selected_id() is None


class TestMoveClamp:
    def test_move_clamped_to_avoid_overlap(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 3_000)
        model.split(model.clips()[1].clip_id, 6_000)
        clips = model.clips()  # timeline 0/3k/6k
        # 第二段（[3k,6k)，时长 3s）向左移动到 0 会与第一段重叠 → 钳制
        moved = model.move(clips[1].clip_id, 0)
        # 逐位置探测：0..2999 均重叠，恰好钳到 3000（原位）→ 无变化
        assert moved is False
        assert model.clips()[1].timeline_start_ms == 3_000

    def test_move_right_creates_gap(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 3_000)
        model.split(model.clips()[1].clip_id, 6_000)
        last = model.clips()[2]  # [6k,10k)：末段右移不与任何段重叠
        assert model.move(last.clip_id, 8_000) is True
        assert model.has_gaps()
        assert model.clips()[2].timeline_start_ms == 8_000

    def test_move_middle_pushed_past_blocker(self, model):
        clip = model.clips()[0]
        model.split(clip.clip_id, 3_000)
        model.split(model.clips()[1].clip_id, 6_000)
        middle = model.clips()[1]  # [3k,6k)：右移到 4k 与末段重叠 → 推挤到末段右缘
        assert model.move(middle.clip_id, 4_000) is True
        # 排序后序列位置集合：中段被推到末段右缘 10000
        assert sorted(c.timeline_start_ms for c in model.clips()) == [0, 6_000, 10_000]
        assert not any(
            a.timeline_start_ms < b.timeline_end_ms
            and b.timeline_start_ms < a.timeline_end_ms
            for a in model.clips()
            for b in model.clips()
            if a is not b
        )  # 核心不变量：无重叠
