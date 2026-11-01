"""音频波形提取：解码音频轨 → 分块峰值 → 声波图渲染数据。

选型与关键帧索引（player/keyframe_index）同思路：PyAV 进程内解码 +
sidecar 文件缓存（``<视频路径>.waveform.json``），二次打开免重复解码。
缓存以源文件 mtime + 尺寸校验，不一致弃用重建。

峰值口径：每 ``block_ms`` 毫秒取块内样本绝对值最大值，归一化到
[0, 1]（s16 满幅 32768）。声波图按可视窗口对 peaks 分桶重采样绘制
（缩放不重新解码）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renpy_overlay.video.clip.waveform")

#: 默认分块分辨率（毫秒/块）：10ms 在常用缩放范围下足够细腻且体积可控
DEFAULT_BLOCK_MS = 10
_S16_MAX = 32_768.0
_CACHE_SUFFIX = ".waveform.json"


@dataclass
class WaveformData:
    """波形数据：等宽分块峰值序列（覆盖 [0, duration_ms)）。"""

    duration_ms: int
    block_ms: int
    peaks: list[float]  # 每块 max|x| / 32768（0..1）

    def __post_init__(self) -> None:
        if self.block_ms <= 0:
            raise ValueError(f"block_ms 必须为正：{self.block_ms}")

    def peak_at(self, position_ms: int) -> float:
        """取某位置的峰值（越界返回 0；声波图指针着色等用途）。"""
        idx = int(position_ms) // self.block_ms
        if 0 <= idx < len(self.peaks):
            return self.peaks[idx]
        return 0.0

    def window_peaks(self, start_ms: int, end_ms: int, bucket_count: int) -> list[float]:
        """[start, end) 可视窗口 → bucket_count 个桶（桶内取最大值）。

        声波图缩放绘制的唯一入口：不重新解码，对 peaks 分桶重采样。
        空窗口返回全 0。
        """
        if bucket_count <= 0 or end_ms <= start_ms:
            return [0.0] * max(0, bucket_count)
        first = max(0, start_ms // self.block_ms)
        last = min(len(self.peaks), max(first, -(-end_ms // self.block_ms)))
        span = max(1, last - first)
        buckets = [0.0] * bucket_count
        for idx in range(first, last):
            bucket = (idx - first) * bucket_count // span
            if self.peaks[idx] > buckets[bucket]:
                buckets[bucket] = self.peaks[idx]
        return buckets


def extract_waveform(
    path: str | Path, block_ms: int = DEFAULT_BLOCK_MS, progress=None
) -> WaveformData | None:
    """解码音频轨生成分块峰值（无音轨/解码失败返回 ``None`` 不抛异常）。

    ``progress(done, total)`` 可选回调（worker 线程调用，供日志区域
    显示进度）。帧级粒度聚合到块，内存只保留峰值数组。
    """
    try:
        import audioop

        import av

        with av.open(str(path)) as container:
            audio_streams = container.streams.audio
            if not audio_streams:
                logger.info("无音频轨，跳过波形提取：%s", path)
                return None
            stream = audio_streams[0]
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=16_000
            )
            block_bytes = max(2, 16_000 * block_ms // 1000) * 2  # s16 字节宽
            peaks: list[float] = []
            carry = bytearray()
            total_frames = (
                int(stream.frames) if stream.frames and stream.frames > 0 else 0
            )
            decoded = 0  # 本地解码帧计数（AudioFrame 无 index 属性，不可读 frame.index）
            for frame in container.decode(stream):
                for plane in resampler.resample(frame):
                    carry.extend(bytes(plane.planes[0]))
                # audioop.max（C 实现）按块取绝对值最大样本，避免逐样本
                # Python 循环（30 分钟视频 ~29M 样本，纯循环分钟级）
                while len(carry) >= block_bytes:
                    chunk = bytes(carry[:block_bytes])
                    del carry[:block_bytes]
                    peaks.append(audioop.max(chunk, 2) / _S16_MAX)
                decoded += 1
                if total_frames:
                    if progress is not None and decoded % 512 == 0:
                        progress(decoded, total_frames)
            if carry:  # 尾块不足一块也保留
                peaks.append(audioop.max(bytes(carry), 2) / _S16_MAX)
            duration_ms = len(peaks) * block_ms
            if progress is not None and total_frames:
                progress(total_frames, total_frames)
            logger.info("波形提取完成：%s（%d 块 × %dms）", path, len(peaks), block_ms)
            return WaveformData(duration_ms, block_ms, peaks)
    except Exception:
        logger.warning("波形提取失败：%s", path, exc_info=True)
        return None


def cached_waveform(
    path: str | Path, block_ms: int = DEFAULT_BLOCK_MS, progress=None
) -> WaveformData | None:
    """带 sidecar 缓存的提取入口（mtime+size 校验；缓存不可用自动重建）。"""
    source = Path(path)
    cache_file = source.with_name(source.name + _CACHE_SUFFIX)
    cached = _load_cache(cache_file, source, block_ms)
    if cached is not None:
        return cached
    data = extract_waveform(source, block_ms, progress)
    if data is not None:
        _save_cache(cache_file, source, data)
    return data


def _load_cache(
    cache_file: Path, source: Path, block_ms: int
) -> WaveformData | None:
    try:
        if not cache_file.is_file():
            return None
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        stat = source.stat()
        if (
            payload.get("mtime") != stat.st_mtime
            or payload.get("size") != stat.st_size
            or payload.get("block_ms") != block_ms
        ):
            return None
        peaks = payload.get("peaks")
        if not isinstance(peaks, list):
            return None
        return WaveformData(
            duration_ms=int(payload["duration_ms"]),
            block_ms=int(payload["block_ms"]),
            peaks=[float(p) for p in peaks],
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        logger.debug("波形缓存读取失败（忽略并重建）：%s", cache_file, exc_info=True)
        return None


def _save_cache(cache_file: Path, source: Path, data: WaveformData) -> None:
    try:
        stat = source.stat()
        payload = {
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "duration_ms": data.duration_ms,
            "block_ms": data.block_ms,
            "peaks": data.peaks,
        }
        cache_file.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
    except OSError:
        logger.debug("波形缓存写入失败（忽略）：%s", cache_file, exc_info=True)
