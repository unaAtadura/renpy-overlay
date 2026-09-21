"""游戏脚本扫描与解析：从硬盘提取对白 / 分支选项 / tl 显示文本。

三层能力（全部只依赖标准库，不依赖 renpy / PyQt6）：

1. :func:`scan_rpy_files` —— 枚举 ``game/`` 下的 ``.rpy`` 相对路径（含
   ``.rpa`` 归档内条目，形如 ``game/x.rpa!script/a.rpy``），排序稳定保证
   弹窗顺序一致；只列文件不读内容（毫秒级，供选择弹窗同步展示）；
2. :func:`extract_blocks` / :func:`extract_from_rpy` —— 轻量行解析：
   say 三形态（角色前缀 / 旁白 / with 修饰）、menu 选项组与选项体内嵌的
   say（分支被选中后触发的差分对白）、translate 块的显示文本（``strings``
   块取 ``new``，label 块内未注释的 say 行即 new）；跳过注释、python 块
   与 screen language 语句；
3. :func:`collect_texts` —— 加载（直接读文件或解包 rpa 条目）→ 解析 →
   清洗（``cleaning`` 口径）→ 去重，产出缓存键原文条目。

解析为启发式行解析（设计文档已知局限）：.rpyc 不解析；screen language
文本按钮不收集；解析失败的文件记日志跳过，不中断整体。
"""

from __future__ import annotations

import ast
import logging
import pickle
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from .cleaning import build_choice_key, clean_text

logger = logging.getLogger("renpy_overlay.pretranslate.parser")

#: .rpa 归档内条目的分隔符（相对路径形如 "game/x.rpa!inner/a.rpy"）
RPA_SEPARATOR = "!"


@dataclass
class SourceText:
    """清洗后的原文条目（text 即缓存键原文）。"""

    text: str  # 缓存键原文（对白 / 选项编号拼接 / tl new 文本）
    kind: str  # "say" | "menu" | "tl_str"
    source: str  # 来源文件相对路径（弹窗条目与日志用）


#: 这些关键字开头的行不是 say（流程 / 数据 / screen language 语句等）
_NON_SAY_PREFIX = frozenset(
    {
        "add", "bar", "button", "call", "caption", "default", "define", "drag",
        "draggroup", "elif", "else", "expression", "fixed", "for", "frame", "grid",
        "hide", "hbox", "hotbar", "hotspot", "if", "image", "imagemap", "imagebutton",
        "init", "jump", "key", "label", "mousearea", "nearrect", "new", "null", "old",
        "pause", "play", "python", "queue", "return", "scene", "screen", "set",
        "show", "side", "stop", "style", "text", "textbutton", "timer", "transform",
        "translate", "transclude", "use", "vbox", "viewport", "voice", "while",
        "window", "with",
    }
)

#: menu 条目：字符串后紧跟 ":"（可选 if 条件），行尾只允许空白或注释
_MENU_ENTRY_RE = re.compile(r'\s*(?:if\b[^:]*)?\s*:\s*(?:#.*)?$')
_TRANSLATE_STRINGS_RE = re.compile(r"^translate\s+\S+\s+strings\s*:\s*(?:#.*)?$")
_PYTHON_RE = re.compile(r"^(?:init\s+(?:-\d+\s+)?|screen\s+lang\s+)?python.*:\s*(?:#.*)?$")
_SAY_PREFIX_RE = re.compile(r"\s*(?:([A-Za-z_]\w*)\s+)?")
_WORD_RE = re.compile(r"[A-Za-z_]")
#: 跨行三引号 say 的最大吸收行数（未闭合防御上限）
_MAX_MULTILINE_LINES = 64


# ------------------------------------------------------------------ 字符串字面量


def _string_at(line: str, start: int) -> tuple[str, int] | None:
    """解析 ``line[start]`` 起的字符串字面量，返回 ``(值, 结束后位置)``。

    支持单双引号与三引号、反斜杠转义；解析结果必须能被 ``ast.literal_eval``
    还原为 str（失败返回 None —— 非法转义等按行级放弃处理）。
    """
    quote = line[start]
    n = len(line)
    if line[start : start + 3] == quote * 3:
        end = line.find(quote * 3, start + 3)
        if end < 0:
            return None
        token, next_pos = line[start : end + 3], end + 3
    else:
        pos = start + 1
        while pos < n:
            ch = line[pos]
            if ch == "\\":
                pos += 2
                continue
            if ch == quote:
                break
            pos += 1
        else:
            return None
        if pos >= n:
            return None
        token, next_pos = line[start : pos + 1], pos + 1
    try:
        value = ast.literal_eval(token)
    except (ValueError, SyntaxError):
        return None
    if not isinstance(value, str):
        return None
    return value, next_pos


def _say_text(line: str) -> str | None:
    """say 语句三种形态的首段字符串：``e "文本"`` / ``"文本"`` / ``e "文本" with x``。"""
    start = _say_string_start(line)
    if start is None:
        return None
    parsed = _string_at(line, start)
    return parsed[0] if parsed else None


