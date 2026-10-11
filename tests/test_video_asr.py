"""语音识别客户端的离线验证：本地 HTTP 桩模拟 OpenAI 兼容 chat 接口。

照 test_screenshot_vision.py 模式：ThreadingHTTPServer 桩接收请求、
校验请求体（input_audio base64 / model / 思考方言字段），覆盖成功解析、
响应缺字段、空转写与连接失败。
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from renpy_overlay import config, translator
from renpy_overlay.video import asr


class _StubHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    response_body = {"choices": [{"message": {"content": "识别出的语音文本"}}]}
    status = 200

    def log_message(self, *args):  # 静音桩服务器日志
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        _StubHandler.received.append(
            {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)}
        )
        self._send(self.status, self.response_body)


@pytest.fixture()
def stub_server():
    _StubHandler.received = []
    _StubHandler.response_body = {"choices": [{"message": {"content": "识别出的语音文本"}}]}
    _StubHandler.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, _StubHandler.received
    server.shutdown()
    server.server_close()


_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt fake-audio-bytes"


def test_resolve_asr_options_falls_back_to_main_chain():
    """三键空值回退主链路（与 vision 同构）；非空优先。"""
    cfg = config.AppConfig(
        api_base_url="http://main:1234",
        model="main-model",
        api_key="sk-main",
    )
    assert asr.resolve_asr_options(cfg) == ("http://main:1234", "main-model", "sk-main")
    cfg = config.AppConfig(
        api_base_url="http://main:1234",
        model="main-model",
        api_key="sk-main",
        api_base_url_asr="http://asr:9999",
        model_asr="asr-model",
        api_key_asr="sk-asr",
    )
    assert asr.resolve_asr_options(cfg) == ("http://asr:9999", "asr-model", "sk-asr")


def test_audio_content_embeds_base64_as_data_url():
    """data 用 Data URL 而非纯 base64（防回归：DashScope compatible-mode
    把 data 当音频 URL 解析，纯 base64 会被判 URL 无效 HTTP 400，实测见
    logs/renpy_overlay_20261011_163432.log）。"""
    content = asr.audio_content(_WAV)
    assert content["type"] == "input_audio"
    assert content["input_audio"]["format"] == "wav"
    data = content["input_audio"]["data"]
    prefix = "data:audio/wav;base64,"
    assert data.startswith(prefix)
    assert base64.b64decode(data[len(prefix):]) == _WAV


def test_is_dedicated_asr_model():
    """模型名含 asr（不区分大小写）即为专用转写模型。"""
    assert asr.is_dedicated_asr_model("qwen3-asr-flash") is True
    assert asr.is_dedicated_asr_model("Qwen-Audio-3.1-ASR-Flash") is True
    assert asr.is_dedicated_asr_model("qwen2.5-omni") is False
    assert asr.is_dedicated_asr_model("") is False
    assert asr.is_dedicated_asr_model(None) is False


def test_recognize_audio_dedicated_asr_payload(stub_server):
    """专用 ASR 模型（如 qwen3-asr-flash）按官方形态发送：单条 user 消息
    仅含 input_audio——text 指令段与思考方言字段会被判 does not support
    this input（HTTP 400，实测 logs/renpy_overlay_20261011_165601.log）。"""
    base, received = stub_server
    result = asr.recognize_audio(
        _WAV, base_url=base, timeout=5, model="qwen3-asr-flash", api_key="sk-1"
    )
    assert result == "识别出的语音文本"
    body = received[0]["body"]
    assert body["model"] == "qwen3-asr-flash"
    assert body["stream"] is False
    content = body["messages"][0]["content"]
    assert len(content) == 1, "专用 ASR 不接受 text 指令段"
    assert content[0]["type"] == "input_audio"
    assert "temperature" not in body
    assert "enable_thinking" not in body
    assert "chat_template_kwargs" not in body
    assert "reasoning_effort" not in body


def test_recognize_audio_success_sends_input_audio(stub_server):
    """通用全模态模型（名字不含 asr）：保留指令文本段与思考方言字段。"""
    base, received = stub_server
    result = asr.recognize_audio(_WAV, base_url=base, timeout=5, model="omni", api_key="sk-1")
    assert result == "识别出的语音文本"
    request = received[0]
    assert request["path"].endswith("/chat/completions")
    assert request["headers"].get("Authorization") == "Bearer sk-1"
    body = request["body"]
    assert body["model"] == "omni"
    assert body["stream"] is False
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "input_audio"
    audio_data = content[0]["input_audio"]["data"]
    assert audio_data.startswith("data:audio/wav;base64,")
    assert base64.b64decode(audio_data.split(",", 1)[1]) == _WAV
    assert content[0]["input_audio"]["format"] == "wav"
    assert "逐字转写" in content[1]["text"]
    # 思考模式方言：默认关闭时附 reasoning_effort
    assert body.get("reasoning_effort") == translator.DEFAULT_REASONING_EFFORT


def test_recognize_audio_empty_text_raises(stub_server):
    base, _received = stub_server
    _StubHandler.response_body = {"choices": [{"message": {"content": "  "}}]}
    with pytest.raises(translator.TranslationError, match="空转写"):
        asr.recognize_audio(_WAV, base_url=base, timeout=5, model="m")


def test_recognize_audio_missing_choices_raises(stub_server):
    base, _received = stub_server
    _StubHandler.response_body = {"error": {"message": "boom"}}
    with pytest.raises(translator.TranslationError, match="choices"):
        asr.recognize_audio(_WAV, base_url=base, timeout=5, model="m")


def test_recognize_audio_connection_error_is_endpoint_unavailable():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(translator.EndpointUnavailable):
        asr.recognize_audio(_WAV, base_url=f"http://127.0.0.1:{port}", timeout=2, model="m")


def test_recognize_audio_rejects_empty_wav():
    with pytest.raises(ValueError):
        asr.recognize_audio(b"", base_url="http://127.0.0.1:1", timeout=1, model="m")
