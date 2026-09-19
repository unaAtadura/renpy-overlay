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
from renpy_overlay.screenshot.vision import looks_untranslated, translate_image_verified

NO_CHOICES = "__no_choices__"
EMPTY_TEXT = "__empty_text__"
ENGLISH_SAMPLE = "Sicae was the pride of the kingdom, their banners fluttering in the wind."


class _StubHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    models: list[dict] = [{"id": "stub-model"}]
    english_first = False  # True：首个 POST 返回英文原文（模拟模型未翻译），之后返回中文

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
        elif "不翻译" in instruction:
            # 识别原文指令：模型按指令原样返回图中文字（英文样例）
            self._send(200, {"choices": [{"message": {"content": ENGLISH_SAMPLE}}]})
        elif type(self).english_first and len(type(self).received) == 1:
            self._send(200, {"choices": [{"message": {"content": ENGLISH_SAMPLE}}]})
        else:
            self._send(200, {"choices": [{"message": {"content": "识别到的中文译文"}}]})


@pytest.fixture()
def stub_server():
    _StubHandler.received = []
    _StubHandler.models = [{"id": "stub-model"}]
    _StubHandler.english_first = False
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


# ---- looks_untranslated：CJK 占比校验（纯函数） -------------------------------


def test_looks_untranslated():
    assert not looks_untranslated("西凯是王国的骄傲，他们的旗帜在风中飘扬。")
    assert not looks_untranslated("称为Sicae，王国的守护者。")  # 少量英文专名不算未翻译
    assert looks_untranslated(ENGLISH_SAMPLE)
    assert looks_untranslated("Sicae was proud.\nTheir banners fluttered.")
    assert looks_untranslated("   ")  # 空白文本视为未翻译
    # 阈值边界：CJK 占比恰好等于 0.25 时不算未翻译（2 CJK / 8 非空白）
    assert not looks_untranslated("西凯a b c d e f")
    assert looks_untranslated("西凯a b c d e f g")  # 2/9 < 0.25：判定未翻译


def test_verified_passes_through_translated_text(stub_server):
    base, received = stub_server
    result = translate_image_verified(_tiny_base64(), base_url=base, timeout=5, model="m")
    assert result == "识别到的中文译文"
    assert len(received) == 1  # 校验通过：不重试


def test_verified_retries_with_strict_instruction(stub_server):
    base, received = stub_server
    _StubHandler.english_first = True
    result = translate_image_verified(_tiny_base64(), base_url=base, timeout=5, model="m")
    assert result == "识别到的中文译文"  # 重试后拿到中文
    assert len(received) == 2  # 首次 + 强化重试
    retry_instruction = received[1]["body"]["messages"][0]["content"][-1]["text"]
    assert "禁止输出原文" in retry_instruction


def test_verified_retry_aborted_returns_first_result(stub_server):
    base, received = stub_server
    _StubHandler.english_first = True
    result = translate_image_verified(
        _tiny_base64(),
        base_url=base,
        timeout=5,
        model="m",
        abort_check=lambda: True,  # 请求已作废：不重试
    )
    assert result == ENGLISH_SAMPLE
    assert len(received) == 1


# ---- recognize_image：只识别原文、不翻译（与翻译链路平级） ---------------------


def test_ocr_prompt_distinct_from_translation_prompts():
    # 识别原文指令与翻译指令明确区分：强调只输出原文、不翻译不改写不解释
    assert vision.DEFAULT_OCR_PROMPT != vision.DEFAULT_SCREENSHOT_PROMPT
    assert "原文" in vision.DEFAULT_OCR_PROMPT
    assert "不翻译" in vision.DEFAULT_OCR_PROMPT
    assert "不改写" in vision.DEFAULT_OCR_PROMPT
    assert "不添加解释" in vision.DEFAULT_OCR_PROMPT
    assert "翻译成简体中文" in vision.DEFAULT_SCREENSHOT_PROMPT


def test_recognize_returns_source_without_translation(stub_server):
    base, received = stub_server
    result = vision.recognize_image(_tiny_base64(), base_url=base, timeout=5)
    assert result == ENGLISH_SAMPLE  # 原样返回图中文字，不做翻译
    assert len(received) == 1  # 单次请求：不做 CJK 校验、不重试（区别于 verified）
    instruction = received[0]["body"]["messages"][0]["content"][-1]["text"]
    assert "不翻译" in instruction  # 发出的是识别原文指令


def test_recognize_params_forwarded_like_translate(stub_server):
    base, received = stub_server
    vision.recognize_image(
        _tiny_base64(), base_url=base, timeout=5, model="m", enable_thinking=True
    )
    body = received[0]["body"]
    assert body["model"] == "m"
    assert body["stream"] is False
    assert body["enable_thinking"] is True
    assert "reasoning_effort" not in body


def test_recognize_custom_instruction_forwarded(stub_server):
    base, received = stub_server
    result = vision.recognize_image(
        _tiny_base64(), base_url=base, timeout=5, model="m", instruction="自定义指令"
    )
    assert received[0]["body"]["messages"][0]["content"][-1]["text"] == "自定义指令"
    assert result == "识别到的中文译文"  # 接口原样返回模型输出，不做二次加工


def test_recognize_missing_choices_raises(stub_server):
    base, _received = stub_server
    with pytest.raises(translator.TranslationError, match="choices"):
        vision.recognize_image(
            _tiny_base64(), base_url=base, timeout=5, model="m", instruction=NO_CHOICES
        )


def test_recognize_empty_text_raises(stub_server):
    base, _received = stub_server
    with pytest.raises(translator.TranslationError, match="空"):
        vision.recognize_image(
            _tiny_base64(), base_url=base, timeout=5, model="m", instruction=EMPTY_TEXT
        )


def test_recognize_empty_base64_rejected(stub_server):
    _base, received = stub_server
    with pytest.raises(ValueError, match="没有可识别的图片"):
        vision.recognize_image("")
    assert received == []


def test_recognize_exported_from_package_root():
    from renpy_overlay.screenshot import recognize_image

    assert recognize_image is vision.recognize_image
