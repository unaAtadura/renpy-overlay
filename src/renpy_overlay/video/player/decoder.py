"""解码 worker：异步 Seek（最新胜出）+ 多帧预解码。

线程模型（全部 av 操作限定在 worker 线程，PyAV 对象非线程安全）：

- 请求优先级：stop > open > seek latch > play/pause 命令 > 播放推进。
- seek 为异步 latch：UI 线程只写最新目标（连拖只保留最终值），worker
  在包间隙检查 latch 并中断当前解码——最新胜出，风暴从源头合并。
- 双缓冲/多帧预解码：worker 恒把视频帧队列填到上限（默认 8 帧）、
  音频环填到水位（800ms），渲染线程按主时钟节拍消费，互不阻塞。
- 单遍 demux 分流：视频/音频共用同一包流按包归属分流解码（双迭代器
  会互相吞包——主项目 av_player 实测教训），快进沿途音频丢弃。
- seek 后音频对齐：丢弃 pts 早于 seek 目标的音频帧（音频与视频按
  关键帧各自起步，不裁剪会有最长一个 GOP 的偏差）。

离线测试：open_fn / resampler_factory / 帧转换器均可注入，DecodeContext
可不启线程同步驱动。
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass

from .keyframe_index import KeyframeIndex, build_index, load_cached, save
from .queues import AudioRingBuffer, BoundedFrameQueue, QueueAborted
from .source_filter import CachedSource, PageCache
from .sync_clock import frame_time_ms, media_duration_ms, stream_fps

logger = logging.getLogger("renpy_overlay.video.player.decoder")

#: 音频输出标准参数：worker 重采样目标（16bit 交错双声道 48kHz）
AUDIO_RATE = 48_000
#: 音频目标水位（毫秒）：低于该值 worker 持续预解码填充。取值需
#: 远大于实时供给的临界平衡余量：播放态 worker 供给速率 ≈ 消费速率
#:（1x 实时），水位过薄时任何抖动（GC/Qt 主线程忙）都会断供——
#: 断供即音频时钟基准重跳，表现为视频成片段跳跃（实测跳 ~1s）
AUDIO_WATERMARK_MS = 800
#: seek 落定后的预填充水位（毫秒）：低水位快启（目标帧尽快流动），
#: 播放恢复后由持续解码自然补足
_SEEK_AUDIO_WATERMARK_MS = 200
#: 音频缓冲容量（毫秒）：水位 × 2 兜底突发
AUDIO_CAPACITY_MS = 2000
#: 预填充目标：打开/seek 落定后至少填满的视频帧数
PREFILL_FRAMES = 2
#: 音频帧丢弃容差（毫秒）：seek 后 pts 早于目标该容差的音频帧丢弃
AUDIO_ALIGN_TOLERANCE_MS = 30

PLAY = "play"
PAUSE = "pause"

#: 全局解码代数号：跨媒体实例单调递增（换片后 gen 不与旧帧撞车）
_GEN_COUNTER = itertools.count(1)


class SeekLatch:
    """异步 seek 请求锁存（最新胜出；线程安全，纯逻辑可单测）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target_ms: int | None = None

    def request(self, target_ms: int) -> None:
        with self._lock:
            self._target_ms = max(0, int(target_ms))

    def pop(self) -> int | None:
        """取走当前挂起目标（无则 None）；一次消费后清零。"""
        with self._lock:
            target, self._target_ms = self._target_ms, None
            return target


@dataclass
class MediaInfo:
    """媒体元数据（worker 打开后回报主线程）。"""

    path: str
    fps: float
    duration_ms: int
    has_audio: bool
    width: int
    height: int
    keyframes: int
    index_source: str  # "cache" | "scan"


@dataclass
class VideoFrame:
    """渲染流水线帧：pts（毫秒）+ 转换后的像素载荷 + 代数号。"""

    pts_ms: int
    payload: object  # 真实路径为 av 帧（yuv420p 直传）；测试可注入任意对象
    gen: int


