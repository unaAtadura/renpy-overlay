"""Shazam 听歌识曲：shazamio 异步识别的同步封装 + 结果提取/展示的纯函数。

shazamio 及其依赖链（aiohttp、pydub、numpy 等）较重且按需使用，全部延迟到
``recognize()`` 内部导入；未安装时 ``SHAZAMIO_AVAILABLE`` 为 False，调用方
据此降级提示，不影响主程序启动。
"""

from __future__ import annotations

import asyncio
import logging
from importlib.util import find_spec
from pathlib import Path

logger = logging.getLogger("renpy_overlay.song_recognition.recognizer")

#: shazamio 是否可用（模块导入时确定，运行期不变）
SHAZAMIO_AVAILABLE = find_spec("shazamio") is not None


def recognize(audio_path: Path) -> dict | None:
    """同步识别歌曲：内部用独立事件循环执行 shazamio 的异步识别。

    Args:
        audio_path: 临时 wav 文件路径

    Returns:
        Shazam 的 track 字典（title/subtitle 等），失败返回 None
    """
    if not SHAZAMIO_AVAILABLE:
        logger.error("shazamio 未安装，无法识别歌曲")
        return None
    if not audio_path.exists():
        logger.error("音频文件不存在：%s", audio_path)
        return None

    try:
        from shazamio import Shazam

        logger.info("正在使用 Shazam 识别音频：%s", audio_path)
        out = asyncio.run(Shazam().recognize(audio_path.as_posix()))
    except Exception:
        logger.exception("Shazam 识别过程发生错误")
        return None

    if not out:
        logger.warning("Shazam 未返回任何结果")
        return None
    track = out.get("track")
    if not track:
        # 响应里没有 track 字段：通常是采样不足或该曲目未被 Shazam 收录
        logger.info("未识别到歌曲信息（响应无 track 字段）")
        logger.debug("完整响应：%s", out)
        return None
    return track


def extract_track_info(track: dict | None) -> tuple[str, str]:
    """从 Shazam track 字典提取 (歌曲名, 艺术家)；字段缺失回退「未知」。"""
    if not isinstance(track, dict):
        return ("未知", "未知")
    title = str(track.get("title") or "").strip() or "未知"
    artist = str(track.get("subtitle") or "").strip() or "未知"
    return (title, artist)


def format_success_body(title: str, artist: str) -> str:
    """识曲成功的正文展示文本（需求：歌曲名与艺术家各占一行）。"""
    return f"歌曲名: {title}\n艺术家: {artist}\n"


def format_fail_body() -> str:
    """识曲失败的正文展示文本（需求固定文案，含多次失败的说明）。"""
    return "识曲失败。\n多次失败可能是这首歌没有被收录。\n"
