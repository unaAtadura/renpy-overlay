"""vision 识别翻译客户端的离线验证：本地 HTTP 桩模拟 OpenAI 兼容多模态接口。"""

from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from renpy_overlay import translator
from renpy_overlay.screenshot import vision

NO_CHOICES = "__no_choices__"
EMPTY_TEXT = "__empty_text__"


class _StubHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    models: list[dict] = [{"id": "stub-model"}]

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
        content = body["messages"][0]["content"]
        instruction = content[-1]["text"]
        if instruction == NO_CHOICES:
            self._send(200, {"choices": []})
        elif instruction == EMPTY_TEXT:
            self._send(200, {"choices": [{"message": {"content": "  "}}]})
        else:
            self._send(200, {"choices": [{"message": {"content": "识别到的中文译文"}}]})


@pytest.fixture()
def stub_server():
    _StubHandler.received = []
    _StubHandler.models = [{"id": "stub-model"}]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, _StubHandler.received
    server.shutdown()
    server.server_close()


def _tiny_jpeg_bytes() -> bytes:
    import io

    from PIL import Image

    buffered = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 255, 255)).save(buffered, format="JPEG")
    return buffered.getvalue()


def _tiny_base64() -> str:
    return base64.b64encode(_tiny_jpeg_bytes()).decode("ascii")


def test_roundtrip_with_auto_model(stub_server):
    base, received = stub_server
    result = vision.translate_image(_tiny_base64(), base_url=base, timeout=5)
    assert result == "识别到的中文译文"
    assert len(received) == 1  # 桩只记录 POST；body.model 已证明发生了 /v1/models 查询
    body = received[0]["body"]
    assert body["model"] == "stub-model"
    assert body["stream"] is False
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    url = content[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1])  # base64 可解码
    assert content[1]["type"] == "text"
    assert "翻译" in content[1]["text"]


def test_explicit_model_skips_lookup_and_keeps_image(stub_server):
    base, received = stub_server
    vision.translate_image(_tiny_base64(), base_url=base, timeout=5, model="cfg-vl")
    assert len(received) == 1  # 没有额外的 GET /v1/models
    assert received[0]["body"]["model"] == "cfg-vl"


def test_thinking_disabled_by_default(stub_server):
    base, received = stub_server
    vision.translate_image(_tiny_base64(), base_url=base, timeout=5, model="m")
    body = received[0]["body"]
    assert body["enable_thinking"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["reasoning_effort"] == "none"  # LM Studio 需要它才能真正关闭思考


def test_enable_thinking_forwarded(stub_server):
    base, received = stub_server
    vision.translate_image(_tiny_base64(), base_url=base, timeout=5, model="m", enable_thinking=True)
    body = received[0]["body"]
    assert body["enable_thinking"] is True
    assert "reasoning_effort" not in body


def test_custom_instruction_forwarded(stub_server):
    base, received = stub_server
    vision.translate_image(
        _tiny_base64(), base_url=base, timeout=5, model="m", instruction="只输出原文"
    )
    assert received[0]["body"]["messages"][0]["content"][-1]["text"] == "只输出原文"


def test_empty_base64_rejected_before_any_request(stub_server):
    _base, received = stub_server
    with pytest.raises(ValueError, match="没有可识别的图片"):
        vision.translate_image("")
    assert received == []


def test_missing_choices_raises(stub_server):
    base, _received = stub_server
    with pytest.raises(translator.TranslationError, match="choices"):
        vision.translate_image(_tiny_base64(), base_url=base, timeout=5, instruction=NO_CHOICES)


def test_empty_text_raises(stub_server):
    base, _received = stub_server
    with pytest.raises(translator.TranslationError, match="空译文"):
        vision.translate_image(_tiny_base64(), base_url=base, timeout=5, instruction=EMPTY_TEXT)


def test_unreachable_service_raises():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(translator.TranslationError, match="无法连接"):
        vision.translate_image(_tiny_base64(), base_url=f"http://127.0.0.1:{port}", timeout=2)
