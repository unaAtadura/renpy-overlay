"""截图识图客户端：OpenAI 兼容 vision 接口（图片 base64 → 中文译文 / 文字原文）。

与 :mod:`renpy_overlay.translator` 同风格：只依赖标准库 ``urllib``、不含任何
线程 / GUI 逻辑，请求由调用方在后台线程执行。请求体是 OpenAI 多模态
``image_url + text`` 结构，思考模式的三方方言字段与 ``translator._chat_payload``
保持同一语义（LM Studio 等本地服务需要 ``reasoning_effort`` 才能真正关闭思考）。

两条平级链路互不影响：:func:`translate_image` 识图翻译为中文，
:func:`recognize_image` 只识别返回图中文字原文（供原文译文对照等后续功能复用）。
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

#: 首次请求指令：不提"保持原始格式/顺序"（实测 qwen3.5-vl 对多行诗体会被这句
#: 带偏，只输出保持原文格式的识别结果而不翻译，表现为"返回英文"）
DEFAULT_SCREENSHOT_PROMPT = (
    "把图片中的全部文字翻译成简体中文。"
    "只输出中文译文本身，不要输出原文，不要解释，不要引号。"
)

#: 结果校验未通过（译文疑似未翻译）时的强化重试指令
RETRY_SCREENSHOT_PROMPT = (
    "图中的文字是外文。请把图中全部文字翻译成简体中文，"
    "只输出中文译文，禁止输出原文，禁止保持原文格式。"
)

#: 识别原文指令：与翻译指令明确区分——只输出图中文字原文，不翻译、不改写、
#: 不添加解释，避免模型自行翻译或改写（供原文译文对照等后续功能复用）
DEFAULT_OCR_PROMPT = (
    "把图片中的全部文字原样输出。"
    "只输出图中文字的原文，不翻译，不改写，不添加解释，不要引号。"
)

#: 译文中 CJK 字符占比低于该阈值时判定为"疑似未翻译"：正常中文译文几乎全
#: 为 CJK，允许少量英文专名（如 Sicae/legacy）混入，取 0.25 留足余量
UNTRANSLATED_CJK_RATIO = 0.25


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


def looks_untranslated(text: str, min_cjk_ratio: float = UNTRANSLATED_CJK_RATIO) -> bool:
    """判断译文是否"疑似只识别未翻译"（纯函数，便于离线单测）。

    依据：非空白字符中 CJK 字符占比。正常中文译文几乎全为 CJK（英文专名
    占比很低），而英文原文占比接近 0；空文本同样视为未翻译。
    """
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return True
    cjk = sum(1 for ch in chars if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(chars) < min_cjk_ratio


def translate_image_verified(jpeg_base64: str, abort_check=None, **call_kwargs) -> str:
    """带结果校验的识图翻译：译文疑似未翻译时用强化指令自动重试一次。

    模型对"识别+翻译"混合指令存在非确定行为（实测同一段诗体英文有时只输出
    识别结果），首条指令已去掉"保持原始格式"诱因；此函数再加一层 CJK 占比
    校验：未通过则用 :data:`RETRY_SCREENSHOT_PROMPT` 重试一次，重试结果无论
    是否通过都返回并记 WARNING（不无限重试，同一请求最多两次 API 调用）。

    ``abort_check``：返回 True 时放弃重试（直接返回首次结果），由调用方传入
    中止信号（如 ``stop.is_set``），语义与参考实现 ``llm_service`` 一致。
    """
    instruction = call_kwargs.pop("instruction", DEFAULT_SCREENSHOT_PROMPT)
    text = translate_image(jpeg_base64, instruction=instruction, **call_kwargs)
    if not looks_untranslated(text):
        return text
    if abort_check is not None and abort_check():
        logger.warning("译文疑似未翻译且请求已作废，不重试：%r", text[:50])
        return text
    logger.warning(
        "译文疑似未翻译（CJK 占比过低），用强化指令重试一次：%r", text[:50]
    )
    retried = translate_image(
        jpeg_base64, instruction=RETRY_SCREENSHOT_PROMPT, **call_kwargs
    )
    if looks_untranslated(retried):
        logger.warning("强化指令重试后仍疑似未翻译，保留重试结果：%r", retried[:50])
    else:
        logger.info("强化指令重试成功，已获得中文译文")
    return retried


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


def recognize_image(
    jpeg_base64: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = "",
    api_key: str = "",
    enable_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    instruction: str = DEFAULT_OCR_PROMPT,
) -> str:
    """识图 + 识别原文：把 base64 JPEG 发给 vision 模型，返回图中文字原文。

    与 :func:`translate_image` 平级的独立链路（只识别、不翻译）：请求体结构、
    参数与错误处理完全一致，仅默认指令不同——:data:`DEFAULT_OCR_PROMPT` 强调
    只输出原文、不翻译不改写。翻译流程互不影响：调用方分别调用两个接口即可
    同时获得原文与译文。空结果 / 响应缺字段抛 :class:`TranslationError`。
    """
    if not jpeg_base64:
        raise ValueError("没有可识别的图片数据")
    logger.info("发起截图识别原文请求（只识别、不翻译）")
    return translate_image(
        jpeg_base64,
        base_url=base_url,
        timeout=timeout,
        model=model,
        api_key=api_key,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        instruction=instruction,
    )
