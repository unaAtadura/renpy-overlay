"""实验指标聚合（线程安全快照，诊断心跳周期轮询展示）。

验收观测口径：
- seek_latency_ms：最近一次 seek 从请求落定到目标帧就绪的耗时
- video_decoded / presented / dropped / stale：解码与渲染流水线计数
- audio_level_ms：音频独立缓冲水位；underruns：callback 缺数据次数
- render_fps：渲染线程 present 速率（EMA）
- cache_hits / misses / pages：源滤镜分页缓存命中情况
"""

from __future__ import annotations

import threading


class Stats:
    """跨线程计数器（GIL 下整型读写原子，聚合快照加锁保一致）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.video_decoded = 0
        self.presented = 0
        self.dropped = 0
        self.stale = 0
        self.underruns = 0
        self.mixed_calls = 0  # callback 混音调用次数（有数据才混）
        self.mixed_silent = 0  # 混音后输出仍全零的次数（无声定位：>0 即设备层异常）
        self.audio_dropped = 0  # 音频满载弃置次数（非零即咔嗒杂音源）
        self.partial_reads = 0  # callback 部分读次数（数据不足一块，尾部补静音）
        self.edge_jumps = 0  # 相邻 callback 边界样本跳变次数（波形断裂实证）
        self.cb_len_misaligned = 0  # callback 长度非 4 对齐次数
        self.cb_len_max = 0  # callback 长度最大值
        self.total_blackouts = 0  # 完全断供（n==0）次数
        self.reset_jump_ms = 0  # 最近一次时钟重跳幅度（ms，正=前跳）
        self.intra_jumps = 0  # callback 块内相邻样本跳变（写入数据错乱实证）
        self.render_fps = 0.0
        self.audio_level_ms = 0.0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_pages = 0
        self.seek_count = 0
        self.seek_target_ms = 0
        self.seek_reached_ms = 0
        self.seek_latency_ms = 0.0

    def record_seek(self, target_ms: int, reached_ms: int, latency_ms: float) -> None:
        with self._lock:
            self.seek_count += 1
            self.seek_target_ms = target_ms
            self.seek_reached_ms = reached_ms
            self.seek_latency_ms = latency_ms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "video_decoded": self.video_decoded,
                "presented": self.presented,
                "dropped": self.dropped,
                "stale": self.stale,
                "underruns": self.underruns,
                "mixed_calls": self.mixed_calls,
                "mixed_silent": self.mixed_silent,
                "audio_dropped": self.audio_dropped,
                "partial_reads": self.partial_reads,
                "edge_jumps": self.edge_jumps,
                "cb_len_misaligned": self.cb_len_misaligned,
                "cb_len_max": self.cb_len_max,
                "total_blackouts": self.total_blackouts,
                "reset_jump_ms": self.reset_jump_ms,
                "intra_jumps": self.intra_jumps,
                "render_fps": round(self.render_fps, 1),
                "audio_level_ms": round(self.audio_level_ms, 1),
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "cache_pages": self.cache_pages,
                "seek_count": self.seek_count,
                "seek_target_ms": self.seek_target_ms,
                "seek_reached_ms": self.seek_reached_ms,
                "seek_latency_ms": round(self.seek_latency_ms, 1),
            }
