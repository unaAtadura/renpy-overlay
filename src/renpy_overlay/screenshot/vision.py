"""截图识别翻译：OpenAI 兼容 vision 接口（图片 base64 → 中文译文）。

与 :mod:`renpy_overlay.translator` 同风格：只依赖标准库 ``urllib``、不含任何
线程 / GUI 逻辑，请求由调用方在后台线程执行。请求体是 OpenAI 多模态
``image_url + text`` 结构，思考模式的三方方言字段与 ``translator._chat_payload``
保持同一语义（LM Studio 等本地服务需要 ``reasoning_effort`` 才能真正关闭思考）。
"""

from __future__ import annotations

import logging

from ..translator import (
    DEFAULT_BASE_URL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TIMEOUT,
    TranslationError,
    _request_json,
    first_model,
)

logger = logging.getLogger("renpy_overlay.screenshot.vision")

DEFAULT_SCREENSHOT_PROMPT = (
    "请识别图片中的所有文字，保持原始格式和顺序，然后将识别结果翻译成简体中文。"
    "只输出译文本身，不要解释、不要引号。"
)


def _image_payload(jpeg_base64: str, instruction: str) -> dict:
    """构造 OpenAI 兼容多模态请求体（非流式；model 由调用方补齐）。"""
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{jpeg_base64}"},
                    },
                    {"type": "text", "text": instruction},
                ],
            }
        ],
        "temperature": 0.2,
        "stream": False,
    }


def translate_image(
    jpeg_base64: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = "",
    api_key: str = "",
    enable_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    instruction: str = DEFAULT_SCREENSHOT_PROMPT,
) -> str:
    """识图 + 翻译：把 base64 JPEG 发给 vision 模型，返回中文译文。

    ``model`` 为空时先向 ``/v1/models`` 取第一个已加载模型（与文本翻译一致）；
    ``enable_thinking=False`` 时按三方方言一并声明关闭思考（同
    ``translator._chat_payload``）。响应缺字段 / 空译文抛 :class:`TranslationError`。
    """
    if not jpeg_base64:
        raise ValueError("没有可识别的图片数据")
    if not model:
        model = first_model(base_url, timeout, api_key)
    payload = _image_payload(jpeg_base64, instruction)
    payload["model"] = model
    # 思考模式开关（默认关闭）：与文本翻译同一套三方方言，见 translator._chat_payload
    payload["enable_thinking"] = bool(enable_thinking)
    payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    if not enable_thinking and reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    logger.info("发起截图识别翻译请求（model=%s，图片 %d 字节 base64）", model, len(jpeg_base64))
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    data = _request_json(url, payload, timeout, api_key)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TranslationError(f"响应缺少 choices[0].message.content：{str(data)[:200]}") from exc
    result = str(content or "").strip()
    if not result:
        raise TranslationError("模型返回了空译文")
    logger.info("截图识别翻译完成（译文 %d 字）", len(result))
    return result