def _say_string_start(line: str) -> int | None:
    """say 语句字符串字面量的起始偏移（非 say 行返回 None）。"""
    match = _SAY_PREFIX_RE.match(line)
    prefix = match.group(1)
    if prefix is not None and (prefix in _NON_SAY_PREFIX or not _WORD_RE.match(prefix)):
        return None
    rest = line[match.end() :]
    if not rest or rest[0] not in "\"'":
        return None
    return len(line) - len(rest)


def _menu_caption(line: str) -> str | None:
    """menu 条目的 caption：行首字符串，其后为 ``:`` / ``if 条件:``（行尾注释允许）。"""
    if not line or line[0] not in "\"'":
        return None
    parsed = _string_at(line, 0)
    if parsed is None:
        return None
    _, next_pos = parsed
    if not _MENU_ENTRY_RE.match(line[next_pos:]):
        return None
    return parsed[0]


# ------------------------------------------------------------------ 行解析


def extract_blocks(text: str) -> list[tuple[str, list[str]]]:
    """解析脚本文本为 ``(kind, payload)`` 块序列（清洗前原文）。

    - ``("say", [文本])``：对白（含 menu 选项体内嵌的 say——分支差分
      对白，非 caption 的 say 形态行均按此收集）；
    - ``("menu", [caption, ...])``：同一 menu 语句的选项组（按出现顺序）；
    - ``("tl_str", [文本])``：translate strings 块的 ``new`` 文本。
    translate label 块内未注释的 say 行按普通 say 收集（即 new 文本）。
    """
    blocks: list[tuple[str, list[str]]] = []
    menu_stack: list[int] = []  # 激活中的 menu 块缩进（支持嵌套，启发式）
    menu_items: list[str] | None = None  # 当前 menu 组的 captions（退出 menu 时落块）
    strings_indent: int | None = None  # translate strings 块缩进
    python_stack: list[int] = []  # python 块缩进（块内全部跳过）
    multiline: list[str] | None = None  # 跨行三引号 say 的行缓冲（闭合即落块）

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if not stripped.startswith("#") and multiline is not None:
            # 跨行三引号 say：持续吸收直到字面量可闭合解析。字符串内容
            # 行不参与下方缩进收缩（续行缩进回落不误触块退出与 menu 落组）
            multiline.append(stripped)
            joined = "\n".join(multiline)
            start = _say_string_start(joined)
            parsed = _string_at(joined, start) if start is not None else None
            if parsed is not None:
                blocks.append(("say", [parsed[0]]))
                multiline = None
            elif len(multiline) > _MAX_MULTILINE_LINES:  # 防御：未闭合放弃
                logger.warning("三引号 say 未闭合，放弃解析：%s…", joined[:40])
                multiline = None
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        # 先按本行缩进收缩已退出的块（保证块内/块外判定正确）
        while menu_stack and indent <= menu_stack[-1]:
            menu_stack.pop()
        while python_stack and indent <= python_stack[-1]:
            python_stack.pop()
        if strings_indent is not None and indent <= strings_indent:
            strings_indent = None
        if not menu_stack and menu_items is not None:
            blocks.append(("menu", menu_items))
            menu_items = None
        if not stripped.startswith("#"):
            if python_stack:
                continue
            if strings_indent is None and _TRANSLATE_STRINGS_RE.match(stripped):
                strings_indent = indent
                continue
            if strings_indent is None and _PYTHON_RE.match(stripped):
                python_stack.append(indent)
                continue
            if strings_indent is None and re.match(r"^menu\b", stripped):
                if menu_items is None:
                    menu_items = []
                menu_stack.append(indent)
                continue
            if strings_indent is not None:
                # strings 块内：old 是被翻译的原文（非显示文本），new 才收集
                if stripped.startswith("new") or stripped.startswith("old"):
                    inner = stripped.split(None, 1)
                    if len(inner) == 2 and inner[1][:1] in "\"'":
                        parsed = _string_at(stripped, len(stripped) - len(inner[1]))
                        if parsed is not None and inner[0] == "new":
                            blocks.append(("tl_str", [parsed[0]]))
                continue
            if menu_stack:
                caption = _menu_caption(stripped)
                if caption is not None:
                    if menu_items is None:  # pragma: no cover - menu 行已建组
                        menu_items = []
                    menu_items.append(caption)
                    continue
                # 选项体内非 caption 行 fall through 到下方 say 判定：分支
                # 差分对白由此收集；jump / $ / pass / if 等流程行被
                # _say_string_start 拒绝后跳过
            start = _say_string_start(stripped)
            if start is not None:
                parsed = _string_at(stripped, start)
                if parsed is not None:
                    blocks.append(("say", [parsed[0]]))
                    continue
                if '"""' in stripped or "'''" in stripped:
                    multiline = [stripped]  # 三引号未闭合：进入跨行吸收
                    continue
    if menu_items is not None:
        blocks.append(("menu", menu_items))
    return blocks


def extract_from_rpy(text: str) -> list[str]:
    """全部显示文本的拍平视图（say / menu caption / tl new，清洗前原文）。"""
    result: list[str] = []
    for _kind, payload in extract_blocks(text):
        result.extend(payload)
    return result


# ------------------------------------------------------------------ .rpa 归档


