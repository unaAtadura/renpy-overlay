"""ClipMarkerModel 单测：f 键切片状态机全规则矩阵（离线）。"""

import pytest

from renpy_overlay.video.clip.editing.clip_marker import ClipMarkerModel


@pytest.fixture()
def marker():
    return ClipMarkerModel()


def segs(marker):
    return [(s.start_ms, s.end_ms) for s in marker.segments()]


class TestMarkBasics:
    def test_mark_start_then_end(self, marker):
        marker.mark(1_000)
        assert marker.pending_start() == 1_000
        assert marker.segments() == []
        marker.mark(3_000)
        assert marker.pending_start() is None
        assert segs(marker) == [(1_000, 3_000)]

    def test_reverse_direction_normalizes(self, marker):
        marker.mark(5_000)
        marker.mark(2_000)  # 终点早于起点：整理为 [2k, 5k)
        assert segs(marker) == [(2_000, 5_000)]

    def test_zero_length_ignored(self, marker):
        marker.mark(1_000)
        marker.mark(1_000)
        assert marker.segments() == []
        assert marker.pending_start() == 1_000  # 起点保持挂起


class TestClearRules:
    def test_f_inside_segment_clears_it(self, marker):
        marker.mark(1_000)
        marker.mark(3_000)
        marker.mark(2_000)  # 范围内（不含端点）→ 清除
        assert marker.segments() == []

    def test_f_on_endpoint_does_not_clear(self, marker):
        marker.mark(1_000)
        marker.mark(3_000)
        marker.mark(1_000)  # 端点：不清除 → 走标记起点（挂起）
        assert segs(marker) == [(1_000, 3_000)]
        assert marker.pending_start() == 1_000

    def test_sequential_adjacent_allowed(self, marker):
        marker.mark(1_000)
        marker.mark(3_000)  # [1k,3k)
        marker.mark(3_000)  # 相接端点 → 新切片起点
        marker.mark(5_000)  # [3k,5k)
        assert segs(marker) == [(1_000, 3_000), (3_000, 5_000)]

    def test_overlapping_end_clears_old_then_new_on_next_press(self, marker):
        marker.mark(1_000)
        marker.mark(4_000)  # 旧切片 [1k,4k)
        marker.mark(6_000)  # 新起点 6k（切片外）
        marker.mark(3_000)  # 终点 3k 落入旧切片 → 本次只清除旧切片
        assert segs(marker) == []
        assert marker.pending_start() == 6_000  # 起点保持挂起
        marker.mark(3_000)  # 再按一次 f → 生成新切片 [3k,6k)
        assert segs(marker) == [(3_000, 6_000)]
        assert marker.pending_start() is None


class TestCoverRule:
    def test_covered_old_does_not_finish_new(self, marker):
        marker.mark(2_000)
        marker.mark(4_000)  # 旧切片 [2k,4k)
        marker.mark(1_000)  # 新起点 1k
        marker.mark(5_000)  # 新切片 [1k,5k) 覆盖旧切片 → 清除旧切片但不标终点
        assert marker.segments() == []  # 旧切片已清除
        assert marker.pending_start() == 1_000  # 终点未标记：起点保持挂起
        # 再次按 f 完成切片
        marker.mark(6_000)
        assert segs(marker) == [(1_000, 6_000)]


class TestCrossSegmentStepwise:
    """跨越已有切片的第二次标记：仅清除不成段（分步时序）。"""

    def test_second_mark_inside_only_clears(self, marker):
        marker.mark(1_000)
        marker.mark(3_000)  # 已有切片 A-B = [1k,3k)
        marker.mark(6_000)  # C = 6k（A-B 之外）→ 标记起点
        marker.mark(2_000)  # D = 2k（A-B 之内）→ 仅清除 A-B，不生成 C-D
        assert segs(marker) == []  # A-B 已清除且无 C-D
        assert marker.pending_start() == 6_000  # 待定起点 C 保留
        marker.mark(2_000)  # 再按一次 f → 以 D 为终点生成 C-D
        assert segs(marker) == [(2_000, 6_000)]
        assert marker.pending_start() is None

    def test_pending_start_before_segment_kept(self, marker):
        marker.mark(5_000)
        marker.mark(7_000)  # A-B = [5k,7k)
        marker.mark(1_000)  # C = 1k（A-B 左侧之外）
        marker.mark(6_000)  # D = 6k（A-B 之内）→ 仅清除 A-B
        assert segs(marker) == []
        assert marker.pending_start() == 1_000
        marker.mark(6_000)
        assert segs(marker) == [(1_000, 6_000)]

    def test_both_marks_outside_finishes_in_two_presses(self, marker):
        marker.mark(1_000)
        marker.mark(3_000)  # A-B = [1k,3k)
        marker.mark(5_000)  # C = 5k（A-B 之外）
        marker.mark(7_000)  # D = 7k（A-B 之外且不相交）→ 两次直接成段
        assert segs(marker) == [(1_000, 3_000), (5_000, 7_000)]
        assert marker.pending_start() is None

    def test_changed_emitted_once_per_step(self, marker):
        signals: list[int] = []
        marker.changed.connect(lambda: signals.append(1))
        marker.mark(1_000)
        marker.mark(3_000)  # A-B 就绪
        marker.mark(5_000)  # 标记起点 C
        marker.mark(2_000)  # 中间状态：仅清除 A-B → 一次刷新
        assert len(signals) == 4
        marker.mark(2_000)  # 最终状态：生成 C-D → 又一次刷新
        assert len(signals) == 5

    def test_cancel_pending_in_intermediate_state(self, marker):
        marker.mark(1_000)
        marker.mark(3_000)
        marker.mark(5_000)
        marker.mark(2_000)  # 中间状态：A-B 已清除，C 仍挂起
        marker.cancel_pending()
        assert segs(marker) == []  # A-B 不回滚（清除已生效）
        assert marker.pending_start() is None
        marker.mark(8_000)  # 之后仍可正常重新标记
        assert marker.pending_start() == 8_000


class TestReplaceAndClear:
    def test_replace_all_sorts_and_drops_pending(self, marker):
        from renpy_overlay.video.clip.editing.interfaces import ClipSegment

        marker.mark(9_000)  # 制造挂起
        marker.replace_all(
            [ClipSegment(5_000, 6_000), ClipSegment(1_000, 2_000)]
        )
        assert segs(marker) == [(1_000, 2_000), (5_000, 6_000)]
        assert marker.pending_start() is None

    def test_clear_resets_everything(self, marker):
        marker.mark(1_000)
        marker.clear()
        assert marker.segments() == []
        assert marker.pending_start() is None


class TestNegativeClamp:
    def test_negative_position_clamped(self, marker):
        marker.mark(-5)
        assert marker.pending_start() == 0