class DecodeContext:
    """worker 侧解码上下文（仅 worker 线程访问；可注入桩对象同步驱动）。

    hooks 经构造注入：open_fn（av.open）、resampler_factory、
    on_first_frame / on_seek_done（暂停态信箱帧回调）、clock_ms。
    """

    def __init__(
        self,
        *,
        vqueue: BoundedFrameQueue,
        ring: AudioRingBuffer,
        stats=None,
        open_fn=None,
        resampler_factory=None,
        convert_frame=None,
        on_display_frame=None,
        on_audio_written=None,
        on_audio_flushed=None,
        clock_ms=None,
    ) -> None:
        self._vqueue = vqueue
        self._ring = ring
        self._stats = stats
        self._open_fn = open_fn
        self._resampler_factory = resampler_factory
        self._convert = convert_frame or (lambda f: f)
        self._on_display_frame = on_display_frame or (lambda frame: None)
        self._on_audio_written = on_audio_written or (lambda pts_ms, n: None)
        self._on_audio_flushed = on_audio_flushed or (lambda: None)
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self.source: CachedSource | None = None
        self.container = None
        self.video_stream = None
        self.audio_stream = None
        self.index: KeyframeIndex | None = None
        self.info: MediaInfo | None = None
        self.fps = 30.0
        self.duration_ms = -1
        self.gen = next(_GEN_COUNTER)  # 全局递增代数号：seek/open 换代不重置（渲染侧丢陈帧无撞车）
        self.eof = False
        self._packets = None
        self._resampler = None
        self._audio_drop_before_ms = -1  # seek 对齐：早于该 pts 的音频丢弃
        self._pcm_residual = b""  # PCM 对齐残字节（不足 4 字节的样本尾部）
        self._pcm_spill = bytearray()  # 环满暂存区：解码不阻塞、不丢数据
        dump_in = os.environ.get("VP_AUDIO_DUMP_IN")
        self._dump_in = open(dump_in, "wb") if dump_in else None  # 解码端 PCM 转储诊断
        self._position_ms = 0

    # ---- 打开 ---------------------------------------------------------------

    def open(self, path: str) -> bool:
        """打开媒体：缓存源 → 容器 → 索引（缓存优先）→ 元数据 → 预填充。"""
        import av as _av  # 局部导入：模块可离线导入（不依赖 av）

        open_fn = self._open_fn or _av.open
        page_cache = PageCache()
        source = CachedSource(path, page_cache)
        container = open_fn(source)
        video_stream = container.streams.video[0]
        audio_streams = container.streams.audio
        # 关键帧索引：sidecar 命中秒开；未命中全量扫描（经缓存层预热页面）
        index = load_cached(path)
        index_source = "cache"
        if index is None:
            index = build_index(container, video_stream)
            index_source = "scan"
            save(path, index)
            container.seek(0)  # 扫描已推到 EOF：demux 位置复位到流起点
        self.fps = stream_fps(video_stream)
        self.duration_ms = media_duration_ms(video_stream, container.duration)
        info = MediaInfo(
            path=path,
            fps=self.fps,
            duration_ms=self.duration_ms,
            has_audio=bool(audio_streams),
            width=video_stream.codec_context.width,
            height=video_stream.codec_context.height,
            keyframes=len(index),
            index_source=index_source,
        )
        self.attach(source, container, video_stream, audio_streams[0] if audio_streams else None, index)
        self.info = info
        logger.info(
            "媒体已打开：%s（fps=%.3f duration=%dms audio=%s keyframes=%d 索引=%s）",
            path, self.fps, self.duration_ms, info.has_audio, len(index), index_source,
        )
        self._decode_until(0)  # 首帧经信箱上屏（暂停态画面就绪；消费至首帧之后）
        self._prefill()
        return True

    def attach(self, source, container, video_stream, audio_stream, index) -> None:
        self.source = source
        self.container = container
        self.video_stream = video_stream
        self.audio_stream = audio_stream
        self.index = index
        self.reset_streaming()

    def release(self) -> None:
        """弃用当前媒体（换片）：引用置空交由 GC；worker 内执行安全。"""
        self.source = self.container = self.video_stream = None
        self.audio_stream = self.index = self.info = None
        self._packets = None
        self._resampler = None
        self._pcm_spill.clear()
        self.eof = False

    def reset_streaming(self) -> None:
        """seek/open 后重建 demux 流（容器位置已更新）。"""
        if self.container is None:
            self._packets = None
            return
        streams = [s for s in (self.video_stream, self.audio_stream) if s is not None]
        self._packets = self.container.demux(*streams)
        self._resampler = None  # 惰性创建（首个音频帧时）

    # ---- seek ----------------------------------------------------------------

    def seek(self, target_ms: int) -> None:
        """异步 seek 核心：索引直跳关键帧 → 逐帧解码到目标 → 重新预填充。

        队列清空（旧帧由渲染侧 gen 检查淘汰，不用 abort——避免 seek 窗
        口内渲染无帧可取的空转与供给空窗）→ 代数号自增 → 目标帧经信
        箱回调立即上屏 → 低水位预填充快速恢复播放。
        """
        if self.container is None:
            self._position_ms = target_ms
            return
        t0 = self._clock_ms()
        self._vqueue.clear()
        self._ring.clear()
        self._pcm_residual = b""  # 旧时序残字节作废
        self._pcm_spill.clear()  # 暂存区同属旧时序
        self._on_audio_flushed()  # 主时钟基准作废（新时序重建）
        entry = None
        if self.index is not None and len(self.index) > 0:
            entry = self.index.lookup_floor_ms(target_ms)
        if entry is not None:
            # backward=True：floor 语义与索引一致，精确落在条目关键帧
            # （backward=False 实测会被 mov demuxer 跳到下一个关键帧）
            self.container.seek(entry.pts_units, stream=self.video_stream, backward=True)
        else:  # 无索引兜底：容器级 backward 关键帧定位
            self.container.seek(int(target_ms) * 1000, backward=True, any_frame=False)
        self.reset_streaming()
        self.gen += 1
        self.eof = False
        reached = self._decode_until(target_ms)
        latency_ms = self._clock_ms() - t0
        logger.debug("seek 落定：target=%dms reached=%dms latency=%.1fms", target_ms, reached, latency_ms)
        if self._stats is not None:
            self._stats.record_seek(target_ms, reached, latency_ms)
        self._position_ms = reached
        self._prefill(_SEEK_AUDIO_WATERMARK_MS)  # 低水位快启：目标帧尽快流动

    def _decode_until(self, target_ms: int) -> int:
        """顺序解码丢弃目标之前的视频帧；音频帧只对齐不投递。返回实际落点。"""
        self._audio_drop_before_ms = target_ms - AUDIO_ALIGN_TOLERANCE_MS
        while True:
            frame = self._next_video_frame(emit_audio=False)
            if frame is None:
                self.eof = True
                return self._position_ms
            ms = frame_time_ms(frame)
            if ms < target_ms - 1:  # 半毫秒容差：关键帧起点与目标同帧
                continue
            self._on_display_frame(VideoFrame(ms, self._convert(frame), self.gen))
            return ms

    # ---- 播放推进 / 预填充 ----------------------------------------------------

    def step(self, drop_video: bool = False) -> bool:
        """解码推进一步：取下一个视频帧入队（音频同遍流送）。

        drop_video=True：视频帧不入队直接丢弃（预填充补音频水位用，
        队列深度不增长；消费方不在也不会造成 put 阻塞）。返回 False
        表示 EOF；队列满时有界等待（abort/stop 可打断）。
        """
        if self.eof:
            return False
        self._pump_pcm()  # 暂存区回灌（环有空位则续写，不阻塞）
        try:
            frame = self._next_video_frame(emit_audio=True)
        except QueueAborted:
            return False
        if frame is None:
            self.eof = True
            self._push_eof()
            return False
        ms = frame_time_ms(frame)
        self._position_ms = ms
        if not drop_video:
            try:
                self._vqueue.put(VideoFrame(ms, self._convert(frame), self.gen))
            except QueueAborted:
                return False
            if self._stats is not None:
                self._stats.video_decoded += 1
        return True

    def _push_eof(self) -> None:
        try:
            self._vqueue.put(VideoFrame(-1, None, self.gen))
        except QueueAborted:
            pass

    def _prefill(self, audio_watermark_ms: int = AUDIO_WATERMARK_MS) -> None:
        """预填充：视频队列到 PREFILL_FRAMES 且音频水位到目标。

        depth 满足后继续以 drop_video 吃包补音频水位（帧不入队：消费方
        不在也不阻塞）。总步数预算（budget）兜底坏音轨/无音频块场景，
        绝不无限解码。seek 场景传低水位（快启，后台持续补足）。
        """
        budget = 256
        while not self.eof and budget > 0:
            budget -= 1
            if self._vqueue.depth() >= PREFILL_FRAMES and self._audio_ready(
                audio_watermark_ms
            ):
                return
            if self._vqueue.depth() >= self._vqueue.maxdepth - 1:
                return  # 队列近满兜底（put 满会永久等待消费）
            if self._vqueue.depth() >= PREFILL_FRAMES:
                if not self.step(drop_video=True):
                    return
                continue
            if not self.step():
                return

    def _audio_ready(self, watermark_ms: int = AUDIO_WATERMARK_MS) -> bool:
        if self.audio_stream is None:
            return True
        return self._ring.level_ms(AUDIO_RATE, 4) >= watermark_ms

    # ---- 包级处理（单遍 demux 分流） -------------------------------------------

    def _next_video_frame(self, emit_audio: bool):
        """取下一视频帧（None=EOF）；沿途音频按 emit_audio 决定流送或丢弃。"""
        while True:
            if self._packets is None:
                return None
            try:
                packet = next(self._packets)
            except StopIteration:
                return None
            if packet.dts is None and packet.pts is None:
                continue
            if packet.stream is self.video_stream:
                frames = packet.decode()
                if frames:
                    return frames[0]
                continue
            if emit_audio and self.audio_stream is not None:
                self._decode_audio(packet)

    def _decode_audio(self, packet) -> None:
        """音频包 → 重采样 s16/48k/stereo → 环形缓冲（seek 对齐裁剪）。

        写入以 4 字节（单样本）对齐：resampler 块与环形缓冲部分写入的
        边界都可能切在样本中间，不对齐会造成样本错位的持续杂音——
        尾部残字节累积到下一帧拼接。满载时余量进暂存区（配合 worker
        水位节流，稳态不会触达满载）。
        """
        if self._resampler is None:
            factory = self._resampler_factory
            self._resampler = (
                factory() if factory is not None else _default_resampler()
            )
        for frame in packet.decode():
            frame_ms = frame_time_ms(frame)
            if frame_ms < self._audio_drop_before_ms:
                continue  # seek 对齐：丢弃起点之前的音频
            written_total = 0
            for chunk in self._resampler.resample(frame):
                if not chunk.planes:
                    continue
                raw = _plane_bytes(chunk.planes[0])
                # 有效数据按样本数截取：plane.buffer_size 是分配大小（含
                # 128B 对齐 padding）——padding 尾巴混入 PCM 流即周期性
                # 爆音（咔嗒），安静段尤为明显（实测 8320 vs 8192）
                samples = getattr(chunk, "samples", None)
                if samples and samples * 4 < len(raw):
                    raw = raw[: samples * 4]  # stereo × s16
                if self._dump_in:
                    self._dump_in.write(raw)
                buf = self._pcm_residual + raw
                usable = len(buf) - len(buf) % 4
                data, self._pcm_residual = buf[:usable], buf[usable:]
                while data:
                    written = self._ring.write(data)
                    written_total += written
                    if written >= len(data):
                        break
                    if written == 0:
                        # 环满：剩余 PCM 进暂存区（不丢、不阻塞解码；
                        # step/_pump_pcm 在环有空位时回灌）——弃置会造成
                        # 波形断裂（咔嗒），阻塞会造成帧流落后（冻结）
                        self._pcm_spill += data
                        break
                    data = data[written:]
            if written_total > 0:
                self._on_audio_written(frame_ms, written_total)

    def _pump_pcm(self) -> None:
        """暂存 PCM 回灌环形缓冲（有空间即续写；非阻塞）。"""
        if self._pcm_spill:
            written = self._ring.write(bytes(self._pcm_spill))
            if written:
                del self._pcm_spill[:written]

    @property
    def position_ms(self) -> int:
        return self._position_ms


