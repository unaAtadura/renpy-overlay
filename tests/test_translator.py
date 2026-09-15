"""翻译客户端的离线验证：用本地 HTTP 桩模拟 LM Studio（OpenAI 兼容协议）。"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from renpy_overlay import translator

NO_CHOICES = "__no_choices__"
EMPTY_TRANSLATION = "__empty_text__"


class _StubHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    models: list[dict] = [{"id": "stub-model"}, {"id": "second-model"}]

    def log_message(self, *args):  # 静音桩服务器日志
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server 约定
        if self.path == "/v1/models":
            self._send(200, {"data": type(self).models})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received.append({"path": self.path, "body": body})
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "not found"})
            return
        user_text = body["messages"][-1]["content"]
        if user_text == NO_CHOICES:
            self._send(200, {"choices": []})
        elif user_text == EMPTY_TRANSLATION:
            self._send(200, {"choices": [{"message": {"content": "   "}}]})
        else:
            self._send(200, {"choices": [{"message": {"content": "你好，世界"}}]})


@pytest.fixture()
def stub_server():
    _StubHandler.received = []
    _StubHandler.models = [{"id": "stub-model"}, {"id": "second-model"}]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, _StubHandler.received
    server.shutdown()
    server.server_close()


def test_translate_roundtrip_uses_original_text(stub_server):
    base, received = stub_server
    result = translator.translate_text("Hello, world!", base_url=base, timeout=5)
    assert result == "你好，世界"
    assert len(received) == 1
    body = received[0]["body"]
    assert body["model"] == "stub-model"  # 自动取 /v1/models 的第一个模型
    assert body["stream"] is False
    messages = body["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "Hello, world!"}


def test_explicit_model_skips_models_lookup(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5, model="my-model")
    assert len(received) == 1  # 没有额外的 GET /v1/models
    assert received[0]["body"]["model"] == "my-model"


def test_empty_text_rejected_before_any_request(stub_server):
    _base, received = stub_server
    with pytest.raises(ValueError, match="没有可翻译的文本"):
        translator.translate_text("   ")
    assert received == [], "空文本不应发出任何请求"


def test_missing_choices_raises(stub_server):
    base, _received = stub_server
    with pytest.raises(translator.TranslationError, match="choices"):
        translator.translate_text(NO_CHOICES, base_url=base, timeout=5)


def test_empty_translation_raises(stub_server):
    base, _received = stub_server
    with pytest.raises(translator.TranslationError, match="空译文"):
        translator.translate_text(EMPTY_TRANSLATION, base_url=base, timeout=5)


def test_no_loaded_model_raises(stub_server):
    base, _received = stub_server
    _StubHandler.models = []
    with pytest.raises(translator.TranslationError, match="模型"):
        translator.translate_text("hi", base_url=base, timeout=5)


def test_unreachable_service_raises():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # 关闭后该端口大概率无人监听
    with pytest.raises(translator.TranslationError, match="无法连接"):
        translator.translate_text("hi", base_url=f"http://127.0.0.1:{port}", timeout=2)
