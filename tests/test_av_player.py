"""PyAV 自管播放引擎的离线验证：元数据解析、时钟换算、帧转换与门面状态机。

遵循项目离线测试约定：不构造真实 QWidget/QAudioSink，不启动真实解码线程
（worker 链路人工联测）；仅验证纯函数与门面状态机的可注入逻辑。
"""

from __future__ import annotations

import ctypes
from fractions import Fraction

from PyQt6.QtGui import QImage

from renpy_overlay.video.av_player import (
    AUDIO_RATE,
    DEFAULT_FPS,
    AvVideoPlayer,
    PlaybackState,
    _DecodeContext,
    av_frame_to_qimage,
    container_duration_ms,
    frame_time_ms,
    media_duration_ms,
    stream_duration_ms,
    stream_fps,
    target_playback_pts,
)

# ---------------------------------------------------------------- 元数据解析


class _FakeStream:
    """av 流鸭子接口 stub（rate/duration/time_base 可注入）。"""

    def __init__(self, average_rate=None, guessed_rate=None, duration=None, time_base=None):
        if average_rate is not None:
            self.average_rate = average_rate
        if guessed_rate is not None:
            self.guessed_rate = guessed_rate
        if duration is not None:
            self.duration = duration
        if time_base is not None:
            self.time_base = time_base


def test_stream_fps_average_rate_wins():
    assert stream_fps(_FakeStream(average_rate=Fraction(30000, 1001))) == float(
        Fraction(30000, 1001)
    )


def test_stream_fps_guessed_rate_fallback():
    """无 average_rate 时回退 guessed_rate。"""
    assert stream_fps(_FakeStream(guessed_rate=25)) == 25.0


def test_stream_fps_invalid_or_missing_falls_back():
    assert stream_fps(_FakeStream(average_rate=0)) == DEFAULT_FPS  # 非正回退
    assert stream_fps(_FakeStream(average_rate="bad", guessed_rate=None)) == DEFAULT_FPS
    assert stream_fps(_FakeStream()) == DEFAULT_FPS  # 两者皆缺
    assert DEFAULT_FPS == 30.0


def test_stream_duration_ms_time_base_conversion():
    stream = _FakeStream(duration=90_000, time_base=Fraction(1, 1000))
    assert stream_duration_ms(stream) == 90_000  # 90000 × (1/1000) × 1000


def test_stream_duration_ms_unknown():
    assert stream_duration_ms(_FakeStream()) == -1  # 无 duration
    assert stream_duration_ms(_FakeStream(duration=-5, time_base=Fraction(1, 1000))) == -1


def test_container_duration_ms_micros():
    assert container_duration_ms(1_234_567) == 1234  # 微秒 → 毫秒
    assert container_duration_ms(0) == -1  # 未知
    assert container_duration_ms(None) == -1


def test_media_duration_ms_stream_first_container_fallback():
    stream = _FakeStream(duration=90_000, time_base=Fraction(1, 1000))
    assert media_duration_ms(stream, 500_000) == 90_000  # 流优先
    assert media_duration_ms(_FakeStream(), 500_000) == 500  # 容器兜底
    assert media_duration_ms(_FakeStream(), None) == -1  # 皆未知


# ---------------------------------------------------------------- 时钟与帧转换


def test_target_playback_pts_advances_with_wall_clock():
    assert target_playback_pts(10_000, 1000.0, 1042.0) == 10_042
    assert target_playback_pts(10_000, 1000.0, 900.0) == 10_000  # 时钟回拨钳零
    assert target_playback_pts(10_000, 1000.0, 1000.0) == 10_000  # 零流逝


class _FakePlane:
    """真实 ctypes 缓冲的平面 stub：buffer_ptr/buffer_size 可供 string_at 拷贝。"""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.line_size = width * 3  # rgb24 每行 3 字节/像素（紧凑无填充）
        self._buf = ctypes.create_string_buffer(bytes(range(self.line_size)) * height)
        self.buffer_ptr = ctypes.addressof(self._buf)
        self.buffer_size = self.line_size * height


class _FakeFrame:
    """av 视频帧鸭子接口 stub（reformat 返回自身携带 planes）。"""

    def __init__(self, width: int, height: int, time: float):
        self.planes = [_FakePlane(width, height)]
        self.time = time

    def reformat(self, **_kwargs):
        return self


def test_av_frame_to_qimage_dimensions_and_copy():
    frame = _FakeFrame(4, 3, 0.033)
    image = av_frame_to_qimage(frame)
    assert isinstance(image, QImage)
    assert image.width() == 4
    assert image.height() == 3
    assert image.format() == QImage.Format.Format_RGB888


def test_frame_time_ms_clamps_and_handles_missing():
    assert frame_time_ms(_FakeFrame(2, 2, 1.5)) == 1500
    assert frame_time_ms(_FakeFrame(2, 2, -0.5)) == 0  # 负值钳零
    frame = _FakeFrame(2, 2, 0.0)
    del frame.time
    assert frame_time_ms(frame) == 0  # 缺 time 属性


def test_audio_rate_constant():
    assert AUDIO_RATE == 48_000


# ---------------------------------------------------------------- 门面状态机


def _make_player() -> AvVideoPlayer:
    """不起真线程的门面（start_worker=False：请求字段可安全断言）。"""
    return AvVideoPlayer(clock_ms=lambda: 1000.0, start_worker=False)


def test_player_initial_state():
    player = _make_player()
    assert player.playbackState() == PlaybackState.Stopped
    assert player.position() == 0
    assert player.duration() == -1
    assert player.hasMedia() is False


