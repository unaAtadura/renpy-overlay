"""听歌识曲包的离线验证：结果提取/展示纯函数、录制三重回退与依赖降级。

不触真实音频设备与网络：录制方案的可用性标志与内部函数全部 monkeypatch，
仅验证回退顺序、中断语义与结果格式化逻辑。
"""

from __future__ import annotations

import threading
from pathlib import Path

from renpy_overlay.song_recognition import (
    extract_track_info,
    format_fail_body,
    format_success_body,
    recognize,
    record_system_audio,
)
from renpy_overlay.song_recognition import recognizer as recognizer_mod
from renpy_overlay.song_recognition import recorder as recorder_mod

# ---------------------------------------------------------------- 结果提取与展示


def test_extract_track_info_normal():
    track = {"title": "Song A", "subtitle": "Artist B"}
    assert extract_track_info(track) == ("Song A", "Artist B")


def test_extract_track_info_missing_fields_fall_back():
    """title / subtitle 缺失、空白或 None 时回退「未知」。"""
    assert extract_track_info({"title": "Song A"}) == ("Song A", "未知")
    assert extract_track_info({"subtitle": "Artist B"}) == ("未知", "Artist B")
    assert extract_track_info({}) == ("未知", "未知")
    assert extract_track_info({"title": "  ", "subtitle": None}) == ("未知", "未知")


def test_extract_track_info_non_dict():
    assert extract_track_info(None) == ("未知", "未知")


def test_format_success_body():
    assert format_success_body("Song A", "Artist B") == "歌曲名: Song A\n艺术家: Artist B\n"


def test_format_fail_body():
    assert format_fail_body() == "识曲失败。\n多次失败可能是这首歌没有被收录。\n"


# ---------------------------------------------------------------- 录制三重回退


def _all_recorder_libs_available(monkeypatch):
    monkeypatch.setattr(recorder_mod, "SOUNDCARD_AVAILABLE", True)
    monkeypatch.setattr(recorder_mod, "PYAUDIOWPATCH_AVAILABLE", True)
    monkeypatch.setattr(recorder_mod, "PYAUDIO_AVAILABLE", True)


def test_record_system_audio_without_recorder_lib(monkeypatch):
    """无任何录制库可用时返回 None（依赖降级，不抛异常）。"""
    monkeypatch.setattr(recorder_mod, "SOUNDCARD_AVAILABLE", False)
    monkeypatch.setattr(recorder_mod, "PYAUDIOWPATCH_AVAILABLE", False)
    monkeypatch.setattr(recorder_mod, "PYAUDIO_AVAILABLE", False)
    assert record_system_audio(duration=8) is None


def test_record_system_audio_all_schemes_fail(monkeypatch):
    """三案全失败：按顺序尝试全部方案后返回 None。"""
    _all_recorder_libs_available(monkeypatch)
    called: list[str] = []

    def fail(name):
        def scheme(duration, stop_event):
            called.append(name)
            return None, f"{name} 失败"

        return scheme

    monkeypatch.setattr(recorder_mod, "_record_with_soundcard", fail("soundcard"))
    monkeypatch.setattr(recorder_mod, "_record_with_pyaudiowpatch", fail("pyaudiowpatch"))
    monkeypatch.setattr(recorder_mod, "_record_with_pyaudio", fail("pyaudio"))

    assert record_system_audio(duration=8) is None
    assert called == ["soundcard", "pyaudiowpatch", "pyaudio"]


def test_record_system_audio_falls_through_to_next_scheme(monkeypatch, tmp_path):
    """前案失败自动回退：soundcard 失败后 WASAPI 环回方案的结果被采用。"""
    _all_recorder_libs_available(monkeypatch)
    monkeypatch.setattr(recorder_mod, "_record_with_soundcard", lambda d, s: (None, "不支持"))
    wanted = tmp_path / "fake.wav"
    wanted.write_bytes(b"")
    monkeypatch.setattr(
        recorder_mod, "_record_with_pyaudiowpatch", lambda d, s: (wanted, None)
    )

    def unexpected(duration, stop_event):  # pragma: no cover - 不应被调用
        raise AssertionError("前案已成功时不应回退到后续方案")

    monkeypatch.setattr(recorder_mod, "_record_with_pyaudio", unexpected)

    assert record_system_audio(duration=8) == wanted


def test_record_system_audio_preset_stop_skips_all_schemes(monkeypatch):
    """stop_event 预置（双击先于录制）：不进入任何录制方案，直接返回 None。"""
    monkeypatch.setattr(recorder_mod, "SOUNDCARD_AVAILABLE", True)

    def unexpected(duration, stop_event):  # pragma: no cover - 不应被调用
        raise AssertionError("stop_event 预置时不应进入录制方案")

    monkeypatch.setattr(recorder_mod, "_record_with_soundcard", unexpected)
    stop = threading.Event()
    stop.set()
    assert record_system_audio(duration=8, stop_event=stop) is None


def test_record_system_audio_aborted_midway_does_not_fall_through(monkeypatch):
    """逐块录制中被中止：丢弃结果且不再回退后续方案。"""
    monkeypatch.setattr(recorder_mod, "SOUNDCARD_AVAILABLE", False)
    monkeypatch.setattr(recorder_mod, "PYAUDIOWPATCH_AVAILABLE", True)
    monkeypatch.setattr(recorder_mod, "PYAUDIO_AVAILABLE", True)

    stop = threading.Event()

    def abort_pyaudiowpatch(duration, stop_event):
        stop_event.set()  # 模拟逐块读取期间外部中止（双击解锁）
        return None, None

    def unexpected(duration, stop_event):  # pragma: no cover - 不应被调用
        raise AssertionError("已中止后不应回退到后续方案")

    monkeypatch.setattr(recorder_mod, "_record_with_pyaudiowpatch", abort_pyaudiowpatch)
    monkeypatch.setattr(recorder_mod, "_record_with_pyaudio", unexpected)

    assert record_system_audio(duration=8, stop_event=stop) is None


# ---------------------------------------------------------------- Shazam 识别


def test_recognize_without_shazamio(monkeypatch):
    """shazamio 未安装：recognize 返回 None 且不尝试导入。"""
    monkeypatch.setattr(recognizer_mod, "SHAZAMIO_AVAILABLE", False)
    assert recognize(Path("whatever.wav")) is None


def test_recognize_missing_audio_file(monkeypatch):
    """音频文件不存在：返回 None，不触发识别。"""
    monkeypatch.setattr(recognizer_mod, "SHAZAMIO_AVAILABLE", True)
    assert recognize(Path("Z:/no/such/file.wav")) is None
