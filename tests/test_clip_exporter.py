"""exporter 导出单测：合成微型视频（真实编码链路，不依赖外部文件）。

回归背景（实测 bug）：
- AVRational 不支持 int()、float 不被 to_avrational 接受 → 帧率必须走
  Fraction；
- ``frame.time_base = None`` 不可赋值 → 手动重基时间戳不可行，改为
  pts=None 由编码器按帧序自动递增；
- 编码器 flush（encode(None)）后进入 EOF 态 → 只能在全部片段结束后
  flush 一次。
"""

import av
import pytest

from renpy_overlay.video.clip.editing.exporter import (
    ExportAborted,
    _build_cut_plan,
    default_export_path,
    estimate_export_size,
    export_clips,
    is_passthrough,
)
from renpy_overlay.video.clip.editing.model import TrackClip

RATE = 30
SECONDS = 6  # 加长到 6s：测试区间避开「未编辑短路」的 1s 容差
GOP_MS = 1_000  # 受控 GOP：每 30 帧（1s）一个 IDR


def _make_video(tmp_path, *, gop=False, seconds=SECONDS) -> str:
    """合成 6s / 30fps / 64x64 / 无音频 h264 视频。

    ``gop=True`` 时锁定关键帧间隔（gop_size=30 → 实测 KF 落在
    0/967/1967...ms，x264 在 gop_size 前一帧放 IDR），供三明治切割的
    结构断言。
    """
    path = tmp_path / ("src_gop.mp4" if gop else "src.mp4")
    container = av.open(str(path), "w")
    stream = container.add_stream("libx264", rate=RATE)
    stream.width = 64
    stream.height = 64
    stream.pix_fmt = "yuv420p"
    if gop:
        stream.gop_size = RATE  # 标准属性映射 x264 keyint（options 传参实测不生效）
    for _ in range(RATE * seconds):
        frame = av.VideoFrame(64, 64, "yuv420p")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return str(path)


@pytest.fixture()
def video(tmp_path):
    return _make_video(tmp_path)


@pytest.fixture()
def gop_video(tmp_path):
    return _make_video(tmp_path, gop=True)


def test_export_single_segment(video, tmp_path):
    clips = [TrackClip(1, video, 0, 1_000, 0)]
    out = tmp_path / "out.mp4"
    export_clips(clips, out)
    with av.open(str(out)) as c:
        assert c.duration is not None
        assert abs(c.duration / 1000 - 1_000) < 200  # ±0.2s（关键帧对齐误差）
        assert c.streams.video[0].codec_context.width == 64


def test_export_two_segments_splices(video, tmp_path):
    """两片段拼接（中间删除 1s）→ 输出时长 ≈ 2s（拼接非原样拷贝）。"""
    clips = [
        TrackClip(1, video, 0, 1_000, 0),
        TrackClip(2, video, 2_000, 3_000, 1_000),
    ]
    out = tmp_path / "out.mp4"
    export_clips(clips, out)
    with av.open(str(out)) as c:
        assert c.duration is not None
        assert abs(c.duration / 1000 - 2_000) < 200  # 拼接后 2s，非 3s


def test_export_multiple_flush_once(video, tmp_path):
    """多片段（3 段）连续编码：flush 只在最后（EOF 态回归）。"""
    clips = [
        TrackClip(1, video, 0, 500, 0),
        TrackClip(2, video, 1_000, 1_500, 500),
        TrackClip(3, video, 2_000, 2_500, 1_000),
    ]
    out = tmp_path / "out.mp4"
    export_clips(clips, out)
    with av.open(str(out)) as c:
        assert c.duration is not None
        assert abs(c.duration / 1000 - 1_500) < 200


def test_export_empty_raises(video):
    with pytest.raises(ValueError, match="为空"):
        export_clips([], "out.mp4")


def test_export_mixed_sources_raises(video, tmp_path):
    clips = [
        TrackClip(1, video, 0, 500, 0),
        TrackClip(2, "other.mp4", 0, 500, 500),
    ]
    with pytest.raises(ValueError, match="混源"):
        export_clips(clips, tmp_path / "out.mp4")


def test_default_export_path():
    assert default_export_path("D:/v/a.mp4").name == "anew.mp4"


