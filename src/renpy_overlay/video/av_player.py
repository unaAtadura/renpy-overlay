"""基于 PyAV 的自管视频播放引擎：替代 QMediaPlayer 解码管线（选型背景）。

QMediaPlayer 的 FFmpeg 后端在连续 seek 下存在两类实测缺陷（logs/
renpy_overlay_20261013_071224.log、renpy_overlay_20261013_072050.log）：

1. 抢占态产出中间态坏帧并以 ~254 帧/秒异常发帧（上屏即花屏）；
2. 抢占态 QVideoFrame.toImage 单帧从 ~5ms 劣化至 100-420ms，慢帧风暴
   独占主线程数十秒（072050 日志 41s 心跳真空的卡死实锤）。

本引擎把解码全部移入工作线程：av.seek 关键帧定位 + 逐帧解码到目标
（确定性、无抢占竞争），中间帧在 worker 内丢弃、仅投递完成帧；帧在
worker 内转 QImage（成品拷贝）后经信号投递主线程——主线程零解码工作，
花屏与卡死从机制上消除。音频仅用 Qt Multimedia 的 QAudioSink 做输出
（成熟稳定的音频通路），解码仍由 worker 完成。

线程模型：container/stream 全部操作限定在 worker 线程（PyAV 对象非线程
安全）；主线程只写请求字段（open/seek/play/pause，最新胜出）并消费信号。
音画同步为同一 wall 时钟基准各自实时推进（视频 worker 按 pts 节拍送帧、
音频按解码顺序写入 sink），长片段可能有百毫秒级偏差——编辑定位场景
（暂停态步进/跳帧/捕获为绝对主）可接受，注释明示不做主动校正。

离线测试约定：真实 av 解码/QAudioSink 链路人工联测；元数据解析/时钟/
状态机等纯逻辑经注入（open_fn/clock/音频工厂）直接单测。
"""

from __future__ import annotations

import ctypes
import enum
import logging
import threading
from collections import deque
from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

logger = logging.getLogger("renpy_overlay.video.av_player")

#: 帧率回退值：流元数据缺失/非法时的取值（与 editor_window 同口径）
DEFAULT_FPS = 30.0
#: 音频输出标准参数：worker 重采样目标（16bit 交错双声道 48kHz）
AUDIO_RATE = 48_000
#: 预解码帧队列深度：播放态 worker 领先主线程消费的帧数上限（内存兜底）
FRAME_QUEUE_DEPTH = 4

_PLAY = "play"
_PAUSE = "pause"


class PlaybackState(enum.IntEnum):
    """播放状态（语义对齐 QMediaPlayer.PlaybackState 的比较用法）。"""

    Stopped = 0
    Playing = 1
    Paused = 2


@dataclass
class MediaInfo:
    """媒体元数据（worker 打开后经 media_opened 信号投递主线程）。"""

    fps: float
    duration_ms: int
    has_audio: bool


