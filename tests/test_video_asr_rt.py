"""实时语音识别（video/asr_rt）的离线验证：纯函数与控制器状态机。

不导入 dashscope / 不采集真实音频：控制器经 ``client_factory``/``mic_finder``
注入替身，``DASHSCOPE_AVAILABLE`` 由 monkeypatch 切换，覆盖开启成功、在途
忽略、依赖/配置/设备/连接失败回退、关闭释放与事件投递（需求：开关式后台
功能，提示走标题窗，开启阻塞其余 API 请求）。
"""

from __future__ import annotations

import numpy as np
import pytest

from renpy_overlay.video import asr_rt
from renpy_overlay.video.asr_rt import (
    ASR_RT_OFF_HINT,
    ASR_RT_ON_HINT,
    ASR_RT_UNAVAILABLE_PREFIX,
    AsrRtController,
    openai_base_url_to_ws_url,
    resolve_asr_rt_options,
)

OPTIONS = ("https://rt.example/compatible-mode/v1", "rt-model", "sk-rt")


class _StubClient:
    """识别客户端鸭子替身：记录调用序列；connect 可注入失败。"""

    def __init__(self, base_url, api_key, model, on_server_event=None):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.on_server_event = on_server_event
        self.updated = False
        self.connected = False
        self.closed = False
        self.sent: list[bytes] = []
        self.fail_connect = False

    def session_update(self, input_audio_format="pcm16", sample_rate=16000):
        self.updated = True

    def connect(self):
        if self.fail_connect:
            raise RuntimeError("连接被拒绝")
        self.connected = True

    def append_input_audio(self, audio):
        self.sent.append(bytes(audio))

    def close(self):
        self.closed = True


class _StubMic:
    """环回设备替身：record 返回静音块（采集循环立即空转，stop 即退出）。"""

    name = "stub-speaker"

    class _Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def record(self, numframes):
            return np.zeros((numframes, 2), dtype="float32")

    def recorder(self, samplerate):
        return self._Recorder()


class _Harness:
    """控制器测试夹具：记录各回调调用并注入替身。"""

    def __init__(self, *, client: _StubClient | None = None, mic_finder=None):
        self.submitted: list[tuple[str, dict]] = []
        self.notes: list[str] = []
        self.acquires = 0
        self.releases = 0
        self.busy = False
        self.states: list[bool] = []
        self.clients: list[_StubClient] = []

        def make_client(base_url, api_key, model, on_server_event=None):
            stub = client or _StubClient(base_url, api_key, model, on_server_event)
            self.clients.append(stub)
            return stub

        self.controller = AsrRtController(
            submit=lambda kind, payload: self.submitted.append((kind, payload)),
            notify=self.notes.append,
            is_busy=lambda: self.busy,
            acquire=lambda: self._acquire(),
            release=lambda: self._release(),
            options=lambda: OPTIONS,
            on_state=self.states.append,
            client_factory=make_client,
            mic_finder=mic_finder or (lambda: _StubMic()),
        )

    def _acquire(self):
        self.acquires += 1

    def _release(self):
        self.releases += 1


# ---------------------------------------------------------------- 纯函数


def test_resolve_asr_rt_options_reads_keys_without_fallback():
    """三键直读不回退（需求确认）：主链路/asr 键有值也不借用。"""

    class _Cfg:
        api_base_url_asr_rt = "https://rt.example"
        model_asr_rt = "rt-model"
        api_key_asr_rt = "sk-rt"
        api_base_url = "https://main.example"
        model = "main-model"
        api_key = "sk-main"
        api_base_url_asr = "https://asr.example"
        model_asr = "asr-model"
        api_key_asr = "sk-asr"

    assert resolve_asr_rt_options(_Cfg()) == ("https://rt.example", "rt-model", "sk-rt")


def test_openai_base_url_to_ws_url():
    assert (
        openai_base_url_to_ws_url("https://host.example/compatible-mode/v1")
        == "wss://host.example/api-ws/v1/inference"
    )
    assert (
        openai_base_url_to_ws_url("http://host.example/compatible-mode/v1")
        == "ws://host.example/api-ws/v1/inference"
    )
    # 已是 ws/wss 形态则原样返回
    assert openai_base_url_to_ws_url("wss://host.example/x") == "wss://host.example/x"
    assert openai_base_url_to_ws_url("ws://host.example/x") == "ws://host.example/x"


def test_resample_to_16k_integer_and_fraction():
    sr = asr_rt.TARGET_SR
    # 采样率相同：原样返回
    mono = np.arange(sr, dtype="float32")
    assert asr_rt._resample_to_16k(mono, sr) is mono
    # 整数倍降采样（48k→16k）：box filter，输出长度 = 输入 / 3
    mono48 = np.ones(48000, dtype="float32")
    down = asr_rt._resample_to_16k(mono48, 48000)
    assert len(down) == sr
    assert down.shape == (sr,)
    # 非整数倍（44.1k→16k）：线性插值，长度按比例取整
    mono44 = np.ones(44100, dtype="float32")
    down44 = asr_rt._resample_to_16k(mono44, 44100)
    assert len(down44) == round(44100 * sr / 44100) == sr


# ---------------------------------------------------------------- 控制器状态机


def test_controller_defaults_to_off():
    harness = _Harness()
    assert harness.controller.enabled is False


