"""语音识别客户端：OpenAI 兼容 chat/completions + input_audio（qwen-omni 形态）。

选型（需求确认）：与视觉/文本链路同构的 ``chat/completions`` 请求体，音频
以 ``input_audio`` base64 内嵌（全模态 chat 模型形态），而非独立
``/audio/transcriptions`` 端点。

与 :mod:`renpy_overlay.screenshot.vision` 同风格：只依赖标准库 ``urllib``、
不含任何线程 / GUI 逻辑，请求由调用方后台线程执行。三键配置
（``api_base_url_asr`` / ``model_asr`` / ``api_key_asr``）空值回退主链路
（:func:`resolve_asr_options`），无备选模型（需求）。空结果 / 响应缺字段抛
:class:`TranslationError`；连接失败/超时抛 :class:`EndpointUnavailable`。
"""

from __future__ import annotations

import base64
import logging

from ..translator import (
    DEFAULT_BASE_URL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_TIMEOUT,
    EndpointUnavailable,
    TranslationError,
    _api_url,
    _request_json,
)

logger = logging.getLogger("renpy_overlay.video.asr")

__all__ = [
    "DEFAULT_INSTRUCTION",
    "EndpointUnavailable",
    "TranslationError",
    "audio_content",
    "recognize_audio",
    "resolve_asr_options",
]

#: 转写指令：只输出语音文本本身（需求：识别结果直接上屏）
DEFAULT_INSTRUCTION = (
    "请把这段音频中的语音内容逐字转写为文本。"
    "只输出转写文本本身，不要解释，不要引号。"
)


def resolve_asr_options(app_config) -> tuple[str, str, str]:
    """ASR 三键回退链（纯函数，离线测试用）：空值跟随主链路（与 vision 同构）。

    返回 ``(base_url, model, api_key)``；模型不自动发现（ASR 需求无备选，
    留空时由调用方直接使用主链路模型）。
    """
    return (
        app_config.api_base_url_asr or app_config.api_base_url,
        app_config.model_asr or app_config.model,
        app_config.api_key_asr or app_config.api_key,
    )


def audio_content(wav_bytes: bytes, audio_format: str = "wav") -> dict:
    """构造 ``input_audio`` 消息段（纯函数）：wav 字节 → base64 内嵌。"""
    return {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(wav_bytes).decode("ascii"),
            "format": audio_format,
        },
    }


def recognize_audio(
    wav_bytes: bytes,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    model: str = "",
    api_key: str = "",
    enable_thinking: bool = False,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    instruction: str = DEFAULT_INSTRUCTION,
) -> str:
    """把 wav 音频发给全模态模型转写，返回语音文本。

    请求体结构与 vision 链路同构（``chat/completions``，非流式）；思考模式
    方言与 ``translator._chat_payload`` 一致（``enable_thinking`` +
    ``chat_template_kwargs``，未开启思考时附 ``reasoning_effort``）。
    """
    if not wav_bytes:
        raise ValueError("没有可识别的音频数据")
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    audio_content(wav_bytes),
                    {"type": "text", "text": instruction},
                ],
            }
        ],
        "temperature": 0,
        "stream": False,
        "enable_thinking": bool(enable_thinking),
        "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
    }
    if not enable_thinking and reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if model:
        payload["model"] = model
    logger.info(
        "发起语音识别请求（model=%s，音频 %d 字节）", model or "<未指定>", len(wav_bytes)
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
        raise TranslationError("模型返回了空转写文本")
    logger.info("语音识别完成（文本 %d 字）", len(result))
    return result
