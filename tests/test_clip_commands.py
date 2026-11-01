"""UndoStack 与命令单测：快照/回退/清空（离线）。"""

import pytest

from renpy_overlay.video.clip.editing.commands import (
    DeleteCommand,
    JoinCommand,
    MoveCommand,
    SplitCommand,
    UndoStack,
)
from renpy_overlay.video.clip.editing.model import TimelineModel


@pytest.fixture()
def env():
    model = TimelineModel()
    model.load_source("a.mp4", 10_000)
    stack = UndoStack()
    return model, stack


class TestSplitUndo:
    def test_split_and_undo(self, env):
        model, stack = env
        clip = model.clips()[0]
        cmd = SplitCommand(model, clip.clip_id, 5_000)
        cmd.apply()
        stack.push(cmd)
        assert len(model.clips()) == 2
        stack.undo()
        assert len(model.clips()) == 1
        assert model.clips()[0].source_end_ms == 10_000

    def test_undo_empty_stack_noop(self, env):
        model, stack = env
        assert stack.undo() is None

    def test_multiple_undo_lifo(self, env):
        model, stack = env
        first = model.clips()[0]
        c1 = SplitCommand(model, first.clip_id, 4_000)
        c1.apply()
        stack.push(c1)
        second = model.clips()[1]
        c2 = SplitCommand(model, second.clip_id, 7_000)
        c2.apply()
        stack.push(c2)
        assert len(model.clips()) == 3
        stack.undo()  # 撤销第二次分割
        assert [(c.source_start_ms, c.source_end_ms) for c in model.clips()] == [
            (0, 4_000),
            (4_000, 10_000),
        ]
        stack.undo()  # 撤销第一次分割
        assert len(model.clips()) == 1


class TestDeleteUndo:
    def test_delete_undo_restores_position(self, env):
        model, stack = env
        clip = model.clips()[0]
        model.split(clip.clip_id, 2_000)
        model.split(model.clips()[1].clip_id, 5_000)
        cmd = DeleteCommand(model, model.clips()[1].clip_id)
        cmd.apply()
        stack.push(cmd)
        assert model.total_duration_ms() == 7_000
        stack.undo()
        assert model.total_duration_ms() == 10_000
        assert [(c.source_start_ms, c.source_end_ms) for c in model.clips()] == [
            (0, 2_000),
            (2_000, 5_000),
            (5_000, 10_000),
        ]


class TestJoinUndo:
    def test_join_undo_restores_gap(self, env):
        model, stack = env
        clip = model.clips()[0]
        model.split(clip.clip_id, 3_000)
        model.split(model.clips()[1].clip_id, 6_000)
        last = model.clips()[2]  # 末段右移产生间隙
        model.move(last.clip_id, last.timeline_start_ms + 2_000)
        cmd = JoinCommand(model)
        cmd.apply()
        stack.push(cmd)
        assert not model.has_gaps()
        stack.undo()
        assert model.has_gaps()
        assert model.clips()[2].timeline_start_ms == 8_000


class TestMoveUndo:
    def test_move_undo(self, env):
        model, stack = env
        clip = model.clips()[0]
        model.split(clip.clip_id, 3_000)
        model.split(model.clips()[1].clip_id, 6_000)
        last = model.clips()[2]  # 末段右移
        cmd = MoveCommand(model, last.clip_id, last.timeline_start_ms + 1_500)
        cmd.apply()
        stack.push(cmd)
        assert model.has_gaps()
        stack.undo()
        assert model.clips()[2].timeline_start_ms == 6_000


class TestClear:
    def test_clear_drops_all(self, env):
        model, stack = env
        clip = model.clips()[0]
        cmd = SplitCommand(model, clip.clip_id, 5_000)
        cmd.apply()
        stack.push(cmd)
        stack.clear()
        assert stack.can_undo() is False
        assert stack.undo() is None
