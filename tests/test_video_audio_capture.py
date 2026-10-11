"""持续环回录音的离线验证：方案回退顺序、停止语义与降级标志。

不触真实音频设备：三个方案的设备依赖全部 monkeypatch（照
test_song_recognition.py 的回退验证模式），仅验证回退顺序、
stop_event 语义与有效性判定。
"""

from __future__ import annotations

import threading

from renpy_overlay.video import audio_capture
from renpy_overlay.video.audio_capture import record_loopback


def _reset(monkeypatch, soundcard=True, patch=True, pyaudio=True):
    """重置三方案的可用性标志（默认全可用）。"""
    monkeypatch.setattr(audio_capture, "SOUNDCARD_AVAILABLE", soundcard)
    monkeypatch.setattr(audio_capture, "PYAUDIOWPATCH_AVAILABLE", patch)
    monkeypatch.setattr(audio_capture, "PYAUDIO_AVAILABLE", pyaudio)


def _fake_loop(name, ok=True, wrote=True, calls=None):
    """生成方案函数 stub：调用名记入 calls，返回 (ok, err)。"""
    calls = [] if calls is None else calls

    def _loop(out_path, stop_event):
        calls.append(name)
        if ok:
            return wrote, None
        return False, f"{name} failed"

    return _loop


def test_record_available_flag_sum_of_schemes():
    assert audio_capture.RECORD_AVAILABLE == (
        audio_capture.SOUNDCARD_AVAILABLE
        or audio_capture.PYAUDIOWPATCH_AVAILABLE
        or audio_capture.PYAUDIO_AVAILABLE
    )


def test_first_available_scheme_wins(monkeypatch, tmp_path):
    _reset(monkeypatch)
    first_calls: list = []
    second_calls: list = []
    monkeypatch.setattr(
        audio_capture, "_loop_with_soundcard", _fake_loop("soundcard", calls=first_calls)
    )
    monkeypatch.setattr(
        audio_capture, "_loop_with_pyaudiowpatch", _fake_loop("patch", calls=second_calls)
    )
    stop = threading.Event()
    assert record_loopback(tmp_path / "a.wav", stop) is True
    assert first_calls == ["soundcard"]
    assert second_calls == []  # 未回退


def test_falls_back_to_next_scheme_on_failure(monkeypatch, tmp_path):
    _reset(monkeypatch)
    first_calls: list = []
    second_calls: list = []
    monkeypatch.setattr(
        audio_capture,
        "_loop_with_soundcard",
        _fake_loop("soundcard", ok=False, calls=first_calls),
    )
    monkeypatch.setattr(
        audio_capture, "_loop_with_pyaudiowpatch", _fake_loop("patch", calls=second_calls)
    )
    stop = threading.Event()
    assert record_loopback(tmp_path / "a.wav", stop) is True
    assert second_calls == ["patch"]


def test_stop_before_start_skips_recording(monkeypatch, tmp_path):
    _reset(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        audio_capture, "_loop_with_soundcard", _fake_loop("soundcard", calls=calls)
    )
    stop = threading.Event()
    stop.set()
    assert record_loopback(tmp_path / "a.wav", stop) is False
    assert calls == []


def test_stop_during_loop_does_not_fall_back(monkeypatch, tmp_path):
    """停止置位后不再回退下一案（与识曲中断语义一致）。"""
    _reset(monkeypatch)
    first_calls: list = []
    second_calls: list = []
    monkeypatch.setattr(
        audio_capture,
        "_loop_with_soundcard",
        _fake_loop("soundcard", ok=False, calls=first_calls),
    )
    monkeypatch.setattr(
        audio_capture, "_loop_with_pyaudiowpatch", _fake_loop("patch", calls=second_calls)
    )
    stop = threading.Event()
    stop.set()  # 首案失败返回后 stop 已置位：模拟停止竞态
    assert record_loopback(tmp_path / "a.wav", stop) is False
    assert second_calls == []


def test_all_schemes_fail_returns_false(monkeypatch, tmp_path):
    _reset(monkeypatch)
    monkeypatch.setattr(
        audio_capture, "_loop_with_soundcard", _fake_loop("soundcard", ok=False)
    )
    monkeypatch.setattr(
        audio_capture, "_loop_with_pyaudiowpatch", _fake_loop("patch", ok=False)
    )
    monkeypatch.setattr(
        audio_capture, "_loop_with_pyaudio", _fake_loop("pyaudio", ok=False)
    )
    stop = threading.Event()
    assert record_loopback(tmp_path / "a.wav", stop) is False


def test_scheme_writes_no_data_reports_failure(monkeypatch, tmp_path):
    """全部方案均未写入任何数据：视为失败（wav 无效），调用方降级仅视频。

    三案全部 stub（拦截回退链，否则会触到真实音频设备初始化）。
    """
    _reset(monkeypatch)
    monkeypatch.setattr(
        audio_capture,
        "_loop_with_soundcard",
        _fake_loop("soundcard", ok=False, wrote=False),
    )
    monkeypatch.setattr(
        audio_capture,
        "_loop_with_pyaudiowpatch",
        _fake_loop("patch", ok=False, wrote=False),
    )
    monkeypatch.setattr(
        audio_capture,
        "_loop_with_pyaudio",
        _fake_loop("pyaudio", ok=False, wrote=False),
    )
    stop = threading.Event()
    assert record_loopback(tmp_path / "a.wav", stop) is False
