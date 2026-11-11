"""选项副作用日志的纯逻辑层：条目模型、去重、状态翻转与摘要解析。

与 Qt 完全解耦（可离线单测）：视图层 ``ChoiceLogWindow`` 仅消费
:meth:`ChoiceEffectLog.entries` 全量重渲染。本层数据不进入翻译系统——
不进翻译缓存、不参与翻译去重、不作为可翻译文本（需求红线）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("renpy_overlay.choice_log")

#: 同一菜单事件的合并窗口（秒）：exports_menu 主 hook 与 choice screen 轮询
#: 兜底在 agent 端已互斥去重，此处防双路径同拍重复上报的第二道防线
DUPLICATE_WINDOW_SECONDS = 0.5

#: 单条摘要/原文的最大显示宽度（字符）：超出截断，窄幅弹窗下防溢出
_MAX_TEXT_CHARS = 48

#: 定位缺口：与标题窗入口条的既有间距同源（bound_window.BOUND_GAP）
ANCHOR_GAP = 8

_PLUS_RE = re.compile(r"^(\S+?)\s*\+=\s*(.+)$")
_MINUS_RE = re.compile(r"^(\S+?)\s*-=\s*(.+)$")
_BOOL_RE = re.compile(r"^(\S+?)\s*=\s*(True|False)\s*$")


def _truncate(text: str, limit: int = _MAX_TEXT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _strip_dollar(text: str) -> str:
    """剥离可选的 ``$`` 语句前缀（PyCode.source 的真实形态通常不含它）。"""
    body = (text or "").strip()
    if body.startswith("$"):
        body = body[1:].strip()
    return body


def _parse_effect(body: str) -> str | None:
    """三类正则的核心判定：命中返回摘要，未命中返回 None。

    summarize_effect 与 is_effect_parsed 共用本函数，保证「摘要内容」与
    「是否解析成功」永远同口径（无两处逻辑漂移）。
    """
    match = _PLUS_RE.match(body)
    if match:
        return _truncate(f"{match.group(1)} +{match.group(2).strip()}")
    match = _MINUS_RE.match(body)
    if match:
        return _truncate(f"{match.group(1)} -{match.group(2).strip()}")
    match = _BOOL_RE.match(body)
    if match:
        return _truncate(f"{match.group(1)} = {match.group(2)}")
    return None


def is_effect_parsed(text: str) -> bool:
    """与 :func:`summarize_effect` 同口径判定该行是否解析成功（纯函数）。

    渲染层据此逐行二选一：解析成功只显示摘要（不再并列原文），未命中
    （函数调用/条件表达式/多语句折叠等）只显示完整原文——不展示被截断
    的摘要而丢失原文语义。
    """
    return _parse_effect(_strip_dollar(text)) is not None


def summarize_effect(text: str) -> str:
    """把一条 ``$`` 语句解析为极简摘要；解析不了回退原文截断。

    识别形态：``$ var += n`` → ``var +n``、``$ var -= n`` → ``var -n``、
    ``$ flag = True/False`` → ``flag = True``。变量名保留游戏原名（语义
    翻译不可靠，需求仅要求看清加减方向与布尔目标值）；带空格的复杂表达式
    （下标运算、函数调用等）不属于此三类，回退原文展示。``$`` 前缀可选
    （PyCode.source 的真实形态通常不含它，兼容两种来源）。
    """
    body = _strip_dollar(text)
    parsed = _parse_effect(body)
    return parsed if parsed is not None else _truncate(body)


@dataclass
class ChoiceItem:
    """单个选项的副作用条目：原文语句 + 极简摘要 + 预测/已发生状态。"""

    caption: str
    effects: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    state: str = "predicted"  # "predicted" | "happened"


@dataclass
class ChoiceLogEntry:
    """一次菜单事件：时间 + 选项上文 + 全部选项条目 + 玩家所选序号。"""

    ts: float
    who: str
    what: str
    items: list[ChoiceItem]
    picked_index: int | None = None


class ChoiceEffectLog:
    """选项副作用日志（纯内存，会话级）：累积、去重、翻转、清空。

    数据唯一累积点在宿主侧：弹窗只是视图（托盘隐藏/恢复不清空），仅
    「关闭」（图标再点）触发 :meth:`clear`——需求：关闭清空、下次空白开始。
    """

    def __init__(self) -> None:
        self._entries: list[ChoiceLogEntry] = []
        self._last_fingerprint: tuple | None = None
        self._last_ts = 0.0

    def append_menu(
        self,
        items: list[str],
        effects: list | None,
        who: str,
        what: str,
        ts: float | None = None,
    ) -> bool:
        """追加一次菜单事件；同指纹时间窗内重复上报合并（返回 False）。

        ``effects`` 为 agent 端 AST 采集结果（与 ``items`` 等长的语句列表，
        或 None = 采集降级）：长度不齐时不做位置猜测，该条目全部选项按无
        副作用记录——宁可少展示也不错位展示。
        """
        captions = [str(item).strip() for item in (items or []) if str(item).strip()]
        if not captions:
            logger.debug("副作用日志：choice 事件无有效选项，不入账")
            return False
        now = float(ts) if ts is not None else time.time()
        fingerprint = (tuple(captions), (what or "").strip()[:60])
        if (
            fingerprint == self._last_fingerprint
            and abs(now - self._last_ts) <= DUPLICATE_WINDOW_SECONDS
        ):
            logger.debug("副作用日志：同指纹时间窗内合并，未新增条目")
            return False
        effect_lists = None
        if isinstance(effects, list) and len(effects) == len(captions):
            effect_lists = effects
        clean_items: list[ChoiceItem] = []
        for position, caption in enumerate(captions):
            raw_effects: list[str] = []
            if effect_lists is not None:
                block = effect_lists[position]
                if isinstance(block, list):
                    raw_effects = [str(line) for line in block if str(line).strip()]
            clean_items.append(
                ChoiceItem(
                    caption=caption,
                    effects=raw_effects,
                    summaries=[summarize_effect(line) for line in raw_effects],
                )
            )
        self._entries.append(
            ChoiceLogEntry(
                ts=now, who=str(who or ""), what=str(what or ""), items=clean_items
            )
        )
        self._last_fingerprint = fingerprint
        self._last_ts = now
        return True

    def mark_picked(self, index: int, caption: str) -> bool:
        """玩家做出选择：最近一条未选条目按 caption 精确优先、index 兜底翻转。

        都匹配不上时仅记录所选序号（条目停留预测态）；匹配成功把对应条目
        翻转为 "happened"。返回是否发生了状态翻转。
        """
        for entry in reversed(self._entries):
            if entry.picked_index is not None:
                continue
            entry.picked_index = index if isinstance(index, int) else None
            target = None
            text = (caption or "").strip()
            if text:
                for position, item in enumerate(entry.items):
                    if item.caption == text:
                        target = position
                        break
            if target is None and isinstance(index, int) and 0 <= index < len(entry.items):
                target = index
            if target is None:
                return False
            entry.items[target].state = "happened"
            return True
        return False

    def entries(self) -> list[ChoiceLogEntry]:
        """当前全部条目（按时间顺序累积；视图全量重渲染用）。"""
        return list(self._entries)

    def clear(self) -> None:
        """清空全部累积与去重状态（关闭弹窗时调用）。"""
        self._entries.clear()
        self._last_fingerprint = None
        self._last_ts = 0.0


def anchored_position(
    anchor_x: float,
    anchor_y: float,
    width: int,
    height: int,
    screen_x: float,
    screen_y: float,
    screen_w: float,
    screen_h: float,
    gap: int = ANCHOR_GAP,
) -> tuple[int, int]:
    """弹窗定位纯函数：左下角贴合锚点（图标右上角）并向外让 gap，钳在屏内。

    屏幕几何无效（<=0）时不钳制；放不下时整体贴边，保证弹窗完整可见。
    """
    x = int(anchor_x + gap)
    y = int(anchor_y - height)
    if screen_w > 0 and screen_h > 0:
        x = max(int(screen_x), min(x, int(screen_x + screen_w - width)))
        y = max(int(screen_y), min(y, int(screen_y + screen_h - height)))
    return x, y
