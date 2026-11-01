"""ASS 字幕读写：双语构建（中文第一行/原文第二行）+ 解析映射。

制作字幕（future_*.txt）：逐切片 ASR 原文 → LLM 翻译中文 → 切片起止
换算字幕时间轴与持续时间 → ASS 追加写入（文件头一次 + Dialogue 逐
条）；中文第一行、原文第二行（``\\N`` 换行）；文件与视频同名。

加载字幕：解析同名 ASS → 每条字幕时间轴映射为一个切片标记（决策 6：
每切片一条字幕；加载只需时间轴，文本保留供扩展）。

时间格式：ASS 标准 ``h:mm:ss.cc``（厘秒点分，小时不补零）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SubtitleEntry:
    """单条字幕：时间轴 + 双语文本。"""

    start_ms: int
    end_ms: int
    text_zh: str  # 第一行：中文
    text_src: str  # 第二行：原文

    def render(self) -> str:
        lines = [self.text_zh]
        if self.text_src:
            lines.append(self.text_src)
        return "\n".join(lines)


#: 默认样式名（[V4+ Styles] 定义与 Dialogue 行 Style 字段保持一致）。
ASS_STYLE_NAME = "Default"

#: ASS 文件头：Script Info + V4+ Styles + Events Format（首写一次；
#: Dialogue 行经 :func:`build_ass_dialogue` 逐条追加）。UTF-8 编码落盘。
ASS_HEADER = f"""\
[Script Info]
; 双语字幕：中文第一行 / 原文第二行（\\N 换行）
Title: Bilingual Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {ASS_STYLE_NAME},Microsoft YaHei,64,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,2,80,80,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def format_ass_time(ms: int) -> str:
    """毫秒 → ``h:mm:ss.cc``（负值钳零；厘秒向下截断；小时不补零）。"""
    total = max(0, int(ms))
    hours, rem = divmod(total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, milli = divmod(rem, 1_000)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{milli // 10:02d}"


def parse_ass_time(text: str) -> int | None:
    """``h:mm:ss.cc``（或 ``,`` 分隔厘秒；小时可多位）→ 毫秒；非法 ``None``。"""
    text = (text or "").strip()
    head, sep, centis = text.partition(".")
    if not sep:
        head, sep, centis = text.partition(",")
        if not sep:
            return None
    parts = head.split(":")
    if len(parts) != 3:
        return None
    if not (centis.isdigit() and all(p.isdigit() for p in parts)):
        return None
    if len(centis) != 2:
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(centis) * 10


def escape_ass_text(text: str) -> str:
    """字幕文本 → ASS Text 字段安全文本。

    换行 → ``\\N``（硬换行）；``\\`` → ``\\\\``（防吞后续字符）；
    ``{``/``}`` → 全角（防注入 override 标签块被渲染器丢弃）。
    """
    out = []
    for ch in text or "":
        if ch == "\\":
            out.append("\\\\")
        elif ch == "{":
            out.append("｛")
        elif ch == "}":
            out.append("｝")
        elif ch == "\n":
            out.append("\\N")
        elif ch == "\r":
            continue  # \r\n 的 \r 由 \n 分支处理；孤立 \r 丢弃
        else:
            out.append(ch)
    return "".join(out)


def _unescape_ass_text(text: str) -> str:
    """ASS Text 字段 → 原文（单遍扫描，与 :func:`escape_ass_text` 互逆）。"""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in ("N", "n"):  # \N 硬换行（\n 软换行按换行还原）
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        if ch == "｛":
            out.append("{")
        elif ch == "｝":
            out.append("}")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def build_ass_dialogue(entry: SubtitleEntry) -> str:
    """单条字幕 → ASS Dialogue 行（含结尾换行；双语 ``\\N`` 换行）。"""
    return (
        f"Dialogue: 0,{format_ass_time(entry.start_ms)},"
        f"{format_ass_time(entry.end_ms)},{ASS_STYLE_NAME},,0,0,0,,"
        f"{escape_ass_text(entry.render())}\n"
    )


def build_ass(entries: list[SubtitleEntry]) -> str:
    """构建完整 ASS 文本（文件头 + 逐条 Dialogue）。"""
    return ASS_HEADER + "".join(build_ass_dialogue(e) for e in entries)


def parse_ass(text: str) -> list[SubtitleEntry]:
    """解析 ASS 文本 → 字幕列表（仅取 Dialogue 行；容错尽力而为）。

    以 ``[Events]`` 的 Format 行定位 Start/End/Text 字段（缺省按标准
    顺序；Style 段 Format 行无 Text 字段自动忽略）；Text 含逗号时按
    字段数切分保留；时间轴非法的行跳过（不中断整体解析——加载映射
    需要尽力而为）。
    """
    entries: list[SubtitleEntry] = []
    event_fmt: list[str] | None = None
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        low = line.lower()
        if low.startswith("format:"):
            names = [f.strip().lower() for f in line[7:].split(",")]
            if "text" in names:  # 仅 Events 段 Format（Style 段无 Text 字段）
                event_fmt = names
            continue
        if not low.startswith("dialogue:"):
            continue
        parts = line[len("dialogue:") :].strip().split(
            ",", max(9, len(event_fmt) - 1) if event_fmt else 9
        )
        if event_fmt:
            try:
                i_start = event_fmt.index("start")
                i_end = event_fmt.index("end")
                i_text = event_fmt.index("text")
            except ValueError:
                continue
        else:
            i_start, i_end, i_text = 1, 2, 9
        if len(parts) <= max(i_start, i_end, i_text):
            continue
        start = parse_ass_time(parts[i_start])
        end = parse_ass_time(parts[i_end])
        if start is None or end is None:
            continue
        lines = _unescape_ass_text(",".join(parts[i_text:])).split("\n")
        zh = lines[0].strip() if lines else ""
        src = lines[1].strip() if len(lines) > 1 else ""
        entries.append(SubtitleEntry(start, end, zh, src))
    return entries


def entries_to_segments(entries: list[SubtitleEntry]):
    """字幕 → 切片标记（加载字幕映射；origin 标记来源便于 UI 区分）。"""
    from .interfaces import ClipSegment

    return [
        ClipSegment(e.start_ms, e.end_ms, origin="subtitle")
        for e in sorted(entries, key=lambda x: x.start_ms)
    ]
