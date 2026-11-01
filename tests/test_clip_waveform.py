"""waveform 单测：分块峰值纯函数 + 真实 wav 解码链路 + sidecar 缓存。"""

import math
import struct

import pytest

from renpy_overlay.video.clip.waveform import (
    DEFAULT_BLOCK_MS,
    WaveformData,
    cached_waveform,
    extract_waveform,
)


def _make_wave_data(block_ms=10, count=100, peak=0.5) -> WaveformData:
    return WaveformData(
        duration_ms=count * block_ms,
        block_ms=block_ms,
        peaks=[peak * (i % 4 + 1) / 4 for i in range(count)],
    )


class TestWaveformData:
    def test_invalid_block_ms_rejected(self):
        with pytest.raises(ValueError):
            WaveformData(100, 0, [])

    def test_peak_at(self):
        data = _make_wave_data()
        assert data.peak_at(0) == pytest.approx(0.125)
        assert data.peak_at(35) == pytest.approx(0.5)  # 第 3 块（i=3 → 4/4）
        assert data.peak_at(-1) == 0.0
        assert data.peak_at(10_000) == 0.0  # 越界

    def test_window_peaks_buckets(self):
        data = _make_wave_data(count=100)  # 峰值 0.125/0.25/0.375/0.5 循环
        buckets = data.window_peaks(0, 1_000, 4)
        assert len(buckets) == 4
        # 每桶 25 块：i%4 循环内必含峰值 0.5（桶内取最大值语义）
        assert buckets == pytest.approx([0.5, 0.5, 0.5, 0.5])

    def test_window_peaks_zoom_in(self):
        data = _make_wave_data()
        buckets = data.window_peaks(0, 1_000, 100)  # 桶=块（1000ms / 10ms）：一一对应
        assert buckets == pytest.approx(data.peaks)

    def test_window_peaks_degenerate(self):
        data = _make_wave_data()
        assert data.window_peaks(500, 500, 10) == [0.0] * 10  # 空窗口
        assert data.window_peaks(0, 1_000, 0) == []
        assert data.window_peaks(50_000, 60_000, 4) == [0.0] * 4  # 越界窗口


@pytest.fixture()
def sine_wav(tmp_path):
    """合成 0.2s / 16kHz / 单声道 s16 正弦 wav（幅度 8000）。"""
    import av

    path = tmp_path / "sine.wav"
    rate = 16_000
    total = rate // 5  # 0.2s
    samples = bytearray()
    for i in range(total):
        value = int(8_000 * math.sin(2 * math.pi * 440 * i / rate))
        samples.extend(struct.pack("<h", value))
    container = av.open(str(path), "w", format="wav")
    stream = container.add_stream("pcm_s16le", rate=rate)
    stream.layout = "mono"
    frame = av.AudioFrame(format="s16", layout="mono", samples=total)
    frame.planes[0].update(bytes(samples))
    frame.rate = rate
    for packet in stream.encode(frame):
        container.mux(packet)
    container.close()
    return path


class TestExtractReal:
    def test_extract_sine_wav(self, sine_wav):
        data = extract_waveform(sine_wav)
        assert data is not None
        assert data.block_ms == DEFAULT_BLOCK_MS
        assert data.duration_ms == pytest.approx(200, abs=DEFAULT_BLOCK_MS * 3)
        assert len(data.peaks) >= 18  # 0.2s / 10ms
        loud = max(data.peaks)
        assert 0.15 < loud < 0.35  # 幅度 8000 / 32768 ≈ 0.244

    def test_extract_missing_file_returns_none(self, tmp_path):
        assert extract_waveform(tmp_path / "no.wav") is None

    def test_extract_no_audio_stream(self, tmp_path):
        text = tmp_path / "fake.mp4"
        text.write_bytes(b"not a media file")
        assert extract_waveform(text) is None


class TestCachedWaveform:
    def test_roundtrip_and_sidecar(self, sine_wav, tmp_path):
        first = cached_waveform(sine_wav)
        assert first is not None
        sidecar = sine_wav.with_name(sine_wav.name + ".waveform.json")
        assert sidecar.is_file()
        second = cached_waveform(sine_wav)
        assert second == first  # 数据类相等：缓存往返无损

    def test_cache_rejected_on_block_change(self, sine_wav):
        first = cached_waveform(sine_wav, block_ms=10)
        rebuilt = cached_waveform(sine_wav, block_ms=50)
        assert first.block_ms == 10
        assert rebuilt.block_ms == 50
        assert rebuilt.peaks != first.peaks  # 不同分辨率各自成立
