"""调用 OpenAI 兼容服务（LM Studio / OpenAI / DeepSeek 等）把游戏对话翻译成中文。

只依赖标准库 ``urllib``，且不含任何线程 / Tk 逻辑：请求由调用方在后台线程里执行，
本模块只负责“发请求 → 校验响应 → 返回译文”这一段纯逻辑，便于离线单测
（``tests/test_translator.py`` 用本地 HTTP 桩验证往返与错误路径）。

连接参数（地址 / 超时 / 模型 / 系统提示词 / API Key）均可由调用方注入（来自
``config.json``）；模块级常量保留为默认兜底，未传参时与历史行为完全一致。
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator

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


class EndpointUnavailable(TranslationError):
    """服务端点不可达（连接失败/超时）。

    与普通请求失败（HTTP 错误、响应协议不符等）区分：仅这类错误会触发
    备选链路降级（服务可达但请求出错时，换链路重试无意义）。
    """


def _api_url(base_url: str, path: str) -> str:
    """拼接 OpenAI 兼容端点 URL（``path`` 如 ``/chat/completions`` / ``/models``）。

    基址以 ``/v1`` 结尾时（如阿里云 compatible-mode 的 ``.../compatible-mode/v1``）
    不重复追加 ``/v1``——否则拼出 ``/v1/v1/...``，网关直接返回空体 404（实测）。
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{path}"
    return f"{base}/v1{path}"


def _chat_headers(api_key: str) -> dict[str, str]:
    """OpenAI 兼容请求头；api_key 为空时不附加鉴权头（本地服务无需鉴权）。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _request_json(url: str, payload: dict | None, timeout: float, api_key: str = "") -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=_chat_headers(api_key),
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200]
        raise TranslationError(f"HTTP {exc.code}：{detail!r}") from exc
    except urllib.error.URLError as exc:
        raise EndpointUnavailable(f"无法连接 {url}：{exc.reason}") from exc
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
    data = _request_json(_api_url(base_url, "/models"), None, timeout, api_key)
    models = data.get("data")
    if not isinstance(models, list) or not models:
        raise TranslationError("LM Studio 未返回任何已加载模型（/v1/models 为空）")
    first = models[0]
    model_id = str(first.get("id") or "") if isinstance(first, dict) else ""
    if not model_id:
        raise TranslationError(f"模型条目缺少 id：{first!r}")
    return model_id


def _chat_payload(
    text: str,
    model: str,
    system_prompt: str,
    enable_thinking: bool,
    reasoning_effort: str,
    stream: bool,
) -> dict:
    """构造 /v1/chat/completions 请求体（流式与非流式共用同一套字段）。"""
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": text})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": stream,
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
    return payload


def _open_stream(url: str, payload: dict, timeout: float, api_key: str):
    """发起流式 POST 并返回未读完的响应对象（SSE 逐行迭代由调用方负责）。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**_chat_headers(api_key), "Accept": "text/event-stream"},
        method="POST",
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200]
        raise TranslationError(f"HTTP {exc.code}：{detail!r}") from exc
    except urllib.error.URLError as exc:
        raise EndpointUnavailable(f"无法连接 {url}：{exc.reason}") from exc


def translate_text(
    text: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    api_key: str = "",
    enable_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    base_url_stanby: str = "",
    api_key_stanby: str = "",
    model_stanby: str = "",
) -> str:
    """把 ``text`` 翻译成简体中文并返回译文。

    所有连接参数均可在调用时注入（来自 ``config.json``），未传入时回退模块级
    默认常量：``model`` 为空则先查 ``/v1/models`` 取第一个已加载模型（显式配置
    则直用）；``system_prompt`` 为空则不附加 system 消息；``api_key`` 非空时
    附带 ``Authorization: Bearer <key>``；``enable_thinking`` 控制模型思考
    （reasoning）模式，默认关闭，经多平台方言一并声明；关闭思考时使用的
    ``reasoning_effort``（默认 ``"none"``，LM Studio 等本地服务靠它真正关闭）
    同样可注入。空文本抛 ``ValueError``（调用方应先跳过）。

    备选链路：``base_url_stanby`` 非空时启用——主链路发生
    :class:`EndpointUnavailable`（连接失败/超时）时自动切换到备选端点重试
    （鉴权用 ``api_key_stanby``、模型用 ``model_stanby``，留空则同样自动
    发现）；备选端点也失败则抛出末次异常。服务可达但请求出错（HTTP 错误、
    响应缺字段等）不触发切换。
    """
    if not text or not text.strip():
        raise ValueError("没有可翻译的文本")

    def _request(base_url: str, api_key: str, model: str) -> str:
        model = model or first_model(base_url, timeout, api_key)
        payload = _chat_payload(
            text, model, system_prompt, enable_thinking, reasoning_effort, False
        )
        data = _request_json(
            _api_url(base_url, "/chat/completions"), payload, timeout, api_key
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(
                f"响应缺少 choices[0].message.content：{str(data)[:200]}"
            ) from exc
        result = str(content or "").strip()
        if not result:
            raise TranslationError("模型返回了空译文")
        return result

    try:
        return _request(base_url, api_key, model)
    except EndpointUnavailable:
        if not base_url_stanby:
            raise
        logger.warning(
            "主链路不可达（%s），切换备选链路（%s）", base_url, base_url_stanby
        )
        return _request(base_url_stanby, api_key_stanby, model_stanby)


def translate_text_stream(
    text: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = "",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    api_key: str = "",
    enable_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    base_url_stanby: str = "",
    api_key_stanby: str = "",
    model_stanby: str = "",
) -> Iterator[str]:
    """流式版 ``translate_text``：逐块产出模型增量输出（SSE，``stream=True``）。

    返回生成器：首次迭代时才真正发起请求（模型自动发现也在此时进行）；每个
    ``yield`` 是一段增量译文，迭代正常结束即翻译完整结束。请求体字段与
    ``translate_text`` 完全一致（含思考模式三方言语义），仅流式开关不同；
    思考型模型输出的 reasoning 内容不在 ``delta.content`` 中，天然被跳过。
    空 ``delta`` / 空块 / 非 JSON 行一律跳过；``data: [DONE]`` 结束。连接与
    协议错误抛 :class:`TranslationError`；迭代中途连接中断同样收敛为该异常。

    备选链路：``base_url_stanby`` 非空时启用——主链路建连阶段（模型自动
    发现 / 发起流式请求）发生 :class:`EndpointUnavailable` 时自动切换到
    备选端点；切换只发生在建连阶段，流式迭代中途断开不切换（避免重复
    输出前半段）。
    """
    if not text or not text.strip():
        raise ValueError("没有可翻译的文本")

    def _open(url: str, api_key: str, model: str):
        model = model or first_model(url, timeout, api_key)
        payload = _chat_payload(
            text, model, system_prompt, enable_thinking, reasoning_effort, True
        )
        return _open_stream(
            _api_url(url, "/chat/completions"), payload, timeout, api_key
        )

    try:
        response = _open(base_url, api_key, model)
    except EndpointUnavailable:
        if not base_url_stanby:
            raise
        logger.warning(
            "主链路不可达（%s），切换备选链路（%s）", base_url, base_url_stanby
        )
        response = _open(base_url_stanby, api_key_stanby, model_stanby)
    with response:
        try:
            for raw in response:  # 按行阻塞读取，每行尽快产出，降低显示延迟
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)
        except (http.client.HTTPException, OSError) as exc:
            # URLError / socket.timeout / IncompleteRead 等都归入这一族
            raise TranslationError(f"流式响应中断：{exc}") from exc
