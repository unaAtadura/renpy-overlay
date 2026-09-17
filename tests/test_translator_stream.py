"""流式翻译客户端的离线验证：本地 HTTP 桩模拟 SSE（stream=True）响应。"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from renpy_overlay import translator

ERROR_TEXT = "__http_error__"

SSE_BODY = (
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'  # 无 content，跳过
    'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
    'data: {"choices":[{"delta":{"reasoning_content":"思考中"}}]}\n\n'  # 推理内容不算正文
    ": keep-alive\n\n"  # SSE 注释行，跳过
    "data: not-json\n\n"  # 非 JSON 行，跳过
    'data: {"choices":[]}\n\n'  # 空 choices，跳过
    'data: {"choices":[{"delta":{"content":"，世界"}}]}\n\n'
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'  # 空增量，跳过
    "data: [DONE]\n\n"
)


class _StreamStubHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    models: list[dict] = [{"id": "stub-model"}]

    def log_message(self, *args):  # 静音桩服务器日志
        pass

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server 约定
        if self.path == "/v1/models":
            self._send_json(200, {"data": type(self).models})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
            }
        )
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        user_text = body["messages"][-1]["content"]
        if user_text == ERROR_TEXT:
            self._send_json(500, {"error": "boom"})
            return
        # 流式响应：不带 Content-Length，写完即关闭连接（HTTP/1.0 语义）
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(SSE_BODY.encode("utf-8"))
        self.close_connection = True


@pytest.fixture()
def stub_server():
    _StreamStubHandler.received = []
    _StreamStubHandler.models = [{"id": "stub-model"}]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StreamStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, _StreamStubHandler.received
    server.shutdown()
    server.server_close()


def test_stream_roundtrip_yields_chunks_in_order(stub_server):
    base, received = stub_server
    chunks = list(translator.translate_text_stream("Hello!", base_url=base, timeout=5))
    assert chunks == ["你好", "，世界"]
    assert len(received) == 1
    body = received[0]["body"]
    assert body["stream"] is True  # 与非流式接口唯一的请求差异
    assert body["model"] == "stub-model"  # 自动取 /v1/models 的第一个模型
    assert received[0]["accept"] == "text/event-stream"
    assert body["messages"][-1] == {"role": "user", "content": "Hello!"}


def test_stream_payload_matches_non_stream_dialect(stub_server):
    """流式请求体携带与非流式一致的思考模式方言字段。"""
    base, received = stub_server
    list(translator.translate_text_stream("hi", base_url=base, timeout=5))
    body = received[0]["body"]
    assert body["enable_thinking"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["reasoning_effort"] == "none"


def test_stream_options_forwarded(stub_server):
    base, received = stub_server
    list(
        translator.translate_text_stream(
            "hi",
            base_url=base,
            timeout=5,
            model="cfg-model",
            system_prompt="只输出中文",
            api_key="sk-test-123",
            enable_thinking=True,
            reasoning_effort="",
        )
    )
    body = received[0]["body"]
    assert body["model"] == "cfg-model"  # 显式 model：不查 /v1/models
    assert body["messages"][0] == {"role": "system", "content": "只输出中文"}
    assert body["enable_thinking"] is True
    assert "reasoning_effort" not in body
    assert received[0]["authorization"] == "Bearer sk-test-123"


def test_stream_is_lazy_until_iterated(stub_server):
    """生成器惰性：未迭代时不发请求（模型发现也在首次迭代时进行）。"""
    base, received = stub_server
    generator = translator.translate_text_stream("hi", base_url=base, timeout=5)
    assert received == [], "创建生成器不应发出任何请求"
    list(generator)
    assert len(received) == 1


def test_stream_empty_text_rejected_before_any_request(stub_server):
    _base, received = stub_server
    with pytest.raises(ValueError, match="没有可翻译的文本"):
        list(translator.translate_text_stream("   "))
    assert received == [], "空文本不应发出任何请求"


def test_stream_http_error_raises(stub_server):
    base, received = stub_server
    with pytest.raises(translator.TranslationError, match="HTTP 500"):
        list(translator.translate_text_stream(ERROR_TEXT, base_url=base, timeout=5))
    assert len(received) == 1


def test_stream_no_loaded_model_raises(stub_server):
    base, _received = stub_server
    _StreamStubHandler.models = []
    with pytest.raises(translator.TranslationError, match="模型"):
        list(translator.translate_text_stream("hi", base_url=base, timeout=5))


def test_stream_unreachable_service_raises():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # 关闭后该端口大概率无人监听
    with pytest.raises(translator.TranslationError, match="无法连接"):
        list(translator.translate_text_stream("hi", base_url=f"http://127.0.0.1:{port}", timeout=2))
