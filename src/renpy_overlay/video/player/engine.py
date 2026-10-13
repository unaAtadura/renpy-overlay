"""播放引擎门面：组装解码 worker / 双缓冲 / SDL 音频 / SDL 渲染。

主线程 API 沿用 QMediaPlayer 风格（open/position/setPosition/play/pause/
playbackState/setVolume/duration），UI 层以 Qt 信号消费引擎事件；worker
与渲染线程的回调经信号跨线程投递（PyQt 自动队列化），主线程零解码工作。

时钟体系：音频主时钟（callback 计量）→ 渲染节拍；无音轨或基准失效时
渲染侧回退墙钟推算（play 基准 + 已流逝时间）。

移植适配（相对参考项目 player.py）：
- 状态改为 :class:`PlaybackState` 枚举（主项目 editor_window 比较口径）。
- 新增 ``grab_frame_image()``：SDL present 不经过 Qt 图像，暂停放大帧
  按需把最近显示帧（yuv420p）转 QImage（非热路径，单次转换）。
- ``hasMedia()``：进度条/起止点就绪判定（duration >= 0）。
"""

from __future__ import annotations

import enum
import logging
import time

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from .decoder import AUDIO_CAPACITY_MS, PAUSE, PLAY, DecoderWorker, VideoFrame
from .queues import AudioRingBuffer, BoundedFrameQueue
from .stats import Stats

logger = logging.getLogger("renpy_overlay.video.player.engine")

#: 视频帧队列深度（多帧预解码上限）
FRAME_QUEUE_DEPTH = 8


class PlaybackState(enum.IntEnum):
    """播放状态（语义对齐主项目 editor_window 的比较用法）。"""

    Stopped = 0
    Playing = 1
    Paused = 2


