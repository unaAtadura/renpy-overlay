"""剪辑命令流：分割/删除/合并/移动命令 + 撤销栈。

语义（future_*.txt）：「分割、删除、合并操作记录命令流，应用前可回退」；
ctrl+z 取消上一步（退回按钮同源）。

实现口径：命令 ``apply`` 前记录模型快照（:meth:`TimelineModel.snapshot`），
``revert`` 经 :meth:`TimelineModel.restore` 全量恢复——片段数量级小，
快照恢复换取实现简单与绝对一致（增量逆操作易漏边界场景）。命令对象
实现 :class:`~renpy_overlay.video.clip.editing.interfaces.EditCommand` 协议。
"""

from __future__ import annotations

from .interfaces import EditCommand
from .model import TimelineModel


class _ModelCommand:
    """模型命令基类：apply 前自动快照，revert 全量恢复。"""

    name = "command"

    def __init__(self, model: TimelineModel) -> None:
        self._model = model
        self._snapshot = model.snapshot()

    def apply(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def revert(self) -> None:
        self._model.restore(self._snapshot)


class SplitCommand(_ModelCommand):
    """分割：以进度标记（源时间）切割选中段。"""

    name = "split"

    def __init__(self, model: TimelineModel, clip_id: int, at_source_ms: int) -> None:
        super().__init__(model)
        self._clip_id = clip_id
        self._at = at_source_ms

    def apply(self) -> None:
        self._model.split(self._clip_id, self._at)


class DeleteCommand(_ModelCommand):
    """删除：移除选中段（音视频一体联动）并收紧。"""

    name = "delete"

    def __init__(self, model: TimelineModel, clip_id: int) -> None:
        super().__init__(model)
        self._clip_id = clip_id

    def apply(self) -> None:
        self._model.delete(self._clip_id)


class JoinCommand(_ModelCommand):
    """合并（c）：按当前顺序紧凑重排全部片段。"""

    name = "join"

    def __init__(self, model: TimelineModel) -> None:
        super().__init__(model)

    def apply(self) -> None:
        self._model.join_all()


class MoveCommand(_ModelCommand):
    """拖拽移动：段移动到目标序列位置（模型内防重叠钳制）。"""

    name = "move"

    def __init__(self, model: TimelineModel, clip_id: int, target_start_ms: int) -> None:
        super().__init__(model)
        self._clip_id = clip_id
        self._target = target_start_ms

    def apply(self) -> None:
        self._model.move(self._clip_id, self._target)


class RestoreCommand(_ModelCommand):
    """通用快照命令：操作已直接生效（如声波图拖拽实时预览），入栈仅
    为了可回退——apply 为空操作，revert 恢复快照。

    ``snapshot`` 供外部传入「拖拽开始前」的快照（拖拽场景下构造时机
    已是拖拽后，必须用开始前的快照才能回退）；缺省则取当前状态。"""

    name = "restore"

    def __init__(self, model: TimelineModel, snapshot=None) -> None:
        if snapshot is None:
            super().__init__(model)
        else:
            self._model = model
            self._snapshot = list(snapshot)

    def apply(self) -> None:
        pass  # 状态已生效（回退所需快照已在构造时捕获）


class UndoStack:
    """撤销栈：push(已 apply 的命令)/undo；「应用」成功后 clear。

    长按连发（ctrl+z 2 秒后 0.5 秒间隔连发）由 UI 层节拍驱动，逐次调
    :meth:`undo`；栈空时 no-op（不报错）。
    """

    def __init__(self) -> None:
        self._stack: list[EditCommand] = []

    def push(self, command: EditCommand) -> None:
        self._stack.append(command)

    def undo(self) -> EditCommand | None:
        """撤销最近一条命令；栈空返回 ``None``。"""
        if not self._stack:
            return None
        cmd = self._stack.pop()
        cmd.revert()
        return cmd

    def clear(self) -> None:
        self._stack.clear()

    def can_undo(self) -> bool:
        return bool(self._stack)

    def depth(self) -> int:
        return len(self._stack)
