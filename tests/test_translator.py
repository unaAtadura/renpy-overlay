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
        type(self).received.append(
            {
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
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


def test_custom_options_forwarded(stub_server):
    base, received = stub_server
    translator.translate_text(
        "hi", base_url=base, timeout=5, model="cfg-model", system_prompt="只输出中文"
    )
    assert len(received) == 1  # 显式 model：不查 /v1/models
    body = received[0]["body"]
    assert body["model"] == "cfg-model"
    assert body["messages"][0] == {"role": "system", "content": "只输出中文"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_empty_system_prompt_skips_system_message(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5, system_prompt="")
    assert received[0]["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_thinking_disabled_by_default(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5)
    body = received[0]["body"]
    assert body["enable_thinking"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # LM Studio 等本地服务忽略 enable_thinking，需同时声明才能真正关闭思考
    assert body["reasoning_effort"] == "none"


def test_enable_thinking_forwarded(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5, enable_thinking=True)
    body = received[0]["body"]
    assert body["enable_thinking"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert "reasoning_effort" not in body  # 开启时不干预模型的思考挡位


def test_custom_reasoning_effort_forwarded(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5, reasoning_effort="low")
    assert received[0]["body"]["reasoning_effort"] == "low"


def test_empty_reasoning_effort_omits_field(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5, reasoning_effort="")
    assert "reasoning_effort" not in received[0]["body"]


def test_api_key_header_added_when_configured(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5, api_key="sk-test-123")
    assert received[0]["authorization"] == "Bearer sk-test-123"


def test_api_key_header_absent_without_config(stub_server):
    base, received = stub_server
    translator.translate_text("hi", base_url=base, timeout=5)
    assert received[0]["authorization"] is None


def test_api_key_forwarded_to_models_lookup(stub_server):
    base, received = stub_server
    _StubHandler.models = [{"id": "only-model"}]
    translator.translate_text("hi", base_url=base, timeout=5, api_key="sk-test-123")
    # 未显式配置 model 时先查 /v1/models（GET 也应带鉴权头）
    assert received[0]["body"]["model"] == "only-model"


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


# ---- 备选 API 链路：主链路不可达时自动降级 -------------------------------------


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # 关闭后该端口大概率无人监听
    return port


def test_endpoint_unavailable_is_translation_error_subclass():
    # 仅「不可达」类错误触发备选链路降级；它仍是 TranslationError 的子类
    assert issubclass(translator.EndpointUnavailable, translator.TranslationError)


def test_api_url_appends_v1_once():
    # 基址不含 /v1：照常追加；基址已含 /v1（如阿里云 compatible-mode）：
    # 不重复追加，否则拼出 /v1/v1/... 被网关以空体 404 拒绝（实测）
    assert translator._api_url("http://h:1234", "/chat/completions") == (
        "http://h:1234/v1/chat/completions"
    )
    assert translator._api_url("http://h:1234/", "/chat/completions") == (
        "http://h:1234/v1/chat/completions"
    )
    assert translator._api_url("https://x/compatible-mode/v1", "/chat/completions") == (
        "https://x/compatible-mode/v1/chat/completions"
    )
    assert translator._api_url("https://x/compatible-mode/v1", "/models") == (
        "https://x/compatible-mode/v1/models"
    )


def test_translate_text_with_v1_suffixed_base(stub_server):
    # 基址自带 /v1：请求路径不重复追加，往返正常
    base, received = stub_server
    result = translator.translate_text(
        "hi", base_url=f"{base}/v1", timeout=5, model="m"
    )
    assert result == "你好，世界"
    assert received[0]["path"] == "/v1/chat/completions"


def test_translate_text_falls_back_to_standby(stub_server):
    base, received = stub_server
    dead = f"http://127.0.0.1:{_closed_port()}"
    result = translator.translate_text(
        "hi",
        base_url=dead,
        timeout=5,
        base_url_stanby=base,
        model_stanby="stanby-model",
    )
    assert result == "你好，世界"  # 备选链路完成翻译
    assert len(received) == 1  # model_stanby 显式：跳过备选端点的模型发现
    assert received[0]["body"]["model"] == "stanby-model"


def test_translate_text_standby_auto_model_and_key(stub_server):
    base, received = stub_server
    dead = f"http://127.0.0.1:{_closed_port()}"
    result = translator.translate_text(
        "hi", base_url=dead, timeout=5, base_url_stanby=base, api_key_stanby="sk-stanby"
    )
    assert result == "你好，世界"
    # model_stanby 留空：备选端点同样自动发现模型；鉴权用 api_key_stanby
    assert received[0]["body"]["model"] == "stub-model"
    assert received[0]["authorization"] == "Bearer sk-stanby"


def test_translate_text_without_standby_raises():
    dead = f"http://127.0.0.1:{_closed_port()}"
    with pytest.raises(translator.EndpointUnavailable, match="无法连接"):
        translator.translate_text("hi", base_url=dead, timeout=2)  # 未启用备选链路


def test_translate_text_http_error_does_not_fallback(stub_server):
    # 服务可达但 HTTP 错误（404）不属于「不可达」：不切换备选链路
    base, received = stub_server
    with pytest.raises(translator.TranslationError, match="HTTP 404"):
        translator.translate_text(
            "hi",
            base_url=f"{base}/wrong-prefix",
            timeout=5,
            model="m",  # 跳过模型发现，直接命中 404
            base_url_stanby=base,
        )
    # 仅主链路被访问一次（wrong-prefix 路径 404），备选链路未被访问
    assert len(received) == 1
    assert received[0]["path"] == "/wrong-prefix/v1/chat/completions"


def test_translate_text_standby_also_unreachable_raises_last():
    dead = f"http://127.0.0.1:{_closed_port()}"
    with pytest.raises(translator.EndpointUnavailable, match="无法连接"):
        translator.translate_text(
            "hi", base_url=dead, timeout=2, base_url_stanby=dead
        )