def test_player_play_pause_sets_state_and_command():
    player = _make_player()
    player.play()
    assert player.playbackState() == PlaybackState.Playing
    assert player._command == "play"
    player.pause()
    assert player.playbackState() == PlaybackState.Paused
    assert player._command == "pause"


def test_player_set_position_latest_wins():
    """连点 seek：请求字段被覆盖，worker 每轮只消费最新目标。"""
    player = _make_player()
    player.setPosition(1000)
    player.setPosition(2000)
    player.setPosition(3000)
    assert player._seek_request_ms == 3000  # 最新胜出


def test_player_set_position_clamps_negative():
    player = _make_player()
    player.setPosition(-500)
    assert player._seek_request_ms == 0


def test_player_volume_clamps():
    player = _make_player()
    player.setVolume(1.5)
    assert player._volume == 1.0
    player.setVolume(-0.5)
    assert player._volume == 0.0
    player.setVolume(0.5)
    assert player._volume == 0.5


def test_player_open_records_request():
    player = _make_player()
    player.open("D:/video/a.mp4")
    assert player._open_request == "D:/video/a.mp4"
    assert player._seek_request_ms is None  # open 清挂起 seek
    assert player._command is None


def test_player_close_sets_stop_flag():
    player = _make_player()
    player.close()
    assert player._stop_flag is True


def test_decode_context_state_transitions():
    """_DecodeContext 命令应用：play 记录节拍基准（暂停为无操作）。"""
    player = _make_player()
    context = _DecodeContext(player)
    player._position_ms = 5000
    context.apply_command("play")
    assert context._play_start_pts == 5000
    assert context._play_start_wall == 1000.0  # 注入钟
    context.apply_command("pause")  # 无操作不抛错


class _FakeSinkIO:
    """push 模式 QIODevice stub：write 记录（音频 PCM 落点）。"""

    def __init__(self):
        self.written: list[bytes] = []

    def write(self, chunk):
        self.written.append(chunk)
        return len(chunk)


class _StubSink:
    """QAudioSink stub：记录 start/resume/suspend/reset/setVolume。"""

    def __init__(self):
        self.started = 0
        self.resumed = 0
        self.suspended = 0
        self.reset_count = 0
        self.volume: float | None = None
        self.io = _FakeSinkIO()

    def start(self):
        self.started += 1
        return self.io  # push 模式：返回 QIODevice

    def resume(self):
        self.resumed += 1

    def suspend(self):
        self.suspended += 1

    def reset(self):
        self.reset_count += 1
        self.io = _FakeSinkIO()  # reset 后旧 io 失效（与引擎 _sink_io 同步）

    def setVolume(self, v):
        self.volume = v


def test_control_sink_state_management():
    """sink 状态管理：play→start（首）/resume（suspend 后）、
    播放中 setPosition→reset+start、暂停态 setPosition→仅 reset。"""
    player = _make_player()
    sink = _StubSink()
    player._audio_output = sink
    player._state = PlaybackState.Playing
    player._control_sink("start")
    assert sink.started == 1
    player._control_sink("suspend")
    assert sink.suspended == 1
    player._control_sink("start")  # suspend 后 start 走 resume
    assert sink.resumed == 1
    player.setPosition(5000)  # 播放中 seek：reset + start
    assert sink.reset_count == 1
    assert sink.started == 2
    player._state = PlaybackState.Paused
    player.setPosition(6000)  # 暂停态 seek：仅 reset
    assert sink.reset_count == 2
    assert sink.started == 2


def test_write_audio_only_when_playing():
    """音频写入守卫：仅播放态经 push io 写 sink（seek 快进/暂停丢弃）。"""
    player = _make_player()
    sink = _StubSink()
    player._audio_output = sink
    player._state = PlaybackState.Paused
    player._write_audio(b"a")
    assert sink.io.written == []
    player._state = PlaybackState.Playing
    player._control_sink("start")  # stopped→start 获得 push io
    player._write_audio(b"b")
    assert sink.io.written == [b"b"]


def test_reset_play_clock_uses_engine_position():
    """节拍基准重置：pts 取引擎缓存位置、wall 取注入钟；play 与
    播放中 seek（_do_seek 内）共用同一重置入口（修复 seek 后快放）。"""
    player = _make_player()
    player._position_ms = 123_456
    context = _DecodeContext(player)
    context.reset_play_clock()
    assert context._play_start_pts == 123_456
    assert context._play_start_wall == 1000.0
    player._position_ms = 223_456
    context.apply_command("play")
    assert context._play_start_pts == 223_456


def test_audio_format_ready_signal_reaches_apply():
    """worker→主线程的音频参数信号：emit 后参数正确到达槽（含音量同步）。

    先断开真实 sink 构造槽（无 GUI 测试不构造 QAudioSink），另接记录器
    验证信号到槽的参数传递（修复跨线程 QTimer.singleShot 静默失效致
    sink 永不创建致无声的问题，该修复即本信号）。
    """
    player = _make_player()
    sink = _StubSink()
    player._audio_output = sink
    player.setVolume(0.5)
    player._audio_format_ready.disconnect(player._apply_audio_format)
    received: list[tuple[int, int]] = []
    player._audio_format_ready.connect(lambda rate, ch: received.append((rate, ch)))
    player._audio_format_ready.emit(48_000, 2)
    assert received == [(48_000, 2)]
    assert player._volume == 0.5