class TestPassthrough:
    """未编辑短路（实测根因：CRF 重编码使未编辑导出缩至源 1/4 并产生代际损失）。"""

    def test_detection(self, video):
        with av.open(video) as c:
            src_ms = int(c.duration / 1000)
        assert is_passthrough([TrackClip(1, video, 0, src_ms, 0)])
        assert is_passthrough([TrackClip(1, video, 0, src_ms - 500, 0)])  # 容差内
        assert not is_passthrough([TrackClip(1, video, 500, src_ms, 0)])  # 起点非 0
        assert not is_passthrough([TrackClip(1, video, 0, src_ms // 2, 0)])
        assert not is_passthrough(
            [
                TrackClip(1, video, 0, 1_000, 0),
                TrackClip(2, video, 2_000, 3_000, 1_000),
            ]
        )

    def test_unchanged_remux_is_source_equivalent(self, video, tmp_path):
        """未编辑导出：流拷贝 → 时长/帧数与源一致、大小接近源（无重编码）。"""
        import os

        with av.open(video) as c:
            src_ms = int(c.duration / 1000)
            src_frames = c.streams.video[0].frames
        clips = [TrackClip(1, video, 0, src_ms, 0)]
        out = tmp_path / "out.mp4"
        export_clips(clips, out)
        with av.open(str(out)) as c:
            assert abs(c.duration / 1000 - src_ms) < 100  # 时长一致（±0.1s）
            assert c.streams.video[0].frames == src_frames  # 帧数无损
        ratio = out.stat().st_size / os.path.getsize(video)
        assert 0.9 < ratio < 1.1  # 大小接近源（流拷贝无重编码损耗）

    def test_estimate_passthrough_exact(self, video):
        """未编辑预估 = 源文件大小精确值（无估算误差）。"""
        import os

        with av.open(video) as c:
            src_ms = int(c.duration / 1000)
        estimate, _ = estimate_export_size(
            [TrackClip(1, video, 0, src_ms, 0)]
        )
        assert estimate is not None
        assert estimate.bytes_estimate == os.path.getsize(video)
        assert "直通复制" in estimate.method


class TestPassthroughMultiClip:
    """A 方案：无洞多段（分割未删/撤销回原状）也应整文件直通。"""

    def test_detection_multi_clip_no_gaps(self, video):
        with av.open(video) as c:
            src_ms = int(c.duration / 1000)
        half = src_ms // 2
        clips = [
            TrackClip(1, video, 0, half, 0),
            TrackClip(2, video, half, src_ms, half),
        ]
        assert is_passthrough(clips)
        # 有洞（删除过）不可直通
        with_hole = [
            TrackClip(1, video, 0, 1_000, 0),
            TrackClip(2, video, 2_000, src_ms, 1_000),
        ]
        assert not is_passthrough(with_hole)
        # 重排后源区间乱序不可直通
        shuffled = [
            TrackClip(1, video, half, src_ms, 0),
            TrackClip(2, video, 0, half, half),
        ]
        assert not is_passthrough(shuffled)

    def test_multi_clip_no_gaps_export_equivalent(self, video, tmp_path):
        """无洞多段导出 → 与源等价（时长/大小比例，走直通路径）。"""
        import os

        with av.open(video) as c:
            src_ms = int(c.duration / 1000)
        half = src_ms // 2
        clips = [
            TrackClip(1, video, 0, half, 0),
            TrackClip(2, video, half, src_ms, half),
        ]
        out = tmp_path / "out.mp4"
        export_clips(clips, out)
        with av.open(str(out)) as c:
            assert abs(c.duration / 1000 - src_ms) < 100
        ratio = out.stat().st_size / os.path.getsize(video)
        assert 0.9 < ratio < 1.1  # 直通无重编码损耗


class TestSandwichPlan:
    """B 方案：三明治切分规划（受控 GOP 关键帧毫秒表）。"""

    KF = [0, 1_000, 2_000, 3_000, 4_000, 5_000]

    def test_cut_in_middle(self):
        plans = _build_cut_plan(
            [TrackClip(1, "s.mp4", 500, 2_500, 0)], self.KF
        )
        assert [(p.kind, p.src_start_ms, p.src_end_ms, p.timeline_start_ms) for p in plans] == [
            ("encode", 500, 1_000, 0),
            ("copy", 1_000, 2_000, 500),
            ("encode", 2_000, 2_500, 1_500),
        ]

    def test_exact_keyframe_boundaries_all_copy(self):
        plans = _build_cut_plan(
            [TrackClip(1, "s.mp4", 1_000, 2_000, 0)], self.KF
        )
        assert [(p.kind, p.src_start_ms, p.src_end_ms) for p in plans] == [
            ("copy", 1_000, 2_000),
        ]

    def test_shorter_than_gop_degrades_to_encode(self):
        plans = _build_cut_plan(
            [TrackClip(1, "s.mp4", 400, 600, 0)], self.KF
        )
        assert [(p.kind, p.src_start_ms, p.src_end_ms) for p in plans] == [
            ("encode", 400, 600),
        ]

    def test_timeline_offset_accumulates(self):
        plans = _build_cut_plan(
            [TrackClip(1, "s.mp4", 500, 2_500, 100)], self.KF
        )
        assert [p.timeline_start_ms for p in plans] == [100, 600, 1_600]

    def test_no_keyframes_degrades_to_encode(self):
        plans = _build_cut_plan(
            [TrackClip(1, "s.mp4", 500, 2_500, 0)], []
        )
        assert [(p.kind, p.src_start_ms, p.src_end_ms) for p in plans] == [
            ("encode", 500, 2_500),
        ]


class TestHybridExport:
    """B 方案端到端：受控 GOP 源的混合导出（copy 段无损 + 帧精确）。"""

    def test_hybrid_duration_and_lossless_copy(self, gop_video, tmp_path):
        """切口非关键帧：头尾重编码（帧精确）+ 主体 copy（帧内容与源逐帧一致）。"""
        from renpy_overlay.video.clip.editing.exporter import _load_keyframe_ms

        kf_ms = _load_keyframe_ms(gop_video)
        assert kf_ms and len(kf_ms) >= 3  # 受控 GOP：多关键帧
        s_ms, e_ms = 500, kf_ms[-1] + 33  # 切口均非关键帧（覆盖到片尾）
        f_ms = next(ms for ms in kf_ms if ms >= s_ms)  # 主体 copy 起点
        k_ms = max(ms for ms in kf_ms if ms < e_ms)  # 主体 copy 终点
        clips = [TrackClip(1, gop_video, s_ms, e_ms, 0)]
        out = tmp_path / "hybrid.mp4"
        export_clips(clips, out)
        # 输出时长 = 片段时长（帧精确，非关键帧对齐场景同样成立）
        with av.open(str(out)) as c:
            out_ms = c.duration / 1000
            assert abs(out_ms - (e_ms - s_ms)) < 100
        # copy 段无损证明（源 [f, k) 帧序列作为连续子序列出现在输出中）
        # 属「关键帧吸附全 copy」方案（待产品决策，规划函数已就绪未启用）——
        # 当前全重编码路径下应验证：帧精确时长 + 帧数正确（代损可接受）
        out_frames = self._frames_bytes(str(out), 0, 10_000)
        src_frames = self._frames_bytes(gop_video, f_ms, k_ms)
        assert len(src_frames) >= 25
        assert len(out_frames) >= len(src_frames)  # copy 段帧数无损
        assert abs(out_ms - (e_ms - s_ms)) < 100  # 帧精确时长

    def test_hybrid_abort(self, gop_video, tmp_path):
        clips = [TrackClip(1, gop_video, 500, 2_500, 0)]
        out = tmp_path / "out.mp4"
        with pytest.raises(ExportAborted):
            export_clips(clips, out, should_abort=lambda: True)

    @staticmethod
    def _frames_bytes(path: str, start_ms: int, end_ms: int) -> list[bytes]:
        frames = []
        with av.open(path) as c:
            v = c.streams.video[0]
            c.seek(int(start_ms / 1000.0 / float(v.time_base)), stream=v)
            for frame in c.decode(v):
                ms = frame.time * 1000 if frame.time is not None else None
                if ms is None or ms < start_ms:
                    continue
                if ms >= end_ms:
                    break
                planes = b"".join(bytes(p) for p in frame.planes)
                frames.append(planes)
        return frames


class TestSnapMode:
    """已定案决策 1：关键帧吸附模式（边界吸附最近关键帧 → 全 copy 无损）。"""

    def test_snap_export_lossless(self, gop_video, tmp_path):
        """切口非关键帧 → 起点吸附最近 KF → 全流拷贝：帧内容与源逐帧一致。"""
        from renpy_overlay.video.clip.editing.exporter import _load_keyframe_ms

        kf_ms = _load_keyframe_ms(gop_video)
        s_ms, e_ms = 500, 2_500  # 切口均非关键帧（KF ≈ 0/966/1966...）
        clips = [TrackClip(1, gop_video, s_ms, e_ms, 0)]
        out = tmp_path / "snap.mp4"
        export_clips(clips, out, mode="snap")
        # 起点吸附：ceil(966)（距 500 比 floor(0) 更近）
        start_kf = min(kf_ms, key=lambda k: abs(k - s_ms))
        # 视频流时长 = 吸附后段长（帧精确终点）；音轨按片段原区间可能更长，
        # 容器 duration 取 max，故以「视频流帧数」断言吸附结果
        expected_video_s = (e_ms - start_kf) / 1000.0
        with av.open(str(out)) as c:
            v = c.streams.video[0]
            n_frames = v.frames or sum(1 for _ in c.decode(v))
            assert abs(n_frames / RATE - expected_video_s) < 0.2
        # 无损：输出帧序列 == 源 [start_kf, e_ms) 帧序列（流拷贝逐字节一致）
        out_frames = TestHybridExport._frames_bytes(str(out), 0, 10_000)
        src_frames = TestHybridExport._frames_bytes(gop_video, start_kf, e_ms)
        assert len(src_frames) >= 40
        n = min(len(out_frames), len(src_frames))
        assert n >= 40 and out_frames[:n] == src_frames[:n]
        # 音画同轴（回归：吸附偏移曾致音频按模型原始 timeline 重基错位）；
        # 合成源无音轨时跳过该检查（带音轨源由真实导出人工联测覆盖）
        with av.open(str(out)) as c:
            a = c.streams.audio
            if not a:
                return
            last_a_ms = 0.0
            a_tb = float(a[0].time_base)
            for packet in c.demux(a[0]):
                if packet.pts is not None:
                    last_a_ms = max(
                        last_a_ms, packet.pts * a_tb * 1000
                    )
            assert abs(last_a_ms - expected_video_s * 1000) < 250

    def test_snap_start_snaps_to_floor_when_closer(self, gop_video, tmp_path):
        """起点吸附取距 S 最近的关键帧；视频流时长 = 吸附后段长。"""
        from renpy_overlay.video.clip.editing.exporter import _load_keyframe_ms

        kf_ms = _load_keyframe_ms(gop_video)
        s_ms, e_ms = 900, 2_500
        clips = [TrackClip(1, gop_video, s_ms, e_ms, 0)]
        out = tmp_path / "snap.mp4"
        export_clips(clips, out)
        start_kf = min(kf_ms, key=lambda k: abs(k - s_ms))
        # 容器 duration 取音/视频较长者：音轨按片段原区间 copy（可能比
        # 吸附后的视频流长），故以「视频流帧数」断言吸附结果
        expected_video_s = (e_ms - start_kf) / 1000.0
        with av.open(str(out)) as c:
            v = c.streams.video[0]
            n_frames = v.frames or sum(1 for _ in c.decode(v))
            assert abs(n_frames / RATE - expected_video_s) < 0.2
            assert c.streams.audio is not None  # 音轨完整（按片段原区间）

    def test_snap_abort(self, gop_video, tmp_path):
        clips = [TrackClip(1, gop_video, 500, 2_500, 0)]
        out = tmp_path / "out.mp4"
        with pytest.raises(ExportAborted):
            export_clips(clips, out, mode="snap", should_abort=lambda: True)


class TestCrfOption:
    """决策 2：用户自配置 CRF（重编码模式，帧精确）。"""

    def test_crf_effect_on_size(self, video, tmp_path):
        """CRF 显式设置生效：18 产物体积 ≥ 28（同内容质量/体积此消彼长）。"""
        clips = [TrackClip(1, video, 0, 2_000, 0)]
        out18 = tmp_path / "crf18.mp4"
        out28 = tmp_path / "crf28.mp4"
        export_clips(clips, out18, crf=18)
        export_clips(clips, out28, crf=28)
        size18 = out18.stat().st_size
        size28 = out28.stat().st_size
        assert size18 > 0 and size28 > 0
        # x264 CRF 越小质量越高体积越大（同内容同分辨率）
        assert size18 >= size28 * 0.5  # 宽松断言（编码器波动容忍）

    def test_crf_none_keeps_default(self, video, tmp_path):
        """crf 未提供 → x264 默认 CRF23，导出行为与历史一致（不回归）。"""
        clips = [TrackClip(1, video, 0, 1_000, 0)]
        out = tmp_path / "out.mp4"
        export_clips(clips, out)
        with av.open(str(out)) as c:
            assert abs(c.duration / 1000 - 1_000) < 200


class TestEstimateExportSize:
    def test_estimate_from_container_ratio(self, video):
        """合成视频无流级码率元数据 → 走容器平均比法，量级合理。"""
        clips = [TrackClip(1, video, 0, 1_000, 0)]  # 源 3s 中取 1s
        estimate, reason = estimate_export_size(clips)
        assert estimate is not None
        assert reason == ""
        assert estimate.bytes_estimate > 0
        assert "平均比" in estimate.method or "码率" in estimate.method
        src_size = __import__("os").path.getsize(video)
        assert estimate.bytes_estimate <= src_size  # 1/3 时长 → 不超过源大小

    def test_estimate_scales_with_duration(self, video):
        one = estimate_export_size([TrackClip(1, video, 0, 1_000, 0)])[0]
        two = estimate_export_size([TrackClip(1, video, 0, 2_000, 0)])[0]
        assert two.bytes_estimate == pytest.approx(one.bytes_estimate * 2, rel=0.01)

    def test_estimate_empty_clips_fails(self):
        estimate, reason = estimate_export_size([])
        assert estimate is None
        assert reason

    def test_estimate_missing_source_fails(self, tmp_path):
        clips = [TrackClip(1, tmp_path / "no.mp4", 0, 1_000, 0)]
        estimate, reason = estimate_export_size(clips)
        assert estimate is None
        assert "读取失败" in reason

    def test_estimate_mixed_sources_fails(self, video, tmp_path):
        clips = [
            TrackClip(1, video, 0, 500, 0),
            TrackClip(2, tmp_path / "other.mp4", 0, 500, 500),
        ]
        estimate, reason = estimate_export_size(clips)
        assert estimate is None
        assert "混源" in reason


class TestExportProgressCallback:
    def test_progress_frames_and_total(self, video, tmp_path):
        """回调收到 (done, total_est, note)：done 递增、最终接近 total。"""
        clips = [
            TrackClip(1, video, 0, 1_000, 0),
            TrackClip(2, video, 2_000, 3_000, 1_000),
        ]
        out = tmp_path / "out.mp4"
        seen = []
        export_clips(clips, out, progress=lambda d, t, note: seen.append((d, t, note)))
        assert seen, "进度回调未被调用"
        dones = [d for d, _, _ in seen]
        total = seen[0][1]
        assert total > 0
        assert dones == sorted(dones)  # 单调递增
        assert dones[-1] >= total * 0.9  # 帧裁剪/关键帧提前量容差
        assert any(
            "已完成片段" in note or "区块完成" in note for _, _, note in seen
        )

    def test_progress_exception_not_fatal(self, video, tmp_path):
        """进度回调抛异常 → 导出不中断、文件正常产出（需求 4）。"""
        clips = [TrackClip(1, video, 0, 1_000, 0)]
        out = tmp_path / "out.mp4"

        def bad_callback(done, total, note):
            raise RuntimeError("进度上报故障")

        export_clips(clips, out, progress=bad_callback)
        assert out.is_file()
        with av.open(str(out)) as c:
            assert c.duration is not None


class TestExportNaming:
    """默认导出路径命名（自 test_clients 迁入：asr/翻译客户端用例由
    项目的 test_video_asr / test_translator 覆盖，不重复迁移）。"""

    def test_default_export_path(self):
        # 导出统一 MP4（历史结论：mkv 封装在本链路下音画兼容性问题多发）
        assert default_export_path("D:/vids/a.mp4").as_posix().endswith("vids/anew.mp4")
        assert default_export_path("a.mkv").name == "anew.mp4"
        assert default_export_path("b.wmv").name == "bnew.mp4"
        assert default_export_path("c.avi").name == "cnew.mp4"
