"""剪辑功能扩展点：可插拔协议预留（本阶段只定义不实现）。

架构约定：未来剪辑功能（时间轴/声波图/切片/字幕，见 ``.raw_plans/
future_*.txt``）与 UI 层只依赖本模块定义的协议，不直接耦合具体实现，
保证播放引擎可独立演进、剪辑能力可插拔：

- :class:`PlaybackControl`：播放控制抽象（:class:`~renpy_overlay.video.clip.player.engine.
  VideoPlayer` 门面天然满足该协议，open/setPosition/play/pause/音量/状态）；
- :class:`MediaProbe`：媒体信息读取抽象（:func:`~renpy_overlay.video.clip.av_media.
  probe_media_info` 已是实现）；
- :class:`FrameLocator`：帧 ↔ 毫秒换算抽象（按媒体帧率；player_window 的
  ms_to_frame / frame_to_ms 纯函数为默认实现口径）；
- :class:`ClipSegment` / :class:`ClipMarker`：切片区间模型与标记管理协议
  （时间轴/声波图的数据源；「切片起点终点蒙版」「切割/合并/删除」均经
  此抽象操作，不触碰引擎）；
- :class:`EditCommand`：剪辑命令抽象（分割/删除/合并/撤销的统一接口，
  为 ctrl+z 撤销链预留）。

实现约定：协议均为 ``typing.Protocol``（结构化子类型，实现方无需继承），
运行时零依赖；首个具体实现落地时补充离线单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlaybackControl(Protocol):
    """播放控制协议（QMediaPlayer 风格最小面；VideoPlayer 门面满足）。"""

    def open(self, path: str) -> None:
        """请求打开媒体（异步；完成经状态回调回报，落定为暂停态）。"""
        ...

    def setPosition(self, position_ms: int) -> None:
        """异步 seek（最新胜出；完成帧经信箱/队列上屏）。"""
        ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def playbackState(self) -> int:
        """当前播放状态（PlaybackState 枚举值）。"""
        ...

    def position(self) -> int:
        """当前播放位置（毫秒）。"""
        ...

    def duration(self) -> int:
        """媒体总时长（毫秒；未打开为 -1）。"""
        ...

    def hasMedia(self) -> bool:
        """是否已持有可用媒体（进度条/时间轴就绪判定）。"""
        ...

    def setVolume(self, volume: float) -> None:
        """音量（0.0-1.0 线性）。"""
        ...


@runtime_checkable
class MediaProbe(Protocol):
    """媒体信息读取协议（无引擎轻量探测；av_media.probe_media_info 已实现）。"""

    def __call__(self, path: str) -> object | None:
        """返回 MediaInfo 或 None（失败不抛异常）。"""
        ...


class FrameLocator(Protocol):
    """帧定位换算协议：时间轴缩放/帧步进/切片换算共用（按媒体帧率）。"""

    def frame_duration_ms(self) -> int:
        """单帧时长（毫秒）。"""
        ...

    def ms_to_frame(self, position_ms: int) -> int:
        """播放位置（毫秒）→ 帧号。"""
        ...

    def frame_to_ms(self, frame: int) -> int:
        """帧号 → 播放位置（毫秒，帧首）；负帧钳零。"""
        ...

    def max_frame(self) -> int:
        """当前媒体最大帧号（未打开媒体为 -1）。"""
        ...


@dataclass
class ClipSegment:
    """切片区间模型（毫秒，左闭右开语义由 ClipMarker 约定）。

    future_*.txt 逻辑预留：新切片允许与旧切片相接、不允许重叠；新切片
    起点或终点落入旧切片走清除逻辑，覆盖旧切片范围则不标记终点。
    """

    start_ms: int
    end_ms: int
    #: 扩展位：来源标记（人工切片 f 键 / ASR / 翻译字幕对齐），剪辑阶段启用
    origin: str = "manual"

    def contains(self, position_ms: int) -> bool:
        """位置是否在切片范围内（不含端点，future_*.txt 清除逻辑口径）。"""
        return self.start_ms < position_ms < self.end_ms

    def overlaps(self, other: ClipSegment) -> bool:
        """与另一切片是否重叠（相接不算：end == start 允许）。"""
        return self.start_ms < other.end_ms and other.start_ms < self.end_ms


@runtime_checkable
class ClipMarker(Protocol):
    """切片标记管理协议（时间轴/声波图蒙版的数据源）。"""

    def mark(self, position_ms: int) -> ClipSegment | None:
        """以进度标记为准切换标记起点/终点（future_*.txt 的 f 键状态机）。"""
        ...

    def segments(self) -> list[ClipSegment]:
        """当前全部切片（按起点排序）。"""
        ...

    def clear_at(self, position_ms: int) -> bool:
        """清除覆盖该位置的切片；无命中返回 False。"""
        ...


@runtime_checkable
class EditCommand(Protocol):
    """剪辑命令抽象（分割/删除/合并/退回；为 ctrl+z 撤销链预留）。"""

    name: str

    def apply(self) -> None:
        """执行命令（时间轴模型变更在此，不触碰播放引擎）。"""
        ...

    def revert(self) -> None:
        """撤销命令（apply 的严格逆操作）。"""
        ...


#: 剪辑扩展点装配建议（文档性说明，非运行时约束）：
#: - 时间轴/声波图控件读 ClipMarker.segments() 渲染蒙版，经 FrameLocator
#:   做「时间 ↔ 像素」映射（future_*.txt：时间轴与声波图保持时间对应、可缩放）；
#: - 快捷按钮（分割/删除/合并/应用）各自实现 EditCommand，经统一命令栈
#:   执行以获得 ctrl+z 撤销；
#: - PlaybackControl 由 PlayerWindow 持有的 VideoPlayer 直接充当，剪辑层
#:   不感知 SDL/PyAV 细节。
__all__ = [
    "ClipMarker",
    "ClipSegment",
    "EditCommand",
    "FrameLocator",
    "MediaProbe",
    "PlaybackControl",
]
