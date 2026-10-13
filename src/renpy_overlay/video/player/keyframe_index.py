"""关键帧索引预解析 + sidecar 文件缓存。

打开媒体时后台全量 demux 一遍视频流，记录每个关键帧的
``(pts_units, byte_offset)`` 有序表；持久化到 sidecar JSON
``<视频路径>.vpvidx.json``，按 ``size + mtime_ns`` 校验失效——
命中则秒开（25 分钟 720p 实测扫描秒级 → 毫秒级），未命中才扫描。

seek 用途：目标时间 → 索引 floor 定位 → ``container.seek(
ts_units, stream=video, backward=False)`` 直接落关键帧（mp4 的
demuxer 内建 stss/stco 表按时间定位为 O(log n)），替代「backward
线性回搜 + 逐帧解码」的盲找路径；byte_offset 保留在表中供支持字节
seek 的容器格式走直跳（与诊断定位）。
"""

from __future__ import annotations

import bisect
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("renpy_overlay.video.player.keyframe_index")

#: sidecar 缓存格式版本：结构不兼容时弃用重建
INDEX_VERSION = 1


@dataclass(frozen=True)
class KeyframeEntry:
    """单个关键帧：pts 为流时基单位，offset 为包起始字节偏移。"""

    pts_units: int
    offset: int


class KeyframeIndex:
    """关键帧有序索引（按 pts 升序；bisect floor 查找 O(log n)）。"""

    def __init__(self, entries: list[KeyframeEntry], time_base: float) -> None:
        self.entries = sorted(entries, key=lambda e: e.pts_units)
        self.time_base = float(time_base) if time_base else 0.0

    def __len__(self) -> int:
        return len(self.entries)

    def pts_to_ms(self, pts_units: int) -> int:
        if not self.time_base:
            return 0
        return max(0, int(pts_units * self.time_base * 1000))

    def ms_to_pts(self, target_ms: int) -> int:
        if not self.time_base:
            return 0
        return int(target_ms / 1000.0 / self.time_base)

    def lookup_floor_ms(self, target_ms: int) -> KeyframeEntry | None:
        """目标时间之前（含）的最近关键帧；空索引或早于首帧返回首帧/None。"""
        return self.lookup_floor_units(self.ms_to_pts(target_ms))

    def lookup_floor_units(self, target_units: int) -> KeyframeEntry | None:
        if not self.entries:
            return None
        keys = [e.pts_units for e in self.entries]
        i = bisect.bisect_right(keys, target_units) - 1
        if i < 0:
            i = 0
        return self.entries[i]


def build_index(container, video_stream) -> KeyframeIndex:
    """全量 demux 视频流构建关键帧索引（worker 线程执行；缺 pts 的包跳过）。"""
    time_base = float(video_stream.time_base) if video_stream.time_base else 0.0
    entries: list[KeyframeEntry] = []
    for packet in container.demux(video_stream):
        if not packet.is_keyframe:
            continue
        pts = packet.pts
        if pts is None:
            continue
        entries.append(KeyframeEntry(int(pts), int(packet.pos or 0)))
    logger.info("关键帧索引扫描完成：%d 个关键帧（time_base=%s）", len(entries), time_base)
    return KeyframeIndex(entries, time_base)


def sidecar_path(video_path: str | Path) -> Path:
    """sidecar 缓存文件路径：``<视频路径>.vpvidx.json``。"""
    video = Path(video_path)
    return video.with_suffix(video.suffix + ".vpvidx.json")


def _file_stamp(video_path: str | Path) -> dict:
    stat = os.stat(video_path)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def save(video_path: str | Path, index: KeyframeIndex) -> bool:
    """索引持久化（失败仅告警——缓存缺失只影响下次打开速度）。"""
    try:
        payload = {
            "version": INDEX_VERSION,
            "video": str(video_path),
            "time_base": index.time_base,
            "stamp": _file_stamp(video_path),
            "entries": [[e.pts_units, e.offset] for e in index.entries],
        }
        sidecar = sidecar_path(video_path)
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("关键帧索引已缓存：%s", sidecar)
        return True
    except OSError as exc:
        logger.warning("关键帧索引缓存写入失败：%s", exc)
        return False


def load_cached(video_path: str | Path) -> KeyframeIndex | None:
    """加载 sidecar 索引；不存在/过期/损坏返回 None（调用方回退扫描）。

    失效判据仅 stamp（size + mtime_ns）：同内容文件无论以相对/绝对
    路径打开均命中；payload 里的 video 字段仅作诊断记录不参与校验。
    """
    sidecar = sidecar_path(video_path)
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if payload.get("version") != INDEX_VERSION:
            return None
        stamp = payload.get("stamp") or {}
        current = _file_stamp(video_path)
        if stamp.get("size") != current["size"] or stamp.get("mtime_ns") != current["mtime_ns"]:
            logger.info("关键帧索引缓存过期（文件已变更）：%s", sidecar)
            return None
        entries = [KeyframeEntry(int(p), int(o)) for p, o in payload.get("entries", [])]
        if not entries:
            return None
        index = KeyframeIndex(entries, float(payload.get("time_base") or 0.0))
        logger.info("关键帧索引缓存命中：%d 条（%s）", len(index), sidecar.name)
        return index
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("关键帧索引缓存读取失败：%s", exc)
        return None