class DecoderWorker(threading.Thread):
    """解码线程：消费 open/seek/命令请求，持续预填充双缓冲。"""

    def __init__(self, *, vqueue, ring, stats=None, **hooks) -> None:
        super().__init__(name="renpy-overlay-video-decoder", daemon=True)
        self._vqueue = vqueue
        self._ring = ring
        self._stats = stats
        self._hooks = hooks
        self._cond = threading.Condition()
        self._open_request: str | None = None
        self._latch = SeekLatch()
        self._command: str | None = None
        self._stop_flag = False
        self._playing = False
        self.ctx: DecodeContext | None = None

    # ---- 主线程 API -----------------------------------------------------------

    def request_open(self, path: str) -> None:
        with self._cond:
            self._open_request = path
            self._cond.notify_all()

    def request_seek(self, target_ms: int) -> None:
        self._latch.request(target_ms)
        with self._cond:
            self._cond.notify_all()

    def request_command(self, command: str) -> None:
        with self._cond:
            self._command = command
            self._cond.notify_all()

    def set_playing(self, playing: bool) -> None:
        self._playing = playing

    def stop(self) -> None:
        self._stop_flag = True
        self._vqueue.abort()
        with self._cond:
            self._cond.notify_all()

    @property
    def playing(self) -> bool:
        return self._playing

    # ---- 线程主体 ---------------------------------------------------------------

    def run(self) -> None:  # pragma: no cover - 线程编排（组件均可离线驱动）
        try:
            while not self._stop_flag:
                if self._drain_requests():
                    continue
                if self.ctx is not None and self._playing and not self.ctx.eof:
                    # 无音频水位等待：音频超前时 PCM 进暂存区（_pump_pcm 回
                    # 灌），解码速率由视频队列上限（渲染消费）自然约束——
                    # 「整步等待」会使 worker 有效速率跌至 ~0.1x（水位超出
                    # 量需被整段消费后才能再走一步），帧流落后时钟导致
                    # 渲染批量丢帧、画面冻结而声音继续
                    self.ctx.step()
                    continue
                with self._cond:
                    self._cond.wait(timeout=0.05)
        except Exception:  # pragma: no cover - worker 兜底隔离
            logger.warning("解码线程异常退出", exc_info=True)

    def _drain_requests(self) -> bool:
        """处理挂起请求（返回 True 表示做了事，立即再轮询）。"""
        if self._stop_flag:
            return False
        path, self._open_request = self._open_request, None
        if path is not None:
            self._do_open(path)
            return True
        target = self._latch.pop()
        if target is not None:
            if self.ctx is not None:
                self.ctx.seek(target)
            return True
        command, self._command = self._command, None
        if command == PLAY:
            self._playing = True
            return True
        if command == PAUSE:
            self._playing = False
            return True
        return False

    def _do_open(self, path: str) -> None:
        old, self.ctx = self.ctx, None
        if old is not None:
            old.release()
        self._vqueue.clear()  # 旧帧由渲染侧 gen 检查淘汰（全局代数号不重置，无撞车）
        self._ring.clear()
        if self._hooks.get("on_audio_flushed") is not None:
            self._hooks["on_audio_flushed"]()  # 旧音频时序作废（时钟基准重建）
        # worker 级回调（on_open_failed/on_opened）不传入 ctx：仅线程编排层消费
        ctx_hook_keys = (
            "open_fn", "resampler_factory", "convert_frame",
            "on_display_frame", "on_audio_written", "on_audio_flushed",
            "clock_ms",
        )
        ctx = DecodeContext(
            vqueue=self._vqueue,
            ring=self._ring,
            stats=self._stats,
            **{k: v for k, v in self._hooks.items() if k in ctx_hook_keys},
        )
        self.ctx = ctx  # 提前挂载：渲染 gen provider 立即有效（否则 open 窗口期返回 -1，预填充全被判陈帧）
        try:
            ctx.open(path)
        except Exception as exc:
            logger.warning("打开媒体失败：%s（%s）", path, exc)
            self.ctx = None
            if self._hooks.get("on_open_failed") is not None:
                self._hooks["on_open_failed"](f"{path}: {exc}")
            return
        self._playing = False
        if self._hooks.get("on_opened") is not None:
            self._hooks["on_opened"](ctx.info)


def _default_resampler():
    import av

    return av.AudioResampler(format="s16", layout="stereo", rate=AUDIO_RATE)


def _plane_bytes(plane) -> bytes:
    """解码平面缓冲 → bytes（av 19 的 Plane 无 to_bytes，经指针拷贝）。"""
    import ctypes

    return ctypes.string_at(plane.buffer_ptr, plane.buffer_size)
