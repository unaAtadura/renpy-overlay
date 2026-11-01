"""应用导出：按播放序列拼接片段 → 原参数新文件（「应用」按钮）。

导出路径（用户在导出弹窗中二选一，按「仅删除 + 拼接」语义最大化无损）：

- **未编辑**（片段序列 = 完整源视频，含「分割未删/撤销回原状」的无洞
  多段形态）→ packet 级流拷贝（``_remux_passthrough``），零重编码；
- **关键帧吸附**（用户选择 snap，关键帧索引可用）→ 片段起点吸附到
  最近关键帧后全部流拷贝拼接（``_build_snap_plan``）：零重编码损耗，
  边界偏差 ≤半个 GOP，终点帧精确；
- **全重编码**（默认 / 吸附不可用回退）→ 逐段解码重编码拼接，
  ``crf`` 可由用户自配置（18-28）。

历史结论（实测）：copy 段与重编码段混流（三明治切分）不可行——
mp4 单流 avcC 唯一导致重编码段无法解码（``Invalid data``），已由
用户二选一规避。

时间戳策略：统一「输出毫秒」真值——重编码帧按帧序推进、copy 包按源
pts 换算，两侧同一换算函数，衔接误差 ≤1 tick；跨段累计连续（实测结论：
``pts=None`` 自动时间戳不可靠、``time_base`` 不可赋 None、AVRational
不支持 int()，均见 git 历史）。

流程（future_*.txt 应用章节）：二次弹窗确认（UI 侧）→「原名new.扩展名」
同目录落盘；确认后一律执行并清空命令流（UI 侧）。``should_abort`` 供
后台 worker 响应中断（抛 :class:`ExportAborted`，半成品由调用方清理）。

确定性说明：重编码路径 x264 默认多线程帧级并行（threads=auto），同输入
两次导出非 bit-exact（体积波动实测 <5%）——编码器固有行为；未编辑场景
经短路直通天然 bit 级等价。
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .model import TrackClip

logger = logging.getLogger("renpy_overlay.video.clip.exporter")

#: 源视频编码名 → 导出编码器（未收录编码回退 libx264 并告警）
_VIDEO_ENCODERS = {
    "h264": "libx264",
    "hevc": "libx265",
    "h265": "libx265",
    "mpeg4": "mpeg4",
    "mjpeg": "mjpeg",
    "vp9": "libvpx-vp9",
}
DEFAULT_VIDEO_ENCODER = "libx264"

#: AAC 48k 立体声导出的典型码率（bps）：导出音频编码参数（决策 3）的估算面
AAC_DEFAULT_BITRATE = 128_000

#: 直通/估算的片尾容差（毫秒）：源时长元数据与片段终点的帧取整误差
TAIL_TOLERANCE_MS = 1_000


class ExportAborted(RuntimeError):
    """导出被用户中断（worker 捕获后清理半成品）。"""


def default_export_path(source_path: str) -> Path:
    """「原名new.mp4」，与原文件同目录；导出统一 MP4（历史结论：mkv
    封装在本链路下音画兼容性问题多发，统一 mp4 规避，源扩展名不再
    透传——即使源为 mkv/avi/wmv 也输出 mp4）。"""
    src = Path(source_path)
    return src.with_name(f"{src.stem}new.mp4")


def _as_fraction(rate) -> Fraction | None:
    """帧率/time_base → Fraction（AVRational 无 __int__，float 又被
    to_avrational 拒收——Fraction(numerator, denominator) 唯一无损通路）。"""
    if rate is None:
        return None
    try:
        return Fraction(rate.numerator, rate.denominator)
    except AttributeError:  # int/Fraction 形态兜底
        return Fraction(rate)


def _source_duration_ms(src_path: str) -> int:
    """源容器时长（毫秒）；读取失败返回 0。"""
    try:
        import av

        with av.open(src_path) as container:
            return int((container.duration or 0) / 1000)
    except Exception:
        return 0


@dataclass(frozen=True)
class SizeEstimate:
    """导出大小估算结果：字节数 + 估算方法描述（日志可读）。"""

    bytes_estimate: int
    method: str


def estimate_export_size(clips: list[TrackClip]) -> tuple[SizeEstimate | None, str]:
    """估算导出文件大小：码率法 → 容器平均比法，逐级回退。

    返回 ``(SizeEstimate | None, 原因)``——预估不可用时不抛异常（需求 4：
    不阻塞导出），原因供 WARNING 日志；可用时为 ``(estimate, "")``。

    - 未编辑/无洞直通：源文件大小精确值（无估算误差）；
    - 码率法：视频流码率（缺失时从容器的扣除音频部分）+ aac 典型码率
      ×片段总时长；
    - 平均比法：源文件大小/源时长 ×片段总时长（不区分音视频，粗估）。
    """
    if not clips:
        return None, "无导出片段"
    sources = {c.source_path for c in clips}
    if len(sources) > 1:
        return None, "混源片段暂不支持估算"
    total_ms = sum(c.duration_ms for c in clips)
    if total_ms <= 0:
        return None, "片段总时长为 0"
    src_path = sources.pop()
    # 直通（未编辑/无洞多段）：预估 = 源文件大小精确值（无重编码损耗）
    if is_passthrough(clips):
        try:
            size_bytes = Path(src_path).stat().st_size
            return (
                SizeEstimate(
                    size_bytes, "直通复制源流（无重编码损耗，预估精确）"
                ),
                "",
            )
        except OSError:
            pass  # 源大小读不到 → 走后面的码率/平均比路径
    try:
        import av

        with av.open(src_path) as container:
            src_duration_ms = (container.duration or 0) / 1000  # 微秒 → 毫秒
            size_bytes = Path(src_path).stat().st_size
            v_in = container.streams.video[0]
            a_in = (
                container.streams.audio[0] if container.streams.audio else None
            )
            v_bitrate = v_in.codec_context.bit_rate or 0
            a_bitrate = (
                (a_in.codec_context.bit_rate or 0)
                if a_in is not None
                else 0
            )
            container_bitrate = container.bit_rate or 0
    except Exception as exc:  # 元数据读取失败：预估不可用但不阻塞
        return None, f"源元数据读取失败（{exc}）"

    audio_bitrate = AAC_DEFAULT_BITRATE if a_in is not None else 0
    video_bitrate = v_bitrate
    method = "视频流码率"
    if video_bitrate <= 0 and container_bitrate > 0:
        fallback_audio = (a_bitrate or AAC_DEFAULT_BITRATE) if a_in is not None else 0
        video_bitrate = max(0, container_bitrate - fallback_audio)
        method = "容器码率"
    if video_bitrate > 0:
        total_bitrate = video_bitrate + audio_bitrate
        bytes_estimate = int(total_bitrate * total_ms / 8000)
        return (
            SizeEstimate(
                bytes_estimate,
                f"{method} {video_bitrate / 1000:.0f}kbps 视频"
                + (f" + {audio_bitrate / 1000:.0f}kbps 音频" if audio_bitrate else ""),
            ),
            "",
        )
    if src_duration_ms > 0 and size_bytes > 0:
        bytes_estimate = int(size_bytes * total_ms / src_duration_ms)
        return (
            SizeEstimate(
                bytes_estimate,
                f"容器平均比（{size_bytes / 1_048_576:.1f}MB / {src_duration_ms / 1000:.1f}s）",
            ),
            "",
        )
    return None, "缺少码率与时长元数据（无法估算）"


def is_passthrough(clips: list[TrackClip], tolerance_ms: int = TAIL_TOLERANCE_MS) -> bool:
    """是否可整文件直通（stream copy，不重编码）。

    两种等价形态（模型态判定，与关键帧无关）：
    - 单片段从 0 覆盖到片尾（完全未编辑）；
    - 多片段按序列首尾相接、源区间无缝且整体覆盖全片
      （「分割未删」「删除后撤销/合并回原状」等）。

    容差 ``tolerance_ms``：源时长元数据与片段终点的毫秒级误差（帧取整）。
    """
    if not clips:
        return False
    ordered = sorted(clips, key=lambda c: c.timeline_start_ms)
    if ordered[0].source_start_ms != 0:
        return False
    try:
        src_ms = _source_duration_ms(ordered[0].source_path)
    except Exception:
        return False
    if src_ms <= 0:
        return False
    if ordered[-1].source_end_ms < src_ms - tolerance_ms:
        return False
    prev = ordered[0]
    for clip in ordered[1:]:
        if clip.source_path != prev.source_path:
            return False
        if clip.source_start_ms != prev.source_end_ms:
            return False  # 序列存在源区间空洞/重叠：不能整文件直通
        prev = clip
    return True


def _remux_passthrough(
    src_path: str, out_path: str | Path, progress=None
) -> Path:
    """packet 级流拷贝（零重编码、零质量损失）：直通场景的短路路径。

    moov 置于头部（faststart，与源录制习惯一致）。进度按包粒度上报
    （同 emit 容错约定）。
    """
    import av

    def emit(note: str) -> None:
        if progress is None:
            return
        try:
            progress(0, 0, note)
        except Exception:
            logger.debug("导出进度回调异常（忽略）", exc_info=True)

    with av.open(src_path) as src:
        out = av.open(str(out_path), "w", options={"movflags": "faststart"})
        try:
            stream_map = {}
            for in_stream in src.streams:
                stream_map[in_stream] = out.add_stream_from_template(in_stream)
            packets = 0
            for packet in src.demux():
                if packet.dts is None:
                    continue
                out_stream = stream_map.get(packet.stream)
                if out_stream is None:
                    continue
                packet.stream = out_stream
                out.mux(packet)
                packets += 1
                if progress is not None and packets % 1024 == 0:
                    emit(f"已复制 {packets} 个包（流拷贝，无重编码）")
            emit(f"流拷贝完成，共 {packets} 个包")
        finally:
            out.close()
    logger.info("直通短路：源流直接复制（无重编码损耗）→ %s", out_path)
    return Path(out_path)


# ---------------------------------------------------------------- 混合切割（三明治）


@dataclass(frozen=True)
class _CutSegment:
    """混合/吸附导出的执行计划段：copy（packet 流拷贝）或 encode（帧精确重编码）。

    ``source_start_ms``/``source_end_ms`` 为与 :class:`TrackClip` 对齐的
    属性别名（音频处理函数对两者 duck-type 复用）。"""

    kind: str  # "copy" | "encode"
    src_start_ms: int
    src_end_ms: int
    timeline_start_ms: int

    @property
    def source_start_ms(self) -> int:
        return self.src_start_ms

    @property
    def source_end_ms(self) -> int:
        return self.src_end_ms


def _load_keyframe_ms(src_path: str) -> list[int] | None:
    """关键帧毫秒列表（sidecar 命中优先，未命中全量扫描并写缓存）。

    不可用（无视频流/读取失败/空索引）返回 ``None`` → 回退全重编码。
    """
    from ..player import keyframe_index as kfi

    index = kfi.load_cached(src_path)
    if index is None:
        try:
            import av

            with av.open(src_path) as container:
                if not container.streams.video:
                    return None
                index = kfi.build_index(container, container.streams.video[0])
        except Exception:
            logger.warning("关键帧索引构建失败，混合导出降级为全重编码", exc_info=True)
            return None
        if index is not None and len(index):
            kfi.save(src_path, index)
    if index is None or not len(index):
        return None
    return [index.pts_to_ms(e.pts_units) for e in index.entries]


def _build_cut_plan(
    clips: list[TrackClip], kf_ms: list[int]
) -> list[_CutSegment]:
    """三明治切分：边界窄条重编码（帧精确）+ 关键帧间主体无损复制。

    对片段 [S, E]：F = 首个 ≥S 的关键帧、K = E 之前最后一个关键帧
    （E 恰为关键帧时 K = E，尾部为空）→ [S,F) encode + [F,K) copy +
    [K,E] encode；段长不足一个 GOP（F ≥ K）退化为整段 encode。
    """
    plans: list[_CutSegment] = []
    for clip in sorted(clips, key=lambda c: c.timeline_start_ms):
        s_ms, e_ms, ts = (
            clip.source_start_ms,
            clip.source_end_ms,
            clip.timeline_start_ms,
        )
        i_f = bisect.bisect_left(kf_ms, s_ms)
        f_ms = kf_ms[i_f] if i_f < len(kf_ms) else None
        i_e = bisect.bisect_left(kf_ms, e_ms)
        if i_e < len(kf_ms) and kf_ms[i_e] == e_ms:
            k_ms = e_ms  # 终点恰为关键帧：copy 可精确到终点
        elif i_e > 0:
            k_ms = kf_ms[i_e - 1]
        else:
            k_ms = None
        if f_ms is None or k_ms is None or f_ms >= k_ms:
            plans.append(_CutSegment("encode", s_ms, e_ms, ts))
            continue
        if f_ms > s_ms:
            plans.append(_CutSegment("encode", s_ms, f_ms, ts))
        plans.append(_CutSegment("copy", f_ms, k_ms, ts + (f_ms - s_ms)))
        if k_ms < e_ms:
            plans.append(_CutSegment("encode", k_ms, e_ms, ts + (k_ms - s_ms)))
    return plans


def _build_snap_plan(
    clips: list[TrackClip], kf_ms: list[int]
) -> tuple[list[_CutSegment] | None, str]:
    """关键帧吸附规划：全部片段 packet 流拷贝（零重编码损耗）。

    吸附语义：片段起点吸附到「距 S 最近的关键帧」（floor/ceil 取近者，
    偏差 ≤半个 GOP）；终点帧精确（copy 到 pts < E 停，无需关键帧）。
    防重叠：候选起点被前段占用时推到前段终点之后的第一个关键帧；
    推后仍不可用（相邻片段间距过近）→ 规划失败，返回 ``(None, 原因)``，
    调用方回退全重编码。
    """
    plans: list[_CutSegment] = []
    cursor_src = 0  # 已覆盖到的源时间（前段终点）
    cursor_out = 0  # 输出时间轴游标（毫秒）
    for clip in sorted(clips, key=lambda c: c.timeline_start_ms):
        s_ms, e_ms = clip.source_start_ms, clip.source_end_ms
        if e_ms <= s_ms:
            return None, f"片段区间无效（{s_ms}-{e_ms}ms）"
        i = bisect.bisect_left(kf_ms, s_ms)
        candidates = []
        if i > 0:
            candidates.append(kf_ms[i - 1])  # floor
        if i < len(kf_ms):
            candidates.append(kf_ms[i])  # ceil
        usable = [k for k in candidates if k >= cursor_src]
        if usable:
            start = min(usable, key=lambda k: abs(k - s_ms))  # 最近关键帧
        else:
            # floor 被前段占用：取 cursor 后第一个关键帧
            j = bisect.bisect_left(kf_ms, cursor_src)
            if j >= len(kf_ms) or kf_ms[j] >= e_ms:
                return None, f"片段 [{s_ms},{e_ms})ms 无法无损吸附（相邻片段间距过近）"
            start = kf_ms[j]
        if start >= e_ms:
            return None, f"片段 [{s_ms},{e_ms})ms 吸附后为空"
        plans.append(
            _CutSegment("copy", start, e_ms, cursor_out)
        )
        cursor_out += e_ms - start
        cursor_src = e_ms
    return plans, ""


def _abort(should_abort) -> None:
    if should_abort is not None and should_abort():
        raise ExportAborted("导出被用户中断")


def export_clips(
    clips: list[TrackClip],
    out_path: str | Path,
    progress=None,
    should_abort=None,
    *,
    mode: str = "reencode",
    crf: int | None = None,
) -> Path:
    """按播放序列拼接片段写入 ``out_path``；返回输出路径。

    ``progress(done_units, total_units, note)`` 可选回调（worker 线程；
    单位为「帧当量」，copy 段按源毫秒折算，调用方自行节流）。回调与
    ``should_abort`` 均被隔离/即时响应：进度故障不中断导出，中断抛
    :class:`ExportAborted`（半成品文件由调用方清理）。

    ``mode``（用户在导出确认弹窗中二选一）：
    - ``"reencode"``（默认）：全重编码（帧精确）；``crf`` 提供时显式
      设置编码器 CRF（18-28 建议区间，数值越小质量越高体积越大）；
    - ``"snap"``：关键帧吸附——片段起点吸附到最近关键帧（偏差 ≤半个
      GOP），终点帧精确，全部片段 packet 流拷贝拼接（零重编码损耗）。
      关键帧索引不可用或存在无法无损吸附的片段时，自动回退全重编码
      （日志明示回退原因）。

    路径分发与确定性说明见模块 docstring。
    """
    import av

    if not clips:
        raise ValueError("导出片段为空")
    sources = {c.source_path for c in clips}
    if len(sources) > 1:
        raise ValueError("暂不支持混源导出")
    src_path = sources.pop()

    # 未编辑短路：片段序列 = 完整源视频（含无洞多段）→ 直接复制源流
    if is_passthrough(clips):
        logger.info("未检测到编辑（片段序列覆盖全片）：直接复制源流")
        return _remux_passthrough(src_path, out_path, progress)

    with av.open(src_path) as src:
        v_in = src.streams.video[0]
        a_in = src.streams.audio[0] if src.streams.audio else None
        width, height = v_in.codec_context.width, v_in.codec_context.height
        fps_frac = _as_fraction(v_in.average_rate) or _as_fraction(
            v_in.guessed_rate
        ) or Fraction(30)  # 流创建用 Fraction 保精度（29.97 等非整数帧率无损）
        pix_fmt = v_in.codec_context.pix_fmt or "yuv420p"
        codec_name = (v_in.codec_context.name or "h264").lower()
        encoder = _VIDEO_ENCODERS.get(codec_name, DEFAULT_VIDEO_ENCODER)
        if encoder == DEFAULT_VIDEO_ENCODER and codec_name not in _VIDEO_ENCODERS:
            logger.warning("未知源编码 %s，回退 %s", codec_name, encoder)
        # 预估总帧数（进度百分比/ETA 分母）
        total_units = max(
            1, int(sum(c.duration_ms for c in clips) * float(fps_frac) / 1000)
        )

        def emit(done_units: int, note: str = "") -> None:
            if progress is None:
                return
            try:
                progress(min(done_units, total_units), total_units, note)
            except Exception:
                logger.debug("导出进度回调异常（忽略）", exc_info=True)

        # 关键帧吸附模式：全片段流拷贝拼接（零重编码损耗；不可吸附时回退）
        if mode == "snap":
            kf_ms = _load_keyframe_ms(src_path)
            snap_plans: list[_CutSegment] | None = None
            snap_reason = "关键帧索引不可用"
            if kf_ms is not None:
                snap_plans, snap_reason = _build_snap_plan(clips, kf_ms)
            if not snap_plans:
                logger.warning("关键帧吸附不可用（%s），回退全重编码", snap_reason)
            else:
                logger.info(
                    "关键帧吸附导出：%d 段全流拷贝（边界吸附 ≤半个 GOP，终点帧精确）",
                    len(snap_plans),
                )
                out = av.open(str(out_path), "w")  # 格式随输出扩展名（mp4/mkv）
                try:
                    v_out = out.add_stream_from_template(v_in)
                    a_out = None
                    audio_copy = a_in is not None and (
                        (a_in.codec_context.name or "") == "aac"
                    )
                    if a_in is not None:
                        a_out = (
                            out.add_stream_from_template(a_in)
                            if audio_copy
                            else out.add_stream("aac", rate=48_000)
                        )
                        if not audio_copy:
                            a_out.layout = "stereo"
                    sample_counter = 0
                    for seg in snap_plans:
                        _abort(should_abort)
                        _copy_video_range(
                            src, seg, v_in, out, v_out, fps_frac, emit,
                            should_abort,
                        )
                    if a_in is not None:
                        # 音频遍历吸附段（seg 与 TrackClip 同字段，duck-type）：
                        # 与视频完全同区间同轴——吸附起点偏移若仍按模型原始
                        # timeline 重基音频，音画时间轴错位（实测：开头静音
                        # 直到吸附偏移累积点才恢复并伴随视频卡顿）
                        for seg in snap_plans:
                            _abort(should_abort)
                            if audio_copy:
                                sample_counter = _copy_audio_range(
                                    src, seg, a_in, a_out, out, emit,
                                    should_abort, sample_counter,
                                )
                            else:
                                sample_counter = _encode_audio_range(
                                    src, seg, a_in, a_out, out, emit,
                                    should_abort, sample_counter,
                                )
                        emit(sample_counter // 48, "音频处理完成")
                finally:
                    out.close()
                logger.info("吸附导出完成（零重编码损耗）→ %s", out_path)
                return Path(out_path)

        # 导出执行：全重编码（帧精确；无洞多段/未编辑已在上文直通）。
        # 混合方案（copy 段 + 重编码段混流）实测不可行：mp4 单视频流只有
        # 一份 avcC（SPS/PPS），重编码段参数与源不一致时帧无法解码
        # （avcodec_send_packet Invalid data）——已由用户在导出弹窗选择
        # 关键帧吸附（零代损，精度换）或重编码（帧精确）规避
        out = av.open(str(out_path), "w")
        try:
            v_out = out.add_stream(encoder, rate=fps_frac)
            v_out.width = width
            v_out.height = height
            v_out.pix_fmt = pix_fmt if encoder != "libx264" else "yuv420p"
            v_out.gop_size = 60  # GOP ≤2s：产物在播放器/本编辑器中跳转快启
            # （x264 默认 keyint=250 + scenecut 会产生 8s+ 的稀疏区间，
            # 跳转落入后需顺序解码数百帧才能出画，实测卡顿）
            if crf is not None:
                # 用户自配置 CRF（18-28）：显式设置后覆盖 x264 默认 CRF23，
                # 控制本次重编码的质量与体积（经 codec_context.options 通路，
                # 与 bf 实测同一条已验证的生效路径）
                v_out.codec_context.options = {"crf": str(int(crf))}
            a_out = None
            if a_in is not None:
                a_out = out.add_stream("aac", rate=48_000)
                a_out.layout = "stereo"

            frame_counter = 0  # 跨段累计帧序（pts 单位 1/fps，拼接连续）
            sample_counter = 0  # 音频重编码段累计样本序（1/48000）
            for clip in sorted(clips, key=lambda c: c.timeline_start_ms):
                _abort(should_abort)
                frame_counter, sample_counter = _reencode_clip(
                    src, clip, out, v_out, a_out, fps_frac, emit, should_abort,
                    frame_counter, sample_counter,
                )
                emit(
                    frame_counter,
                    f"已完成片段（源区间 {clip.source_start_ms}-{clip.source_end_ms}ms）",
                )
            # flush 只能整次导出做一次：编码器 flush 后进入 EOF 态，
            # 再 send_frame 会报 AVERROR_EOF（实测）
            for packet in v_out.encode(None):
                out.mux(packet)
            if a_out is not None:
                for packet in a_out.encode(None):
                    out.mux(packet)
        finally:
            out.close()
    logger.info("导出完成：%s（%d 片段）", out_path, len(clips))
    return Path(out_path)


def _reencode_clip(
    src, clip: TrackClip, out, v_out, a_out, fps_frac: Fraction, emit,
    should_abort, frame_counter: int, sample_counter: int,
) -> tuple[int, int]:
    """单片段重编码：视频 seek → 解码（pts 裁剪）→ 帧序时间戳 → 编码；
    音频重采样 48k 立体声 → 样本序时间戳 → aac。

    时间戳策略（实测结论）：``pts=None`` 时 PyAV 并不会可靠地自动打时间
    戳（重复 pts 帧被编码器丢弃，输出只剩 1 帧），``time_base`` 亦不可赋
    None——故显式按帧序/样本序打时间戳（time_base 用 Fraction），
    frame_counter/sample_counter 跨片段累计保证拼接时间戳连续。
    返回更新后的累计计数器。"""
    import av

    # 帧时基 = 精确帧率的倒数（23.976fps → 1001/24000）：round 到整数
    # fps（1/24）会与 muxer 时基（1001/24000）非整除，编码器输出 dts
    # rescale 舍入碰撞（相邻包 dts 相等 → mp4 拒绝，实测 EINVAL）；
    # 精确倒数与 muxer tb 恒等，帧距整除无舍入
    frame_time_base = Fraction(1) / (_as_fraction(fps_frac) or Fraction(30))
    target_fmt = v_out.pix_fmt or "yuv420p"
    s_ms, e_ms = clip.source_start_ms, clip.source_end_ms
    start_s = s_ms / 1000.0
    v_in = src.streams.video[0]
    src.seek(int(start_s / float(v_in.time_base)), stream=v_in, backward=True)
    for frame in src.decode(v_in):
        _abort(should_abort)
        if frame.time is not None and frame.time < start_s - 0.001:
            continue
        src_ms = frame.time * 1000 if frame.time is not None else None
        if src_ms is not None and src_ms >= e_ms:
            break
        if frame.format.name != target_fmt:
            # 10bit 源（Hi10P mkv）等帧格式与编码器目标不一致：PyAV 不会
            # 自动转换，10bit 帧直喂 8bit x264 产生坏包 → mux EINVAL（实测）
            frame = frame.reformat(format=target_fmt)
        frame.pts = frame_counter
        frame.time_base = frame_time_base
        frame_counter += 1
        for packet in v_out.encode(frame):
            out.mux(packet)
        emit(frame_counter)
    # flush 由 export_clips 在全部片段结束后统一做一次
    if a_out is None:
        return frame_counter, sample_counter
    # 音频：重采样 48k 立体声 → 样本序时间戳 → aac
    a_in = src.streams.audio[0]
    resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48_000)
    src.seek(int(start_s / float(a_in.time_base)), stream=a_in, backward=True)
    for frame in src.decode(a_in):
        _abort(should_abort)
        if frame.time is not None and frame.time < start_s - 0.001:
            continue
        src_ms = frame.time * 1000 if frame.time is not None else None
        if src_ms is not None and src_ms >= e_ms:
            break
        for piece in resampler.resample(frame):
            piece.pts = sample_counter
            piece.time_base = Fraction(1, 48_000)
            sample_counter += piece.samples
            for packet in a_out.encode(piece):
                out.mux(packet)
    return frame_counter, sample_counter


def _pts_units(out_ms: int, time_base: Fraction) -> int:
    """输出毫秒 → 指定时基的 pts 数值（同一换算函数保证衔接一致）。"""
    return int(round(out_ms / 1000.0 / float(time_base)))


def _copy_video_range(
    src, seg: _CutSegment, v_in, out, v_out, fps_frac: Fraction,
    emit, should_abort,
) -> None:
    """视频子区间 packet 级流拷贝：起点吸附关键帧，终点帧精确。

    时间戳策略（实测修正）：pts 按段起点归零（首帧 pts=ts，否则首帧
    pts 会带上 B 帧延迟偏移，实测产物首帧 pts 漂移至源 8.5s 处导致
    播放器开头静止 8.5s）；dts 保包级 pts-dts 差并以 max(自身, 前包+1)
    全局单调锥制（B 帧 dts<pts 合法；锥制仅在源 dts 异常回退时介入，
    dts 静默追近 pts 只影响个别包的解码缓冲，呈现序不变）。
    """
    src_tb = _as_fraction(v_in.time_base)
    out_tb = _as_fraction(v_out.time_base) or Fraction(1, 30)
    s_ms, e_ms, ts = (
        seg.src_start_ms, seg.src_end_ms, seg.timeline_start_ms,
    )
    copied = 0
    dts_zero_ms = None  # 段内公共零点 = 段内最小 dts（通常为首包，可为负）；
    # pts 与 dts 都减去该零点：整体平移使 dts ≥ 0（mp4 拒负 dts，实测
    # EINVAL）且每包 pts-dts 差保持（B 帧重排语义不变）
    last_dts_out = None  # 已 mux 包的最大 dts（单调锥制下限，防御性）
    src.seek(int(s_ms / 1000.0 / float(src_tb)), stream=v_in)
    for packet in src.demux(v_in):
        _abort(should_abort)
        if packet.pts is None or packet.dts is None:
            continue
        pts_ms = packet.pts * float(src_tb) * 1000.0
        dts_ms = packet.dts * float(src_tb) * 1000.0
        if pts_ms < s_ms - 1:
            continue  # seek 提前量（关键帧对齐时通常为 0）
        if pts_ms >= e_ms:
            break
        if dts_zero_ms is None:
            dts_zero_ms = dts_ms  # demux 序单调 → 首包 dts 即段内最小
        pts_out = _pts_units(ts + (pts_ms - dts_zero_ms), out_tb)
        dts_out = _pts_units(ts + (dts_ms - dts_zero_ms), out_tb)
        if last_dts_out is not None and dts_out <= last_dts_out:
            dts_out = last_dts_out + 1  # 防御性单调（源 dts 已保证，仅兑底）
        last_dts_out = dts_out
        packet.pts = pts_out
        packet.dts = dts_out
        packet.time_base = out_tb
        packet.stream = v_out
        out.mux(packet)
        copied += 1
        if copied % 512 == 0:
            # 进度帧当量：copy 段与 encode 段统一折算（与 total_units 同口径）
            done_equiv = int((ts + (pts_ms - dts_zero_ms)) * float(fps_frac) / 1000)
            emit(done_equiv, f"已复制 {copied} 包（流拷贝）")


def _copy_audio_range(
    src, clip: TrackClip, a_in, a_out, out, emit, should_abort,
    sample_counter: int,
) -> int:
    """音频片段 packet 级直拷（源 AAC；AAC 帧粒度 ±21ms 边界，无二次编码）。

    起点跳过 pts < 片段起点的帧（避免与上一片段输出重叠破坏单调性，
    内容缺口 ≤1 个 AAC 帧约 21ms）；终点止于 pts ≥ 片段终点前。
    """
    src_tb = _as_fraction(a_in.time_base)
    out_tb = _as_fraction(a_out.time_base) or Fraction(1, 48_000)
    s_ms, e_ms, ts = (
        clip.source_start_ms, clip.source_end_ms, clip.timeline_start_ms,
    )
    src.seek(int(s_ms / 1000.0 / float(src_tb)), stream=a_in)
    for packet in src.demux(a_in):
        _abort(should_abort)
        pts = packet.pts
        if pts is None:
            continue
        pts_ms = pts * float(src_tb) * 1000.0
        if pts_ms < s_ms:
            continue  # 起点 AAC 帧粒度让位：不与上一片段重叠
        if pts_ms >= e_ms:
            break
        base_ms = ts + (pts_ms - s_ms)
        pts_out = _pts_units(base_ms, out_tb)
        packet.pts = pts_out
        packet.dts = pts_out if packet.dts is not None else None
        packet.time_base = out_tb
        packet.stream = a_out
        out.mux(packet)
        sample_counter += 1024  # AAC 帧样本数（仅用于进度当量）
        if sample_counter % 4800 == 0:
            emit(sample_counter // 48)
    return sample_counter


def _encode_audio_range(
    src, clip: TrackClip, a_in, a_out, out, emit, should_abort,
    sample_counter: int,
) -> int:
    """音频片段重编码：重采样 48k 立体声 → 样本序时间戳 → aac。"""
    import av

    resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48_000)
    s_ms, e_ms = clip.source_start_ms, clip.source_end_ms
    start_s = s_ms / 1000.0
    src.seek(int(start_s / float(a_in.time_base)), stream=a_in, backward=True)
    for frame in src.decode(a_in):
        _abort(should_abort)
        if frame.time is not None and frame.time < start_s - 0.001:
            continue
        src_ms = frame.time * 1000 if frame.time is not None else None
        if src_ms is not None and src_ms >= e_ms:
            break
        for piece in resampler.resample(frame):
            piece.pts = sample_counter
            piece.time_base = Fraction(1, 48_000)
            sample_counter += piece.samples
            for packet in a_out.encode(piece):
                out.mux(packet)
    return sample_counter