def stream_fps(video_stream) -> float:
    """视频流帧率：average_rate 优先、guessed_rate 兜底、非法回退 30fps。

    PyAV 的 rate 为 Fraction（或 None）：float 化失败与非正值一律回退。
    """
    for attr in ("average_rate", "guessed_rate"):
        rate = getattr(video_stream, attr, None)
        if rate is None:
            continue
        try:
            value = float(rate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_FPS


def stream_duration_ms(video_stream) -> int:
    """视频流时长（毫秒）：duration × time_base 换算；未知 ≤0 返回 -1。"""
    duration = getattr(video_stream, "duration", None)
    time_base = getattr(video_stream, "time_base", None)
    if duration is None or time_base is None or duration <= 0:
        return -1
    return int(duration * float(time_base) * 1000)


def container_duration_ms(container_duration_us) -> int:
    """容器时长（毫秒）：PyAV container.duration 为微秒；未知 ≤0 返回 -1。"""
    if container_duration_us is None or container_duration_us <= 0:
        return -1
    return int(container_duration_us / 1000)


def media_duration_ms(video_stream, container_duration_us) -> int:
    """媒体时长（毫秒）：流时基优先、容器兜底；两者未知返回 -1（无有效范围）。"""
    duration = stream_duration_ms(video_stream)
    if duration >= 0:
        return duration
    return container_duration_ms(container_duration_us)


def target_playback_pts(start_pts_ms: int, start_wall_ms: float, now_wall_ms: float) -> int:
    """播放时钟推算：起播基准 + 已流逝 wall 时间（帧节拍的目标 pts）。"""
    return int(start_pts_ms + max(0.0, now_wall_ms - start_wall_ms))


def _plane_bytes(plane) -> bytes:
    """解码平面缓冲 → bytes（av 19 的 Plane 无 to_bytes，经指针拷贝）。"""
    return ctypes.string_at(plane.buffer_ptr, plane.buffer_size)


def av_frame_to_qimage(av_frame) -> QImage:
    """av 视频帧 → QImage（rgb24 深拷贝；在 worker 线程执行）。

    buffer_size 含逐行对齐填充，QImage 以 line_size 为步长解读即可；
    copy() 脱离临时缓冲，信号跨线程投递后主线程使用安全。
    """
    rgb = av_frame.reformat(format="rgb24")
    plane = rgb.planes[0]
    data = _plane_bytes(plane)
    image = QImage(data, plane.width, plane.height, plane.line_size, QImage.Format.Format_RGB888)
    return image.copy()


def frame_time_ms(av_frame) -> int:
    """帧时间戳（毫秒）：frame.time（秒，float）换算并钳零。"""
    pts = getattr(av_frame, "time", None)
    if pts is None:
        return 0
    return max(0, int(pts * 1000))


class AvVideoPlayer(QObject):
    """PyAV 自管播放器（主线程门面 + 单 worker 解码线程）。

    门面方法名沿用 QMediaPlayer 风格（position/setPosition/play/pause/
    playbackState），最小化 editor_window 与既有测试的改动。
    """

    frame_ready = pyqtSignal(QImage)
    position_changed = pyqtSignal(int)
    state_changed = pyqtSignal(object)
    media_opened = pyqtSignal(object)
    open_failed = pyqtSignal(str)
    audio_chunk = pyqtSignal(bytes)
    _audio_format_ready = pyqtSignal(int, int)  # worker→主线程建 sink（勿用跨线程 QTimer）

    def __init__(
        self,
        parent=None,
        open_fn=None,
        clock_ms=None,
        start_worker: bool = True,
        resampler_factory=None,
    ) -> None:
        super().__init__(parent)
        self._open_fn = open_fn  # av.open 注入点（离线测试；None 用真实实现）
        self._clock_ms = clock_ms or _default_clock_ms
        self._resampler_factory = resampler_factory  # 音频重采样器工厂（测试注入）
        #: 主线程 → worker 请求字段（简单赋值，GIL 下读写安全；最新胜出）
        self._open_request: str | None = None
        self._seek_request_ms: int | None = None
        self._command: str | None = None
        self._stop_flag = False
        #: 主线程可读状态（worker 完成动作后更新并发信号）
        self._state = PlaybackState.Stopped
        self._position_ms = 0
        self._duration_ms = -1
        self._volume = 0.0
        self._audio_output = None  # QAudioSink 延迟创建（首次拿到音频参数时）
        self._sink_io = None  # push 模式 QIODevice（sink.start() 返回，写 PCM 用）
        self._sink_state = "stopped"  # 引擎侧跟踪：stopped/started/suspended
        self._cond = threading.Condition()
        self.audio_chunk.connect(self._write_audio)  # worker 音频流送主线程 sink
        self._audio_format_ready.connect(self._apply_audio_format)  # 跨线程建 sink
        self._worker = threading.Thread(
            target=self._worker_loop, name="av-player-worker", daemon=True
        )
        if start_worker:  # 离线测试传 False：请求字段不被 worker 消费，可断言
            self._worker.start()

    # ---- 主线程 API（门面） ------------------------------------------------

    def hasMedia(self) -> bool:
        return self._duration_ms >= 0

    def playbackState(self) -> PlaybackState:
        return self._state

    def position(self) -> int:
        return self._position_ms

    def duration(self) -> int:
        return self._duration_ms

    def open(self, path: str) -> None:
        """请求打开媒体（worker 完成：暂停态 + 首帧投递）。"""
        with self._cond:
            self._open_request = path
            self._seek_request_ms = None
            self._command = None
            self._cond.notify_all()

    def setPosition(self, position_ms: int) -> None:
        """请求 seek（最新胜出：连点只保留最终目标，worker 逐个消费）。"""
        with self._cond:
            self._seek_request_ms = max(0, int(position_ms))
            self._cond.notify_all()
        # 丢弃旧音频缓冲；播放中 seek 后重启 sink 继续消费新流
        if self._audio_output is not None:
            self._control_sink("reset")
            if self._state == PlaybackState.Playing:
                self._control_sink("start")

    def play(self) -> None:
        self._set_state(PlaybackState.Playing)
        self._control_sink("start")
        with self._cond:
            self._command = _PLAY
            self._cond.notify_all()

    def pause(self) -> None:
        self._set_state(PlaybackState.Paused)
        self._control_sink("suspend")
        with self._cond:
            self._command = _PAUSE
            self._cond.notify_all()

    def setVolume(self, volume: float) -> None:
        """音量 0..1（主线程直接作用于 QAudioSink，worker 不感知）。"""
        self._volume = max(0.0, min(1.0, float(volume)))
        if self._audio_output is not None:
            self._audio_output.setVolume(self._volume)

    def close(self) -> None:
        """停止 worker（进程退出/窗口销毁；daemon 线程兜底不阻塞退出）。"""
        self._stop_flag = True
        with self._cond:
            self._cond.notify_all()

    # ---- 状态与信号（主线程） ------------------------------------------------

    def _set_state(self, state: PlaybackState) -> None:
        if self._state != state:
            logger.debug("播放状态：%s → %s", self._state.name, state.name)
            self._state = state
            self.state_changed.emit(state)

    def _apply_audio_format(self, rate: int, channels: int) -> None:
        """worker 解析出音频参数后回调：创建/复建 QAudioSink（主线程）。"""
        from PyQt6.QtMultimedia import QAudioFormat, QAudioSink

        fmt = QAudioFormat()
        fmt.setSampleRate(rate)
        fmt.setChannelCount(channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if self._audio_output is not None:
            self._audio_output.stop()
        self._audio_output = QAudioSink(fmt, self)
        self._audio_output.setVolume(self._volume)
        self._sink_io = None
        self._sink_state = "stopped"  # 新 sink 未 start（play 时启动）
        logger.info(
            "音频输出已就绪：sink=%dHz/%dch/s16（音量 %.1f）", rate, channels, self._volume
        )

    def _write_audio(self, chunk: bytes) -> None:
        """音频 PCM 写入 sink（audio_chunk 信号槽，主线程；仅播放态消费）。

        PyQt6 6.11 的 QAudioSink 未绑定 write：流式写入走 push 模式——
        start() 返回的 QIODevice（见 _control_sink）承担写入。
        """
        if self._sink_io is not None and self._state == PlaybackState.Playing:
            self._sink_io.write(chunk)

    def _control_sink(self, action: str) -> None:
        """QAudioSink 状态管理（主线程）：start 起播/resume、suspend 暂停。

        以引擎侧三态跟踪（started/suspended/stopped）代替读 sink.state()
        （避免 QAudio 枚举耦合且离线 stub 可测）：suspended 恢复走 resume、
        stopped 起播走 start（返回 push 模式 QIODevice 供写入）；reset
        丢弃缓冲并回到 stopped。
        """
        sink = self._audio_output
        if sink is None:
            return
        if action == "start":
            if self._sink_state == "stopped":
                self._sink_io = sink.start()
            else:  # suspended（或已 started 的冗余请求）：恢复播放
                sink.resume()
            self._sink_state = "started"
        elif action == "suspend":
            sink.suspend()
            self._sink_state = "suspended"
        elif action == "reset":
            sink.reset()
            self._sink_io = None
            self._sink_state = "stopped"

    # ---- worker 线程（全部 av 操作限定于此） ---------------------------------

    def _worker_loop(self) -> None:
        """解码主循环：请求优先级 stop > open > seek > 命令/播放推进。"""
        context = _DecodeContext(self)
        try:
            while not self._stop_flag:
                with self._cond:
                    if (
                        self._open_request is None
                        and self._seek_request_ms is None
                        and self._command is None
                        and (self._state != PlaybackState.Playing)
                    ):
                        self._cond.wait(timeout=0.1)
                if self._stop_flag:
                    break
                if self._open_request is not None:
                    path, self._open_request = self._open_request, None
                    context = self._do_open(path, context)
                    continue
                if self._seek_request_ms is not None:
                    target, self._seek_request_ms = self._seek_request_ms, None
                    context = self._do_seek(target, context)
                    continue
                if self._command is not None:
                    command, self._command = self._command, None
                    context.apply_command(command)
                    continue
                if self._state == PlaybackState.Playing:
                    context = context.play_next_frame()
        except Exception:  # pragma: no cover - worker 兜底隔离，不拖垮进程
            logger.warning("解码线程异常退出", exc_info=True)

    def _do_open(self, path: str, context: _DecodeContext) -> _DecodeContext:
        """打开媒体（worker）：元数据 → 暂停态 → 首帧投递。"""
        open_fn = self._open_fn or _default_open
        try:
            old = context
            container = open_fn(path)
            video_stream = container.streams.video[0]
            audio_streams = container.streams.audio
            fps = stream_fps(video_stream)
            duration = media_duration_ms(video_stream, container.duration)
            has_audio = bool(audio_streams)
        except Exception as exc:
            logger.warning("打开媒体失败：%s（%s）", path, exc)
            self.open_failed.emit(str(exc))
            return context
        old.release()
        info = MediaInfo(fps=fps, duration_ms=duration, has_audio=has_audio)
        self._duration_ms = duration
        self._position_ms = 0
        self._set_state(PlaybackState.Paused)
        logger.info(
            "引擎已打开（暂停）：%s（fps=%s duration=%dms audio=%s）",
            path,
            fps,
            duration,
            has_audio,
        )
        self.media_opened.emit(info)
        if has_audio:
            # 音频参数回调（信号队列投递主线程建 sink）
            self._apply_audio_format_later(audio_streams[0])
        new_context = _DecodeContext(self)
        new_context.attach(container, video_stream, audio_streams[0] if has_audio else None)
        new_context.emit_first_frame()
        return new_context

    def _apply_audio_format_later(self, audio_stream) -> None:
        """音频参数经信号投递主线程建 sink（QAudioSink 线程亲和主线程）。

        勿用 QTimer.singleShot：其必须在有事件循环的线程调用，从 worker
        调用静默失效（实测 sink 永不创建、播放无声）；跨线程一律走信号。
        """
        rate = getattr(audio_stream, "rate", None)
        channels = len(audio_stream.layout.channels) if audio_stream.layout else 2
        try:
            rate = int(rate) if rate else AUDIO_RATE
        except (TypeError, ValueError):
            rate = AUDIO_RATE
        self._audio_format_ready.emit(rate, channels or 2)

    def _do_seek(self, target_ms: int, context: _DecodeContext) -> _DecodeContext:
        """seek（worker）：关键帧定位 + 逐帧解码到目标，仅投递完成帧。"""
        if context.container is None:
            self._position_ms = target_ms
            self.position_changed.emit(target_ms)
            return context
        try:
            # 容器级 seek（微秒）：视频/音频 demux 位置同步更新，backward=True
            # 落在目标之前的关键帧，顺序解码到目标即帧精确（中间帧不投递）。
            context.container.seek(int(target_ms) * 1000, backward=True, any_frame=False)
            context.reset_streaming()
            result = context.decode_until(target_ms)
            if self._state == PlaybackState.Playing:
                # 播放中 seek：重置节拍基准为新位置，否则 worker 以 delay=0
                # 追赶旧基准与新位置间的差距（实测拖拽后 ~199fps 快放）
                context.reset_play_clock()
                logger.debug("seek 完成（播放中）：节拍基准重置 → %dms", self._position_ms)
            return result
        except Exception as exc:  # pragma: no cover - 后端差异容错
            logger.warning("seek 失败（target=%dms）：%s", target_ms, exc)
            return context


def _default_open(path: str):
    import av

    return av.open(path)


def _default_clock_ms() -> float:
    import time

    return time.monotonic() * 1000.0


def _default_resampler():
    import av

    return av.AudioResampler(format="s16", layout="stereo", rate=AUDIO_RATE)


class _DecodeContext:
    """worker 侧解码上下文：单遍 demux 分流解码 + 播放节拍（仅 worker 访问）。

    av 的 decode(stream) 生成器共享容器同一包流：视频/音频各自开迭代器会
    互相吞包（实测 seek 后 play 仅推进两帧即 EOF、音频被单次全量预解码
    1.8 万 chunk）。因此视频与音频共用单个 demux 流，按包归属分流解码：
    视频帧入缓冲供节拍消费，音频帧重采样后经信号流送主线程 sink。
    """

    def __init__(self, player: AvVideoPlayer) -> None:
        self._player = player
        self._resampler_factory = player._resampler_factory
        self.container = None
        self.video_stream = None
        self.audio_stream = None
        self._packets = None
        self._frame_buf: deque = deque()
        self._resampler = None
        self._play_start_pts = 0
        self._play_start_wall = 0.0

    def attach(self, container, video_stream, audio_stream) -> None:
        self.container = container
        self.video_stream = video_stream
        self.audio_stream = audio_stream
        self.reset_streaming()

    def release(self) -> None:
        """弃用旧容器（换视频）：引用置空交由 GC；worker 内执行安全。"""
        self.container = None
        self.video_stream = None
        self.audio_stream = None
        self._packets = None
        self._frame_buf.clear()
        self._resampler = None

    def reset_streaming(self) -> None:
        """seek/open 后重建 demux 流（容器位置已更新）。"""
        self._frame_buf.clear()
        if self.container is None:
            self._packets = None
            return
        streams = [s for s in (self.video_stream, self.audio_stream) if s is not None]
        self._packets = self.container.demux(*streams)
        self._resampler = None  # 惰性创建（首次音频帧时）

    def _decode_packet(self, packet) -> None:
        """按包归属分流解码：视频帧入缓冲，音频帧重采样后流送 sink。"""
        if packet.stream is self.video_stream:
            self._frame_buf.extend(packet.decode())
            return
        if self.audio_stream is None:
            return
        if self._resampler is None:
            self._resampler = (
                self._resampler_factory()
                if self._resampler_factory is not None
                else _default_resampler()
            )
        for frame in packet.decode():
            for chunk in self._resampler.resample(frame):
                if chunk.planes:
                    self._player.audio_chunk.emit(_plane_bytes(chunk.planes[0]))

    def _next_video_frame(self, emit_audio: bool):
        """单遍 demux 取下一视频帧（None=EOF）；沿途音频仅播放态流送——

        seek/打开的快进过程中旧时序音频直接丢弃，不进 sink。
        """
        while True:
            if self._frame_buf:
                return self._frame_buf.popleft()
            if self._packets is None:
                return None
            try:
                packet = next(self._packets)
            except StopIteration:
                return None
            if packet.dts is None:
                continue
            if emit_audio or packet.stream is self.video_stream:
                self._decode_packet(packet)
            # 快进沿途的音频包直接丢弃

    def apply_command(self, command: str) -> None:
        if command == _PLAY:
            self.reset_play_clock()
            logger.debug("命令应用：play（基准 pts=%d）", self._play_start_pts)
        elif command == _PAUSE:
            pass  # 播放节拍由 state != Playing 自然停止；sink 由门面控制

    def reset_play_clock(self) -> None:
        """重置播放节拍基准为当前引擎位置（play 与播放中 seek 共用）。"""
        self._play_start_pts = self._player._position_ms
        self._play_start_wall = self._player._clock_ms()

    def play_next_frame(self) -> _DecodeContext:
        """播放推进：解码一帧（音频同遍流送）→ 投递 → wait 下一帧节拍。"""
        frame = self._next_video_frame(emit_audio=True)
        if frame is None:
            self._player._set_state(PlaybackState.Stopped)
            return self
        ms = frame_time_ms(frame)
        self._player._position_ms = ms
        self._player.frame_ready.emit(av_frame_to_qimage(frame))
        self._player.position_changed.emit(ms)
        fps = stream_fps(self.video_stream)
        interval = max(1.0, 1000.0 / fps)
        target_wall = self._play_start_wall + (ms - self._play_start_pts)
        now = self._player._clock_ms()
        delay = max(0.0, target_wall + interval - now) / 1000.0
        with self._player._cond:
            self._player._cond.wait(timeout=delay)
        return self

    def emit_first_frame(self) -> None:
        """打开完成后投递当前帧（暂停态画面就绪；快进沿途音频丢弃）。"""
        frame = self._next_video_frame(emit_audio=False)
        if frame is None:
            return
        ms = frame_time_ms(frame)
        self._player._position_ms = ms
        self._player.frame_ready.emit(av_frame_to_qimage(frame))
        self._player.position_changed.emit(ms)

    def decode_until(self, target_ms: int) -> _DecodeContext:
        """seek 核心：顺序解码跳过目标之前的帧，投递首个 ≥ 目标的完成帧。"""
        while True:
            frame = self._next_video_frame(emit_audio=False)
            if frame is None:
                break
            ms = frame_time_ms(frame)
            if ms < target_ms - 1:  # 半毫秒容差：关键帧起点与目标同帧
                continue
            self._player._position_ms = ms
            self._player.frame_ready.emit(av_frame_to_qimage(frame))
            self._player.position_changed.emit(ms)
            return self
        logger.info("seek 越过流末尾（target=%dms），停驻最后位置", target_ms)
        return self
