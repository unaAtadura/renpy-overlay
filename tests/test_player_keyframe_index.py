"""keyframe_index 单测：索引构建 / floor 查找 / sidecar 保存加载与失效。"""

from fractions import Fraction

from renpy_overlay.video.player.keyframe_index import (
    KeyframeEntry,
    KeyframeIndex,
    build_index,
    load_cached,
    save,
    sidecar_path,
)


class FakePacket:
    def __init__(self, pts, pos, is_keyframe):
        self.pts = pts
        self.pos = pos
        self.is_keyframe = is_keyframe


class FakeStream:
    time_base = Fraction(1, 1000)  # 1 unit = 1ms，便于换算断言


class FakeContainer:
    def __init__(self, packets):
        self._packets = packets

    def demux(self, *streams):
        return iter(self._packets)


class TestBuildIndex:
    def test_only_keyframes_with_pts(self):
        packets = [
            FakePacket(0, 100, True),
            FakePacket(500, 200, False),  # 非关键帧：跳过
            FakePacket(1000, 300, True),
            FakePacket(None, 400, True),  # 缺 pts：跳过
            FakePacket(2000, 500, True),
        ]
        index = build_index(FakeContainer(packets), FakeStream())
        assert len(index) == 3
        assert index.entries == [
            KeyframeEntry(0, 100),
            KeyframeEntry(1000, 300),
            KeyframeEntry(2000, 500),
        ]

    def test_lookup_floor(self):
        entries = [KeyframeEntry(0, 10), KeyframeEntry(1000, 20), KeyframeEntry(5000, 30)]
        index = KeyframeIndex(entries, 0.001)
        assert index.lookup_floor_ms(0) == entries[0]
        assert index.lookup_floor_ms(999) == entries[0]
        assert index.lookup_floor_ms(1000) == entries[1]
        assert index.lookup_floor_ms(4999) == entries[1]
        assert index.lookup_floor_ms(10**9) == entries[2]

    def test_lookup_empty(self):
        index = KeyframeIndex([], 0.001)
        assert index.lookup_floor_ms(100) is None

    def test_ms_conversion(self):
        index = KeyframeIndex([KeyframeEntry(0, 0)], 1 / 24.0)
        assert abs(index.ms_to_pts(1000) - 24) <= 1
        assert index.pts_to_ms(48) in (1999, 2000)  # int 截断边界


class TestSidecar:
    def test_save_load_roundtrip(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 128)
        index = KeyframeIndex([KeyframeEntry(0, 8), KeyframeEntry(1000, 64)], 0.001)
        assert save(video, index)
        assert sidecar_path(video).is_file()
        loaded = load_cached(video)
        assert loaded is not None
        assert loaded.entries == index.entries
        assert loaded.time_base == index.time_base

    def test_miss_returns_none(self, tmp_path):
        video = tmp_path / "none.mp4"
        video.write_bytes(b"x")
        assert load_cached(video) is None

    def test_invalidated_by_mtime(self, tmp_path):
        import os

        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        save(video, KeyframeIndex([KeyframeEntry(0, 0)], 0.001))
        # mtime 变更 → 缓存失效
        st = video.stat()
        os.utime(video, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        assert load_cached(video) is None

    def test_invalidated_by_size(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        save(video, KeyframeIndex([KeyframeEntry(0, 0)], 0.001))
        video.write_bytes(b"\x00" * 128)  # 尺寸变更 → 失效
        assert load_cached(video) is None

    def test_corrupted_json_returns_none(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        sidecar_path(video).write_text("{broken", encoding="utf-8")
        assert load_cached(video) is None

    def test_wrong_version_returns_none(self, tmp_path):
        import json

        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 64)
        save(video, KeyframeIndex([KeyframeEntry(0, 0)], 0.001))
        payload = json.loads(sidecar_path(video).read_text(encoding="utf-8"))
        payload["version"] = 999
        sidecar_path(video).write_text(json.dumps(payload), encoding="utf-8")
        assert load_cached(video) is None