def test_toggle_on_acquires_slot_and_notifies(monkeypatch):
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    harness = _Harness()
    harness.controller.toggle()
    assert harness.controller.enabled is True
    assert harness.acquires == 1
    assert harness.notes == [ASR_RT_ON_HINT]
    assert harness.states == [True]
    client = harness.clients[0]
    assert client.updated and client.connected
    assert client.model == "rt-model"
    assert client.base_url == "https://rt.example/compatible-mode/v1"
    assert client.api_key == "sk-rt"


def test_toggle_ignored_while_busy():
    """在途（翻译/识曲/对话/预构建由宿主合并查询）时点击被静默忽略。"""
    harness = _Harness()
    harness.busy = True
    harness.controller.toggle()
    assert harness.controller.enabled is False
    assert harness.acquires == 0
    assert harness.notes == [], "在途被忽略不应更新提示"


def test_toggle_on_fails_without_dashscope(monkeypatch):
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", False)
    harness = _Harness()
    harness.controller.toggle()
    assert harness.controller.enabled is False
    assert harness.acquires == 0
    assert harness.notes == [ASR_RT_UNAVAILABLE_PREFIX + "缺少 dashscope 依赖"]


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_toggle_on_fails_with_empty_options(monkeypatch, missing):
    """三键任一为空即未配置（不回退其它链路，需求确认）。"""
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    harness = _Harness()

    values = list(OPTIONS)
    values[missing] = ""
    harness.controller._options = lambda: tuple(values)
    harness.controller.toggle()
    assert harness.controller.enabled is False
    assert harness.acquires == 0
    assert harness.notes == [ASR_RT_UNAVAILABLE_PREFIX + "未配置 API"]


def test_toggle_on_fails_without_loopback_device(monkeypatch):
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)

    def _no_mic():
        raise RuntimeError("未找到可用的 WASAPI loopback 采集设备")

    harness = _Harness(mic_finder=_no_mic)
    harness.controller.toggle()
    assert harness.controller.enabled is False
    assert harness.acquires == 0
    assert harness.notes == [ASR_RT_UNAVAILABLE_PREFIX + "无可用环回录制设备"]


def test_toggle_on_fails_on_connect_and_releases_slot(monkeypatch):
    """连接失败：回退为关并提示，已占用的互斥槽必须释放。"""
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    client = _StubClient(*OPTIONS)
    client.fail_connect = True
    harness = _Harness(client=client)
    harness.controller.toggle()
    assert harness.controller.enabled is False
    assert harness.acquires == 1 and harness.releases == 1
    assert harness.notes == [ASR_RT_UNAVAILABLE_PREFIX + "连接失败"]
    assert harness.states == [], "启动失败不触发状态变更回调"


def test_toggle_off_releases_slot_and_notifies(monkeypatch):
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    harness = _Harness()
    harness.controller.toggle()
    harness.controller.toggle()
    assert harness.controller.enabled is False
    assert harness.acquires == 1 and harness.releases == 1
    assert harness.notes == [ASR_RT_ON_HINT, ASR_RT_OFF_HINT]
    assert harness.states == [True, False]
    assert harness.clients[0].closed, "关闭应停止识别会话（不等待尾部 flush）"


def test_shutdown_is_silent_and_idempotent(monkeypatch):
    """shutdown（宿主关闭/识别错误路径）：释放互斥但不更新提示；重复调用无害。"""
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    harness = _Harness()
    harness.controller.shutdown()  # 未开启时为无害调用
    assert harness.releases == 0
    harness.controller.toggle()
    harness.controller.shutdown()
    assert harness.controller.enabled is False
    assert harness.releases == 1
    assert harness.notes == [ASR_RT_ON_HINT], "shutdown 静默，不追加关闭提示"
    assert harness.states == [True, False]
    harness.controller.shutdown()
    assert harness.releases == 1


def test_events_submitted_to_host_queue(monkeypatch):
    """delta/completed/error 三类事件经 submit 投递；其余事件仅记日志。"""
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    harness = _Harness()
    harness.controller.toggle()
    client = harness.clients[0]
    client.on_server_event({"type": "response.audio_transcript.delta", "delta": "你好"})
    client.on_server_event(
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好。"}
    )
    client.on_server_event({"type": "error", "code": 400, "message": "bad request"})
    client.on_server_event({"type": "session.created"})
    assert harness.submitted == [
        ("asr_rt_delta", {"text": "你好"}),
        ("asr_rt_completed", {"text": "你好。"}),
        ("asr_rt_error", {"code": 400, "message": "bad request"}),
    ]


def test_capture_loop_streams_pcm_chunks(monkeypatch):
    """采集循环：静音块被推流为 16kHz PCM16 字节；stop 后线程自然退出。"""
    monkeypatch.setattr(asr_rt, "DASHSCOPE_AVAILABLE", True)
    harness = _Harness()
    harness.controller.toggle()
    client = harness.clients[0]
    # 采集线程 100ms 一片：轮询等待至少一片被推流（不强定时序，离线稳定）
    import time

    deadline = time.monotonic() + 2.0
    while not client.sent and time.monotonic() < deadline:
        time.sleep(0.01)
    harness.controller.toggle()  # 停止 → 采集线程退出
    assert client.sent, "采集循环应持续推流音频分片"
    assert isinstance(client.sent[0], bytes) and client.sent[0]