class VideoPlayer(QObject):
    """引擎门面（主线程对象；信号槽跨线程投递）。"""

    media_opened = pyqtSignal(object)  # MediaInfo
    position_changed = pyqtSignal(int)
    state_changed = pyqtSignal(object)  # PlaybackState
    open_failed = pyqtSignal(str)
    ended = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        open_fn=None,
        resampler_factory=None,
        convert_frame=None,
        clock_ms=None,
        enable_worker: bool = True,
    ) -> None:
        super().__init__(parent)
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._vqueue = BoundedFrameQueue(FRAME_QUEUE_DEPTH)
        bytes_per_ms = 48_000 * 4 / 1000.0  # 192 字节/ms（48kHz × s16 × 立体声）
        self._ring = AudioRingBuffer(int(AUDIO_CAPACITY_MS * bytes_per_ms))
        self._stats = Stats()
        self._state = PlaybackState.Stopped
        self._position_ms = 0
        self._duration_ms = -1
        self._volume = 1.0
        self._audio = None  # SdlAudioOutput（open 成功后主线程创建）
        self._renderer = None
        self._sdl_ready = False
        self._last_frame: VideoFrame | None = None  # 最近显示帧（暂停放大帧用）
        #: worker 回调（worker 线程执行；信号发射安全）
        self._worker = DecoderWorker(
            vqueue=self._vqueue,
            ring=self._ring,
            stats=self._stats,
            open_fn=open_fn,
            resampler_factory=resampler_factory,
            convert_frame=convert_frame or _default_convert_frame,
            on_display_frame=self._on_display_frame,
            on_audio_written=self._on_audio_written,
            on_audio_flushed=self._on_audio_flushed,
            on_open_failed=lambda msg: self.open_failed.emit(msg),
            on_opened=self._on_worker_opened,
        )
        if enable_worker:
            self._worker.start()

    # ---- 主线程 API -----------------------------------------------------------

    def open(self, path: str) -> None:
        """请求打开媒体（异步；完成经 media_opened 回报，落定为暂停态）。"""
        self._ensure_sdl()
        self._set_state(PlaybackState.Stopped)
        self._position_ms = 0
        self._duration_ms = -1
        self._last_frame = None
        self._worker.request_open(path)

    def setPosition(self, position_ms: int) -> None:
        """异步 seek（最新胜出；完成帧经信箱/队列上屏）。"""
        self._worker.request_seek(position_ms)

    def play(self) -> None:
        self._worker.request_command(PLAY)
        if self._audio is not None:
            self._audio.start()
        if self._renderer is not None:
            self._renderer.set_wall_base(self._position_ms)
        self._set_state(PlaybackState.Playing)

    def pause(self) -> None:
        self._worker.request_command(PAUSE)
        if self._audio is not None:
            self._audio.pause()
        self._set_state(PlaybackState.Paused)

    def playbackState(self) -> PlaybackState:
        return self._state

    def position(self) -> int:
        return self._position_ms

    def duration(self) -> int:
        return self._duration_ms

    def hasMedia(self) -> bool:
        return self._duration_ms >= 0

    def setVolume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        if self._audio is not None:
            self._audio.set_volume(self._volume)

    def volume(self) -> float:
        return self._volume

    def ensure_sdl(self) -> None:
        """SDL 全局初始化（渲染器 attach 前必须完成；可重复调用）。"""
        self._ensure_sdl()

    def set_renderer(self, renderer) -> None:
        """注入渲染线程（UI 层构造后传入；attach 生命周期由渲染器自管）。"""
        self._renderer = renderer

    def renderer_wiring(self) -> dict:
        """渲染线程装配参数（UI 层构造 SdlVideoRenderer 时展开）。"""
        return {
            "vqueue": self._vqueue,
            "clock_provider": self._audio_clock_provider,
            "state_provider": self._state_name_provider,
            "gen_provider": self._current_gen,
            "on_presented": self.on_presented,
            "on_ended": self.on_ended,
            "stats": self._stats,
        }

    def grab_frame_image(self) -> QImage | None:
        """最近显示帧 → QImage（暂停放大帧按需单次转换；无帧返回 None）。"""
        frame = self._last_frame
        if frame is None or frame.payload is None:
            return None
        try:
            pil_image = frame.payload.to_image()
        except Exception:  # pragma: no cover - 元数据异常兜底
            logger.warning("放大帧转换失败", exc_info=True)
            return None
        data = pil_image.tobytes("raw", "RGB")
        image = QImage(
            data, pil_image.width, pil_image.height, pil_image.width * 3,
            QImage.Format.Format_RGB888,
        )
        return image.copy()  # 脱离临时 bytes 缓冲

    def _audio_clock_provider(self):
        """音频主时钟提供者（无音频设备时返回 None → 渲染侧回退墙钟）。"""
        if self._audio is not None:
            return self._audio.clock.clock_ms()
        return None

    def _state_name_provider(self) -> str:
        """渲染线程状态名（小写字符串，与 renderer 常量对齐）。"""
        return self._state.name.lower()

    def _current_gen(self) -> int:
        """当前解码代数号（worker 未打开媒体时返回 -1，全部帧视为陈帧）。"""
        ctx = self._worker.ctx
        return ctx.gen if ctx is not None else -1

    def stats_snapshot(self) -> dict:
        """引擎指标快照（诊断心跳周期轮询）。"""
        snap = self._stats.snapshot()
        if self._audio is not None:
            snap["audio_level_ms"] = round(self._audio.level_ms(), 1)
            clock = self._audio.clock.clock_ms()
            snap["clock_ms"] = round(clock) if clock is not None else -1
            snap["reset_jump_ms"] = round(self._audio.clock.last_reset_jump_ms())
            snap["reset_count"] = self._audio.clock.reset_count()
        else:
            snap["clock_ms"] = -1
            snap["reset_jump_ms"] = 0
            snap["reset_count"] = 0
        snap["last_pts"] = self._position_ms
        snap["state"] = self._state.name.lower()
        if self._worker.ctx is not None and self._worker.ctx.source is not None:
            cache = self._worker.ctx.source.cache
            snap["cache_hits"] = cache.hits
            snap["cache_misses"] = cache.misses
            snap["cache_pages"] = cache.page_count()
        snap["vqueue_depth"] = self._vqueue.depth()
        return snap

    def close(self) -> None:
        """停止引擎线程与 SDL 设备（窗口销毁/退出时调用）。"""
        self._worker.stop()
        if self._renderer is not None:
            self._renderer.stop()
        if self._audio is not None:
            self._audio.close()

    # ---- worker 线程回调（经信号/信箱跨线程） -----------------------------------

    def _on_display_frame(self, frame: VideoFrame) -> None:
        """暂停态信箱帧（打开首帧/seek 目标帧）：立即上屏 + 位置回报。"""
        self._last_frame = frame
        if frame.pts_ms >= 0:
            self._position_ms = frame.pts_ms
            if self._state == PlaybackState.Playing and self._renderer is not None:
                # 播放中 seek：墙钟基准跟到新落点（音频时钟 flush 后由
                # on_write 重建，两者衔接期渲染回退墙钟不回跳旧位置）
                self._renderer.set_wall_base(frame.pts_ms)
            self.position_changed.emit(frame.pts_ms)
        if self._renderer is not None:
            self._renderer.present_now(frame)

    def _on_audio_written(self, pts_ms: int, n_bytes: int) -> None:
        if self._audio is not None:
            self._audio.clock.on_write(pts_ms, n_bytes)

    def _on_audio_flushed(self) -> None:
        if self._audio is not None:
            self._audio.clock.on_flush()

    # ---- 渲染线程回调 -----------------------------------------------------------

    def on_presented(self, frame: VideoFrame) -> None:
        """渲染线程上屏回报：位置推进（信号队列投递主线程）。"""
        self._last_frame = frame
        self._position_ms = frame.pts_ms
        self.position_changed.emit(frame.pts_ms)

    def on_ended(self) -> None:
        """播放自然结束：停态 + 位置归到时长。"""
        self._worker.request_command(PAUSE)
        if self._audio is not None:
            self._audio.pause()
        if self._duration_ms > 0:
            self._position_ms = self._duration_ms
            self.position_changed.emit(self._position_ms)
        self._set_state(PlaybackState.Stopped)
        self.ended.emit()

    def _on_worker_opened(self, info) -> None:
        """open 成功（worker 线程回调）：音频设备就绪 + 元数据信号投递。

        仅在 stopped（无用户意图）时落暂停态：open 请求前用户已 play
        （异步竞态）则保留 playing，否则播放被 open 完成回调吞掉。
        """
        self.create_audio_output()
        self._duration_ms = info.duration_ms
        if self._state == PlaybackState.Stopped:
            self._set_state(PlaybackState.Paused)
        self.media_opened.emit(info)

    def worker_opened(self) -> bool:
        """worker 是否已持有打开的媒体（open 成功回调由 worker 发出）。"""
        return self._worker.ctx is not None and self._worker.ctx.info is not None

    # ---- 内部 ---------------------------------------------------------------

    def _set_state(self, state: PlaybackState) -> None:
        if self._state != state:
            logger.debug("播放状态：%s → %s", self._state.name, state.name)
            self._state = state
            self.state_changed.emit(state)

    def _ensure_sdl(self) -> None:
        """SDL 全局初始化（VIDEO|AUDIO；进程内一次）。"""
        if self._sdl_ready:
            return
        try:
            import sdl2
        except ImportError:
            logger.warning("pysdl2 不可用：SDL 音视频降级（播放将无声且无画面）")
            return
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_AUDIO) != 0:
            err = sdl2.SDL_GetError().decode(errors="replace")
            logger.error("SDL_Init 失败：%s", err)
            return
        self._sdl_ready = True
        logger.info("SDL 已初始化（VIDEO|AUDIO）")

    def create_audio_output(self) -> None:
        """open 成功后创建音频设备（主线程；无音轨也创建——静音空转）。"""
        if self._audio is not None:
            return
        from .audio_output import SdlAudioOutput

        try:
            self._audio = SdlAudioOutput(
                self._ring, volume=self._volume, stats=self._stats
            )
        except (ImportError, RuntimeError) as exc:
            logger.warning("音频输出创建失败（播放将无声）：%s", exc)
            return
        if self._state == PlaybackState.Playing:
            # 设备创建晚于 play（open 异步竞态）：按当前状态直接起播
            self._audio.start()


def _default_convert_frame(frame):
    """帧统一为 yuv420p（渲染纹理 IYUV 直传；原生即该格式则零转换）。"""
    try:
        fmt = frame.format
        if isinstance(fmt, int):
            import av

            name = av.VideoFormat(fmt).name
        else:
            name = getattr(fmt, "name", str(fmt))
        if name == "yuv420p":
            return frame
        return frame.reformat(format="yuv420p")
    except Exception:  # pragma: no cover - 元数据异常兜底
        return frame.reformat(format="yuv420p")
