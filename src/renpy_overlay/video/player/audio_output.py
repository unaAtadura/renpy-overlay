"""SDL2 音频输出：callback 模式 + 独立 PCM 环形缓冲 + 主时钟。

音视频同步策略的音频侧：

- 独立缓冲：解码 worker 把重采样后的 s16/48kHz/stereo PCM 写入
  :class:`~renpy_overlay.video.player.queues.AudioRingBuffer`（目标水位
  800ms），与视频帧队列彻底解耦；SDL 音频线程在 callback 里只做
  「取数 + 混音」，永不阻塞（缺数据补静音并计 underrun）。
- 主时钟：callback 精确计量已消费字节 → 音频主时钟
  ``base_pts + (consumed - consumed_at_base)``；underrun（缓冲耗尽）
  时失效基准，恢复供给后按新数据 pts 重跳——时钟永远贴合实际听到的
  声音。渲染线程按该时钟节拍上屏（见 sync_clock/renderer_sdl2）。

SDL 设备生命周期：player 在主线程创建/关闭（SDL 无 Qt 亲和性，创建
线程即归属线程，统一收口便于清理）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading

import sdl2

logger = logging.getLogger("renpy_overlay.video.player.audio_output")

#: 音频输出标准参数（与 decoder 重采样目标一致）
DEFAULT_RATE = 48_000
DEFAULT_CHANNELS = 2
#: callback 请求块（样本数/声道）：1024 ≈ 21.3ms，粒度够细、频率不密
DEFAULT_SAMPLES = 1024
SDL_MIX_MAXVOLUME = 128


def _scale_pcm(data: bytes, volume: float) -> bytes:
    """s16 PCM 按音量缩放（audioop C 实现；缺失时原样直出全音量）。"""
    if volume >= 0.999:
        return data
    try:
        import audioop

        return audioop.mul(data, 2, volume)
    except ImportError:  # pragma: no cover - 无 audioop-lts 环境
        return data


class AudioClock:
    """音频主时钟：基准 pts + callback 计量的已消费量（线程安全）。

    语义：``clock_ms = base_pts + (consumed - consumed_at_base)`` 换算。
    基准失效时机：clear（seek/换片，旧时序作废）与 underrun（缓冲耗尽，
    恢复供给后按新数据 pts 重跳）。
    """

    def __init__(self, sample_rate: int, frame_size: int) -> None:
        self._rate = sample_rate
        self._frame_size = max(1, frame_size)
        self._lock = threading.Lock()
        self._base_ms: float | None = None
        self._consumed_at_base = 0
        self._consumed_total = 0
        self._last_reset_jump = 0.0
        self._reset_count = 0

    def on_write(self, pts_ms: int, n_bytes: int) -> None:
        """解码侧写入后登记（基准仅在失效后由首个写入重建）。"""
        if n_bytes <= 0:
            return
        with self._lock:
            was_broken = self._base_ms is None
            old_clock = self._clock_ms_unlocked()
            if self._base_ms is None:
                self._base_ms = float(pts_ms)
                self._consumed_at_base = self._consumed_total
            if was_broken:
                self._last_reset_jump = self._clock_ms_unlocked() - (
                    old_clock if old_clock is not None else 0.0
                )
                self._reset_count += 1

    def on_read(self, n_bytes: int) -> None:
        """callback 消费计量（SDL 音频线程）。"""
        if n_bytes <= 0:
            return
        with self._lock:
            self._consumed_total += n_bytes

    def on_underrun(self) -> None:
        """callback 发现缓冲耗尽：基准失效，等待下次写入重跳。"""
        with self._lock:
            self._base_ms = None

    def last_reset_jump_ms(self) -> float:
        with self._lock:
            return self._last_reset_jump

    def reset_count(self) -> int:
        with self._lock:
            return self._reset_count

    def on_flush(self) -> None:
        """seek/换片清空缓冲：基准与计量全部作废。"""
        with self._lock:
            self._base_ms = None
            self._consumed_total = 0
            self._consumed_at_base = 0

    def clock_ms(self) -> float | None:
        """当前主时钟（毫秒）；基准失效返回 None（渲染侧回退墙钟）。"""
        return self._clock_ms_locked()

    def _clock_ms_unlocked(self) -> float | None:
        if self._base_ms is None:
            return None
        consumed = self._consumed_total - self._consumed_at_base
        return self._base_ms + consumed * 1000.0 / (self._rate * self._frame_size)

    def _clock_ms_locked(self) -> float | None:
        with self._lock:
            return self._clock_ms_unlocked()


class SdlAudioOutput:
    """SDL 音频设备（callback 模式）：环形缓冲消费 + 音量混音 + 主时钟。"""

    def __init__(
        self,
        ring,
        *,
        rate: int = DEFAULT_RATE,
        channels: int = DEFAULT_CHANNELS,
        samples: int = DEFAULT_SAMPLES,
        volume: float = 1.0,
        stats=None,
    ) -> None:
        self._ring = ring
        self._volume = max(0.0, min(1.0, float(volume)))
        self._stats = stats
        self._prev_last = None  # 上一 callback 块末样本（边界跳变检测）
        self._prev_last_first = None  # 上一 callback 块首样本
        dump_path = os.environ.get("VP_AUDIO_DUMP")
        self._dump_file = open(dump_path, "wb") if dump_path else None  # PCM 转储诊断
        self._callback = sdl2.SDL_AudioCallback(self._audio_callback)  # 防 GC
        want = sdl2.SDL_AudioSpec(rate, sdl2.AUDIO_S16SYS, channels, samples)
        want.callback = self._callback
        have = sdl2.SDL_AudioSpec(rate, sdl2.AUDIO_S16SYS, channels, samples)
        self._dev = sdl2.SDL_OpenAudioDevice(None, 0, want, have, 0)
        if not self._dev:
            err = sdl2.SDL_GetError().decode(errors="replace")
            raise RuntimeError(f"SDL 音频设备打开失败：{err}")
        self.rate = have.freq
        self.channels = have.channels
        self.samples = have.samples
        self.frame_size = self.channels * 2  # s16
        self.clock = AudioClock(self.rate, self.frame_size)
        self._paused = True
        logger.info(
            "SDL 音频设备就绪：%dHz/%dch/s16（块 %d 样本 ≈ %.1fms）",
            self.rate, self.channels, self.samples,
            self.samples / self.rate * 1000.0,
        )

    # ---- 播放控制 -----------------------------------------------------------

    def start(self) -> None:
        sdl2.SDL_PauseAudioDevice(self._dev, 0)
        self._paused = False

    def pause(self) -> None:
        sdl2.SDL_PauseAudioDevice(self._dev, 1)
        self._paused = True

    @property
    def paused(self) -> bool:
        return self._paused

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))

    def level_ms(self) -> float:
        return self._ring.level_ms(self.rate, self.frame_size)

    def close(self) -> None:
        if self._dump_file:
            self._dump_file.close()
            self._dump_file = None
        if self._dev:
            sdl2.SDL_CloseAudioDevice(self._dev)
            self._dev = 0

    # ---- SDL 音频线程（实时约束：不阻塞、不分配大对象） -------------------------

    def _audio_callback(self, userdata, stream, length) -> None:
        """callback：取环形缓冲 → 静音兜底 → 按音量缩放直写；计量主时钟。"""
        if self._stats is not None:
            if length % 4:
                self._stats.cb_len_misaligned += 1
            if length > self._stats.cb_len_max:
                self._stats.cb_len_max = length
        ctypes.memset(stream, 0, length)
        data = self._ring.read(length)
        n = len(data)
        if n < length and self._stats is not None:
            self._stats.underruns += 1
            if n == 0:
                self._stats.total_blackouts += 1  # 完全断供：允许基准重跳
            else:
                self._stats.partial_reads += 1  # 部分读：尾部静音短填补，基准不动
        if n > 0:
            self.clock.on_read(n)
            if self._volume > 0.0:
                # 不用 SDL_MixAudio：pysdl2 绑定实测调用无效（写入未发生，
                # 混音后输出仍全零）。改用 audioop（C 实现）按音量缩放后
                # memmove 直写——audioop-lts 为主项目共享依赖
                scaled = _scale_pcm(data, self._volume)
                ctypes.memmove(stream, scaled, len(scaled))
                # 波形断裂实证：相邻 callback 边界样本跳变（前块末样本 vs
                # 本块首样本）。正常音频边界差值小；接近满幅的跳变即为
                # 听感咔嗒
                if self._stats is not None:
                    samples = ctypes.cast(
                        stream, ctypes.POINTER(ctypes.c_int16)
                    )
                    first = samples[0]
                    self._prev_last = samples[length // 2 - 1]
                    prev = self._prev_last_first
                    self._prev_last_first = first
                    if prev is not None and abs(first - prev) > 8000:
                        self._stats.edge_jumps += 1
                    prev_s = None
                    for idx in range(0, length // 2, 16):
                        sv = samples[idx]
                        if prev_s is not None and abs(sv - prev_s) > 12000:
                            self._stats.intra_jumps += 1
                        prev_s = sv
            if self._stats is not None:
                # 无声定位：混音后回读首样本。采样窗口避开片头（内容静音
                # 会污染判定）：mixed_calls > 500（≈10s）后采 64 次
                self._stats.mixed_calls += 1
                if 500 < self._stats.mixed_calls <= 564:
                    s2 = ctypes.cast(stream, ctypes.POINTER(ctypes.c_int16))
                    if s2[0] == 0 and s2[1] == 0:
                        self._stats.mixed_silent += 1
        if n == 0:  # 仅完全断供才失效基准（部分读保持时钟连续）
            self.clock.on_underrun()
        if self._dump_file:  # PCM 转储诊断（最终输出，mix 后）
            self._dump_file.write(ctypes.string_at(stream, length))
