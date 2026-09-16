"""调用 OpenAI 兼容服务（LM Studio / OpenAI / DeepSeek 等）把游戏对话翻译成中文。

只依赖标准库 ``urllib``，且不含任何线程 / Tk 逻辑：请求由调用方在后台线程里执行，
本模块只负责“发请求 → 校验响应 → 返回译文”这一段纯逻辑，便于离线单测
（``tests/test_translator.py`` 用本地 HTTP 桩验证往返与错误路径）。

连接参数（地址 / 超时 / 模型 / 系统提示词 / API Key）均可由调用方注入（来自
``config.json``）；模块级常量保留为默认兜底，未传参时与历史行为完全一致。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger("renpy_overlay.translator")

DEFAULT_BASE_URL = "http://127.0.0.1:1234"
DEFAULT_TIMEOUT = 20.0
DEFAULT_REASONING_EFFORT = "none"  # 关闭思考时使用的 reasoning_effort 取值
DEFAULT_SYSTEM_PROMPT = (
    "你是游戏对话翻译器。把用户给出的游戏对话翻译成简体中文，"
    "只输出译文本身，不要解释、不要引号，保留原有语气与标点风格。"
)


class TranslationError(RuntimeError):
    """翻译请求失败（服务不可达、协议不符、响应缺字段等）。"""


def _request_json(url: str, payload: dict | None, timeout: float, api_key: str = "") -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        # OpenAI 兼容鉴权头；为空时不附加，本地服务（LM Studio 等）无需鉴权
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
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


def first_model(
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    api_key: str = "",
) -> str:
    """``GET /v1/models`` 取第一个模型 id（LM Studio 至少加载了一个模型时可用）。"""
    data = _request_json(f"{base_url.rstrip('/')}/v1/models", None, timeout, api_key)
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
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    api_key: str = "",
    enable_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
) -> str:
    """把 ``text`` 翻译成简体中文并返回译文。

    所有连接参数均可在调用时注入（来自 ``config.json``），未传入时回退模块级
    默认常量：``model`` 为空则先查 ``/v1/models`` 取第一个已加载模型（显式配置
    则直用）；``system_prompt`` 为空则不附加 system 消息；``api_key`` 非空时
    附带 ``Authorization: Bearer <key>``；``enable_thinking`` 控制模型思考
    （reasoning）模式，默认关闭，经多平台方言一并声明；关闭思考时使用的
    ``reasoning_effort``（默认 ``"none"``，LM Studio 等本地服务靠它真正关闭）
    同样可注入。空文本抛 ``ValueError``（调用方应先跳过）。
    """
    if not text or not text.strip():
        raise ValueError("没有可翻译的文本")
    if not model:
        model = first_model(base_url, timeout, api_key)
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    # 思考模式开关（默认关闭）：各兼容平台的参数方言不同，一并声明同一语义 ——
    #   enable_thinking      ：Qwen Cloud / DashScope / Qwen3.5 兼容端点（顶层字段）
    #   chat_template_kwargs ：vLLM / SGLang（注入 chat template 变量）
    #   reasoning_effort     ：LM Studio 等本地服务会忽略 enable_thinking，
    #                          实测需同时声明 "none" 才能真正关闭思考（参考 renpybox）
    thinking = bool(enable_thinking)
    payload["enable_thinking"] = thinking
    payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    if not thinking and reasoning_effort:
        # 取值来自 config.json（默认 "none"）；配置为空串则不发送该字段
        payload["reasoning_effort"] = reasoning_effort
    data = _request_json(f"{base_url.rstrip('/')}/v1/chat/completions", payload, timeout, api_key)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslationError(f"响应缺少 choices[0].message.content：{str(data)[:200]}") from exc
    result = str(content or "").strip()
    if not result:
        raise TranslationError("模型返回了空译文")
    return result
