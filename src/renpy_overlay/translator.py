"""调用本地 LM Studio（OpenAI 兼容协议）把游戏对话翻译成中文。

只依赖标准库 ``urllib``，且不含任何线程 / Tk 逻辑：请求由调用方在后台线程里执行，
本模块只负责"发请求 → 校验响应 → 返回译文"这一段纯逻辑，便于离线单测
（``tests/test_translator.py`` 用本地 HTTP 桩验证往返与错误路径）。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("renpy_overlay.translator")

DEFAULT_BASE_URL = "http://127.0.0.1:1234"
DEFAULT_TIMEOUT = 20.0
DEFAULT_SYSTEM_PROMPT = (
    "你是游戏对话翻译器。把用户给出的游戏对话翻译成简体中文，"
    "只输出译文本身，不要解释、不要引号，保留原有语气与标点风格。"
)


class TranslationError(RuntimeError):
    """翻译请求失败（服务不可达、协议不符、响应缺字段等）。"""


def _request_json(url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200]
        raise TranslationError(f"HTTP {exc.code}：{detail!r}") from exc
    except urllib.error.URLError as exc:
        raise TranslationError(f"无法连接 {url}：{exc.reason}") from exc
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise TranslationError(f"响应不是合法 JSON：{body[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise TranslationError(f"响应不是 JSON 对象：{body[:200]!r}")
    return parsed


def first_model(base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> str:
    """``GET /v1/models`` 取第一个模型 id（LM Studio 至少加载了一个模型时可用）。"""
    data = _request_json(f"{base_url.rstrip('/')}/v1/models", None, timeout)
    models = data.get("data")
    if not isinstance(models, list) or not models:
        raise TranslationError("LM Studio 未返回任何已加载模型（/v1/models 为空）")
    first = models[0]
    model_id = str(first.get("id") or "") if isinstance(first, dict) else ""
    if not model_id:
        raise TranslationError(f"模型条目缺少 id：{first!r}")
    return model_id


def translate_text(
    text: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = "",
) -> str:
    """把 ``text`` 翻译成简体中文并返回译文。

    空文本直接抛 ``ValueError``（调用方应先行跳过）；``model`` 为空时先查
    ``/v1/models`` 取第一个已加载模型。
    """
    if not text or not text.strip():
        raise ValueError("没有可翻译的文本")
    if not model:
        model = first_model(base_url, timeout)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    data = _request_json(f"{base_url.rstrip('/')}/v1/chat/completions", payload, timeout)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslationError(f"响应缺少 choices[0].message.content：{str(data)[:200]}") from exc
    result = str(content or "").strip()
    if not result:
        raise TranslationError("模型返回了空译文")
    return result
