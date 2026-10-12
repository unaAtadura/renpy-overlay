"""实时语音识别显示文本与流式替换的纯计算。

全部为纯函数，便于离线测试（见 ``tests/test_stream_window.py``）：
``compose_live_text`` 组合实时识别全文，``plan_stream_replace`` 差分
计算流式替换计划（供 ``BodyWindow.replace_stream`` 使用）。
"""

from __future__ import annotations

#: 句末符号（实时语音识别：结尾为这些符号时主动换行；中英文句号/问号/感叹号）
SENTENCE_END_PUNCTUATION = "。．.？！?!"


def has_sentence_end(text: str) -> bool:
    """文本是否以句末符号结尾（纯函数，离线测试用）。"""
    return bool(text) and text[-1] in SENTENCE_END_PUNCTUATION


def strip_asr_line_breaks(text: str) -> str:
    """删除语音识别返回文本中的换行符（纯函数，离线测试用）。

    需求：句末符号后已带换行符的，删除该换行符（换行由工具侧主动控制，
    防止与主动换行叠加出双换行）；非句末处的换行符一并删除（已确认决议），
    保证「整段连续文字」观感。
    """
    return text.replace("\r", "").replace("\n", "")


def compose_live_text(finals: list[str], current: str) -> str:
    """组合实时语音识别的显示全文（纯函数，离线测试用）。

    已定稿句逐句换行；当前句原样展示、以句末符号结尾时主动补换行
    （需求：结尾为中英文句号/问号/感叹号等句末符号时主动换行）。输入句
    先经 :func:`strip_asr_line_breaks` 归一化。
    """
    parts = [s + "\n" for s in map(strip_asr_line_breaks, finals) if s]
    cleaned = strip_asr_line_breaks(current)
    if cleaned:
        parts.append(cleaned)
        if has_sentence_end(cleaned):
            parts.append("\n")
    return "".join(parts)


def plan_stream_replace(old_text: str, typed_count: int, new_text: str) -> tuple[int, str]:
    """计算正文流式替换计划（纯函数，离线测试用）。

    返回 ``(撤销的已打字数, 新待显示文本)``：new 与 old 的公共前缀保留
    已排版字形；前缀之后旧内容全部撤销（已打字部分计入撤销数，未打字
    部分直接丢弃），待显示文本从撤销点起重喂。纯追加时撤销 0 字。
    """
    if new_text.startswith(old_text):
        return 0, new_text[typed_count:]
    limit = min(len(old_text), len(new_text))
    common = 0
    while common < limit and old_text[common] == new_text[common]:
        common += 1
    untype = max(0, typed_count - common)
    return untype, new_text[typed_count - untype:]