def _rpa_index(archive: Path) -> dict[str, list[tuple[int, int]]]:
    """解析 RPA-3.0 索引：``{内部路径: [(偏移, 长度), ...]}``（偏移已去异或）。"""
    with archive.open("rb") as handle:
        header = handle.readline().decode("ascii", "ignore").split()
        if len(header) < 3 or header[0] != "RPA-3.0":
            raise ValueError(f"不支持的归档格式：{archive.name}（仅 RPA-3.0）")
        key = int(header[2], 16)
        handle.seek(int(header[1], 16))
        index = pickle.loads(zlib.decompress(handle.read()))
    if not isinstance(index, dict):  # pragma: no cover - 异常归档防御
        raise ValueError(f"归档索引不是字典：{archive.name}")
    decoded: dict[str, list[tuple[int, int]]] = {}
    for name, entries in index.items():
        decoded[str(name)] = [
            (int(offset) ^ key, int(length)) for offset, length in entries
        ]
    return decoded


def unpack_rpa_entry(archive: Path, name: str) -> bytes:
    """按索引取出归档内一个文件的原始字节（首段 zlib 解压）。"""
    index = _rpa_index(archive)
    if name not in index:
        raise KeyError(f"归档 {archive.name} 内不存在条目 {name}")
    offset, length = index[name][0]
    with archive.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    if data[:1] == b"\x78":  # zlib 流头：未混淆
        return zlib.decompress(data)
    return zlib.decompress(data[data[0] :])  # 首字节 = 混淆前缀长度


# ------------------------------------------------------------------ 扫描与收集


def scan_rpy_files(game_dir: Path, tl_langs: list[str] | None = None) -> list[str]:
    """枚举 ``game_dir/game`` 下全部 ``.rpy`` 的相对路径（升序稳定）。

    ``tl_langs`` 为 None 时包含全部 ``tl/<语言>/`` 文件；给列表时只保留
    指定语言的 tl 文件（非 tl 文件不受影响）。``.rpa`` 归档内的 ``.rpy``
    以 ``game/x.rpa!inner/path.rpy`` 形式参与列表。
    """
    game_root = Path(game_dir) / "game"
    tl_root = game_root / "tl"
    results: list[str] = []
    if game_root.is_dir():
        for path in sorted(game_root.rglob("*.rpy")):
            rel = path.relative_to(Path(game_dir)).as_posix()
            if tl_root in path.parents and tl_langs is not None:
                parts = path.relative_to(tl_root).parts
                if not parts or parts[0] not in set(tl_langs):
                    continue
            results.append(rel)
        for archive in sorted(game_root.glob("*.rpa")):
            try:
                names = _rpa_index(archive)
            except Exception as exc:  # 损坏归档不阻塞扫描
                logger.warning("读取归档索引失败，跳过 %s：%s", archive.name, exc)
                continue
            for name in sorted(names):
                if not name.endswith(".rpy"):
                    continue
                # 归档内条目的 tl 过滤：语言段 = 路径中 tl 的下一段（兼容
                # "tl/<语言>/x.rpy" 与 ".../tl/<语言>/x.rpy" 两种形态）
                if tl_langs is not None:
                    parts = name.split("/")
                    if "tl" in parts:
                        position = parts.index("tl")
                        lang = parts[position + 1] if position + 1 < len(parts) else None
                        if lang is None or lang not in set(tl_langs):
                            continue
                rel = f"{archive.relative_to(Path(game_dir)).as_posix()}{RPA_SEPARATOR}{name}"
                results.append(rel)
    results.sort()
    return results


def _load_source(rel: str, game_dir: Path) -> str:
    """按相对路径加载脚本文本：普通文件直读，rpa 条目解包后解码。"""
    if RPA_SEPARATOR in rel:
        archive_rel, name = rel.split(RPA_SEPARATOR, 1)
        raw = unpack_rpa_entry(Path(game_dir) / archive_rel, name)
    else:
        raw = (Path(game_dir) / rel).read_bytes()
    return raw.decode("utf-8-sig", "replace")


def collect_texts(files: list[str], game_dir: Path) -> list[SourceText]:
    """收集勾选文件的缓存键原文条目：加载 → 解析 → 清洗 → 全局去重。

    - 对白与 tl 文本经 :func:`clean_text`，选项组经 :func:`build_choice_key`；
    - 清洗后为空（纯标签 / 空串）的条目丢弃；同一原文只保留首个来源
      （每原文只翻译一次）；
    - 单文件加载 / 解析失败记日志跳过，不中断整体。
    """
    seen: dict[str, SourceText] = {}
    for rel in files:
        try:
            content = _load_source(rel, Path(game_dir))
            blocks = extract_blocks(content)
        except Exception as exc:  # 单文件失败不阻塞整体
            logger.warning("预构建解析失败，跳过 %s：%s", rel, exc)
            continue
        for kind, payload in blocks:
            if kind == "menu":
                text = build_choice_key(payload)
            else:
                text = clean_text(payload[0]) if payload else ""
            if not text or text in seen:
                continue
            seen[text] = SourceText(text=text, kind=kind, source=rel)
    return list(seen.values())
