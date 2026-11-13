"""窗口布局持久化（layout.json）：纯逻辑层 + IO 薄壳（不构造任何 Qt 组件）。

需求（.raw_plans/future_恢复布局.txt）：程序退出时（或注入的游戏关闭时）把
当前窗口状态写入 ``renpy_overlay_cache/layout.json``（与 translations.db 等
四库同一缓存目录，落位经用户确认），启动时读取恢复；与 config.json 冲突时
以 layout.json 优先（仅运行时覆盖，不回写配置文件；设置窗口仍只改 config.json）。

设计原则（沿 config.py 同款约定，配置问题绝不导致崩溃）：

- 解析/组装/校验全部为纯函数，离线单测不需要 QApplication；
- 文件缺失/损坏/字段类型非法逐级降级并记日志，不抛异常；
- 不回写用户坏文件：加载失败不删除不覆盖，下次正常退出自然重写。

记录范围（需求逐条）：

- 流式输出窗口：大小（正文窗，跟随循环消费口径）、双击锁定状态、相对游戏
  窗口的偏移（null = 从未拖动 = 自动停靠）与退出时总几何（win32 物理像素，
  仅诊断与离屏校验，不作为恢复依据）；
- 已创建的截图区域：大小、位置、锁定状态，数组序即创建序；数量超过上限
  （``MAX_SCREENSHOT_WINDOWS``，与 QuickMenu 同源）截断保留前 8 个；
- 截图快捷键模式开关（仅模式状态；主开关状态不记录——恢复后恒为关）；
- 选项副作用日志窗口：是否打开、大小、位置、锁定状态（跳过注入模式由宿主
  侧硬守卫不恢复，本模块照记不误）。

坐标口径（保存 API = 恢复 API，规避一切 DPR 换算）：截图窗/日志窗存 Qt 全局
逻辑坐标（``geometry()``，恢复用 ``move()/resize()``）；流式窗尺寸与偏移存
运行时值（compute_geometry 消费口径），``abs_rect`` 存 ``win32api.window_rect``
物理像素。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    COLOR_HEX_PATTERN,
    DEFAULT_CHOICE_LOG_WINDOW_HEIGHT,
    DEFAULT_CHOICE_LOG_WINDOW_WIDTH,
    DEFAULT_REMOTE_SCREENSHOT_DIAMETER,
    DEFAULT_STREAM_WINDOW_HEIGHT,
    DEFAULT_STREAM_WINDOW_WIDTH,
    MIN_CHOICE_LOG_WINDOW_HEIGHT,
    MIN_CHOICE_LOG_WINDOW_WIDTH,
    MIN_REMOTE_SCREENSHOT_DIAMETER,
    MIN_STREAM_WINDOW_HEIGHT,
    MIN_STREAM_WINDOW_WIDTH,
)
from .translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.layout")

LAYOUT_FILENAME = "layout.json"
#: 结构版本：当前唯一受支持版本；缺失按 1 宽容处理，更高版本整份放弃（向后
#: 兼容：未来新增字段只增不改语义，旧工具读到高版本直接放弃而非误读）
LAYOUT_VERSION = 1

#: 截图窗口数量上限（与 QuickMenu.MAX_WINDOWS 同源：需求上限 8，超出截断保留
#: 前 8 个。常量单列于此，避免纯逻辑模块依赖 Qt 组件模块）
MAX_SCREENSHOT_WINDOWS = 8
#: 截图窗口最小尺寸（与 ScreenshotWindow 构造 setMinimumSize 同源口径）
MIN_SCREENSHOT_WINDOW_WIDTH = 80
MIN_SCREENSHOT_WINDOW_HEIGHT = 60
#: 截图窗口默认尺寸（与 ScreenshotWindow 构造 resize 同源口径）
DEFAULT_SCREENSHOT_WINDOW_WIDTH = 300
DEFAULT_SCREENSHOT_WINDOW_HEIGHT = 200


@dataclass(frozen=True)
class StreamLayout:
    """流式输出窗口的布局记录（尺寸/锁定/偏移 + 诊断用绝对矩形）。"""

    width: int = DEFAULT_STREAM_WINDOW_WIDTH
    height: int = DEFAULT_STREAM_WINDOW_HEIGHT
    locked: bool = False
    user_offset: tuple[int, int] | None = None
    abs_rect: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class ScreenshotWindowLayout:
    """单个截图区域的布局记录（Qt 全局逻辑坐标，数组序即创建序）。"""

    x: int = 0
    y: int = 0
    width: int = DEFAULT_SCREENSHOT_WINDOW_WIDTH
    height: int = DEFAULT_SCREENSHOT_WINDOW_HEIGHT
    locked: bool = False


@dataclass(frozen=True)
class ChoiceLogLayout:
    """选项副作用日志窗口的布局记录（open=False 时其余字段无意义）。"""

    open: bool = False
    x: int = 0
    y: int = 0
    width: int = DEFAULT_CHOICE_LOG_WINDOW_WIDTH
    height: int = DEFAULT_CHOICE_LOG_WINDOW_HEIGHT
    locked: bool = False


@dataclass(frozen=True)
class RemoteWindowLayout:
    """遥控器单窗（A 触发器 / B 瞄准器）的布局记录（Qt 全局逻辑坐标）。"""

    x: int = 0
    y: int = 0
    diameter: int = DEFAULT_REMOTE_SCREENSHOT_DIAMETER


@dataclass(frozen=True)
class RemoteScreenshotLayout:
    """遥控器 A/B 的布局记录（loaded_color=None = B 圆形待命态）。"""

    a: RemoteWindowLayout = field(default_factory=RemoteWindowLayout)
    b: RemoteWindowLayout = field(default_factory=RemoteWindowLayout)
    loaded_color: str | None = None


@dataclass(frozen=True)
class LayoutState:
    """layout.json 的解析结果（版本 + 各组窗口状态；遥控器节点缺失为 None）。"""

    version: int = LAYOUT_VERSION
    stream_window: StreamLayout = field(default_factory=StreamLayout)
    screenshot_windows: tuple[ScreenshotWindowLayout, ...] = ()
    hotkey_mode: bool = False
    choice_log_window: ChoiceLogLayout = field(default_factory=ChoiceLogLayout)
    remote_screenshot: RemoteScreenshotLayout | None = None


# ---- 字段级校验（回退默认并 WARNING，同 config.py 的 _read_* 风格） ----------


def _read_bool(raw: dict, key: str, default: bool) -> bool:
    """布尔字段校验：类型不对 → 回退默认值。"""
    value = raw.get(key, default)
    if not isinstance(value, bool):
        logger.warning("layout %s 不是布尔值（%r），回退默认 %s", key, value, default)
        return default
    return value


def _read_int(raw: dict, key: str, default: int) -> int:
    """整数字段校验（坐标允许负值）：类型不对 → 回退默认值。"""
    value = raw.get(key, default)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        logger.warning("layout %s 不是整数（%r），回退默认 %d", key, value, default)
        return default
    return int(value)


def _read_positive_int(raw: dict, key: str, default: int, minimum: int) -> int:
    """带下限的正整数字段校验（尺寸类）：类型不对或低于下限 → 回退默认值。"""
    value = raw.get(key, default)
    # 注意 bool 是 int 的子类，True/False 不算合法数字
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        logger.warning("layout %s 不是整数（%r），回退默认 %d", key, value, default)
        return default
    if int(value) < minimum:
        logger.warning("layout %s 低于下限 %d（%r），回退默认 %d", key, minimum, value, default)
        return default
    return int(value)


def _read_offset(raw: dict, key: str) -> tuple[int, int] | None:
    """偏移字段（[dx, dy] 或 null）：类型不对 → 回退 None（自动停靠语义）。"""
    value = raw.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
    ):
        logger.warning("layout %s 不是二元数组（%r），回退自动停靠", key, value)
        return None
    return (int(value[0]), int(value[1]))


def _read_rect(raw: dict, key: str) -> tuple[int, int, int, int] | None:
    """诊断矩形字段（[left, top, right, bottom] 或 null）：类型不对 → 忽略。"""
    value = raw.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
    ):
        logger.warning("layout %s 不是四元数组（%r），忽略诊断矩形", key, value)
        return None
    return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))


def _read_stream_window(raw: dict) -> StreamLayout:
    """流式窗口整节解析：节缺失/类型不对整节回退默认，字段级逐项降级。"""
    section = raw.get("stream_window")
    if not isinstance(section, dict):
        if section is not None:
            logger.warning("layout stream_window 不是对象（%r），整节回退默认", section)
        return StreamLayout()
    return StreamLayout(
        width=_read_positive_int(
            section, "width", DEFAULT_STREAM_WINDOW_WIDTH, MIN_STREAM_WINDOW_WIDTH
        ),
        height=_read_positive_int(
            section, "height", DEFAULT_STREAM_WINDOW_HEIGHT, MIN_STREAM_WINDOW_HEIGHT
        ),
        locked=_read_bool(section, "locked", False),
        user_offset=_read_offset(section, "user_offset"),
        abs_rect=_read_rect(section, "abs_rect"),
    )


def _read_screenshot_windows(raw: dict) -> tuple[ScreenshotWindowLayout, ...]:
    """截图区域列表解析：数量超上限截断保留前 8 个，非对象条目跳过。"""
    value = raw.get("screenshot_windows")
    if value is None:
        return ()
    if not isinstance(value, list):
        logger.warning("layout screenshot_windows 不是数组（%r），忽略截图区域", value)
        return ()
    entries: list[ScreenshotWindowLayout] = []
    for item in value:
        if len(entries) >= MAX_SCREENSHOT_WINDOWS:
            logger.warning(
                "layout 截图区域记录超过上限 %d 个，超出部分忽略（需求：不得超过 8 个）",
                MAX_SCREENSHOT_WINDOWS,
            )
            break
        if not isinstance(item, dict):
            logger.warning("layout 截图区域条目不是对象（%r），忽略该条目", item)
            continue
        entries.append(
            ScreenshotWindowLayout(
                x=_read_int(item, "x", 0),
                y=_read_int(item, "y", 0),
                width=_read_positive_int(
                    item, "width", DEFAULT_SCREENSHOT_WINDOW_WIDTH, MIN_SCREENSHOT_WINDOW_WIDTH
                ),
                height=_read_positive_int(
                    item, "height", DEFAULT_SCREENSHOT_WINDOW_HEIGHT, MIN_SCREENSHOT_WINDOW_HEIGHT
                ),
                locked=_read_bool(item, "locked", False),
            )
        )
    return tuple(entries)


def _read_choice_log_window(raw: dict) -> ChoiceLogLayout:
    """选项副作用日志窗口整节解析（open 缺失/非法即视为未打开）。"""
    section = raw.get("choice_log_window")
    if not isinstance(section, dict):
        if section is not None:
            logger.warning("layout choice_log_window 不是对象（%r），整节回退默认", section)
        return ChoiceLogLayout()
    return ChoiceLogLayout(
        open=_read_bool(section, "open", False),
        x=_read_int(section, "x", 0),
        y=_read_int(section, "y", 0),
        width=_read_positive_int(
            section, "width", DEFAULT_CHOICE_LOG_WINDOW_WIDTH, MIN_CHOICE_LOG_WINDOW_WIDTH
        ),
        height=_read_positive_int(
            section, "height", DEFAULT_CHOICE_LOG_WINDOW_HEIGHT, MIN_CHOICE_LOG_WINDOW_HEIGHT
        ),
        locked=_read_bool(section, "locked", False),
    )


# ---- 解析与组装（纯函数） ----------------------------------------------------


def _valid_coordinate(value: object) -> bool:
    """坐标字段合法性（整数，bool 不算；负值合法——多屏环境可为负）。"""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value == int(value)
    )


def _read_remote_window(raw: dict, key: str) -> RemoteWindowLayout | None:
    """遥控器单窗节解析：任一字段缺失或类型非法/直径低于下限 → None。

    需求（future_恢复布局.txt 补充）：遥控器节点字段缺失或类型非法时
    行为与改动前一致（屏幕中心默认摆放）——故字段级不降级，整窗放弃。
    """
    section = raw.get(key)
    if not isinstance(section, dict):
        logger.warning(
            "layout remote_screenshot.%s 不是对象（%r），忽略遥控器恢复", key, section
        )
        return None
    x = section.get("x")
    y = section.get("y")
    diameter = section.get("diameter")
    if not _valid_coordinate(x) or not _valid_coordinate(y):
        logger.warning(
            "layout remote_screenshot.%s 坐标非法（x=%r，y=%r），忽略遥控器恢复",
            key,
            x,
            y,
        )
        return None
    if (
        isinstance(diameter, bool)
        or not isinstance(diameter, (int, float))
        or diameter != int(diameter)
        or int(diameter) < MIN_REMOTE_SCREENSHOT_DIAMETER
    ):
        logger.warning(
            "layout remote_screenshot.%s.diameter 非法（%r，下限 %d），忽略遥控器恢复",
            key,
            diameter,
            MIN_REMOTE_SCREENSHOT_DIAMETER,
        )
        return None
    return RemoteWindowLayout(x=int(x), y=int(y), diameter=int(diameter))


def _read_remote_screenshot(raw: dict) -> RemoteScreenshotLayout | None:
    """遥控器节点解析：节点缺失/null → None（不恢复，改动前行为）。"""
    section = raw.get("remote_screenshot")
    if section is None:
        return None
    if not isinstance(section, dict):
        logger.warning("layout remote_screenshot 不是对象（%r），忽略遥控器恢复", section)
        return None
    a = _read_remote_window(section, "a")
    b = _read_remote_window(section, "b")
    color = section.get("loaded_color")
    if color is not None and (
        not isinstance(color, str) or COLOR_HEX_PATTERN.match(color.strip()) is None
    ):
        logger.warning(
            "layout remote_screenshot.loaded_color 非法（%r，需 #RGB/#RRGGBB），忽略遥控器恢复",
            color,
        )
        return None
    if a is None or b is None:
        return None
    return RemoteScreenshotLayout(
        a=a,
        b=b,
        loaded_color=color.strip() if color is not None else None,
    )


def parse_layout(raw: object) -> LayoutState | None:
    """把 JSON 解析结果转成布局状态（纯函数，逐字段降级，绝不抛异常）。

    顶层不是对象或版本高于当前支持 → None（整份放弃，调用方按无布局处理）；
    版本缺失按 1 宽容处理。字段类型非法逐字段回退默认并 WARNING，不整份作废
    （与 config.load_config 的降级粒度一致）。
    """
    if not isinstance(raw, dict):
        logger.warning("layout.json 顶层不是对象（%r），忽略整份布局", raw)
        return None
    version = raw.get("version", LAYOUT_VERSION)
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != int(version):
        logger.warning("layout version 不是整数（%r），忽略整份布局", version)
        return None
    if int(version) > LAYOUT_VERSION:
        logger.warning(
            "layout version %d 高于当前支持的 %d，忽略整份布局（可删除该文件或升级工具）",
            int(version),
            LAYOUT_VERSION,
        )
        return None
    return LayoutState(
        version=int(version),
        stream_window=_read_stream_window(raw),
        screenshot_windows=_read_screenshot_windows(raw),
        hotkey_mode=_read_bool(raw, "hotkey_mode", False),
        choice_log_window=_read_choice_log_window(raw),
        remote_screenshot=_read_remote_screenshot(raw),
    )


def collect_remote_screenshot_layout(
    *,
    a_x: int,
    a_y: int,
    a_diameter: int,
    b_x: int,
    b_y: int,
    b_diameter: int,
    loaded_color: str | None,
) -> dict:
    """遥控器 A/B 快照组装（纯函数；坐标为 Qt 全局逻辑坐标，直径为运行时值）。"""
    return {
        "a": {"x": int(a_x), "y": int(a_y), "diameter": int(a_diameter)},
        "b": {"x": int(b_x), "y": int(b_y), "diameter": int(b_diameter)},
        "loaded_color": None if loaded_color is None else str(loaded_color),
    }


def collect_layout(
    *,
    stream_width: int,
    stream_height: int,
    stream_locked: bool,
    stream_user_offset: tuple[int, int] | None,
    stream_abs_rect: tuple[int, int, int, int] | None,
    screenshot_windows: Sequence[tuple[int, int, int, int, bool]],
    hotkey_mode: bool,
    choice_log_open: bool,
    choice_log_rect: tuple[int, int, int, int] | None,
    choice_log_locked: bool = False,
    remote_screenshot: dict | None = None,
) -> dict:
    """把宿主现取的纯值快照组装为可写盘的 dict（纯函数，离线单测用）。

    ``screenshot_windows`` 为 ``(x, y, w, h, locked)`` 元组序列（池序即创建
    序）；``choice_log_rect`` 为 ``(x, y, w, h)`` 或 None（未打开）；
    ``stream_abs_rect`` 为 ``(left, top, right, bottom)``（win32 物理像素，
    仅诊断与离屏校验）或 None；``remote_screenshot`` 为
    :func:`collect_remote_screenshot_layout` 的结果，None = 本次运行遥控器
    未创建（节点写 null；保存侧宿主会先以旧文件节点回填，见
    :func:`load_layout_raw`）。"""
    stream: dict = {
        "width": int(stream_width),
        "height": int(stream_height),
        "locked": bool(stream_locked),
        "user_offset": None
        if stream_user_offset is None
        else [int(stream_user_offset[0]), int(stream_user_offset[1])],
    }
    if stream_abs_rect is not None:
        stream["abs_rect"] = [int(item) for item in stream_abs_rect]
    payload: dict = {
        "version": LAYOUT_VERSION,
        "stream_window": stream,
        "screenshot_windows": [
            {
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
                "locked": bool(locked),
            }
            for x, y, width, height, locked in screenshot_windows
        ],
        "hotkey_mode": bool(hotkey_mode),
        "choice_log_window": {
            "open": bool(choice_log_open),
            **(
                {
                    "x": int(choice_log_rect[0]),
                    "y": int(choice_log_rect[1]),
                    "width": int(choice_log_rect[2]),
                    "height": int(choice_log_rect[3]),
                    "locked": bool(choice_log_locked),
                }
                if choice_log_rect is not None
                else {}
            ),
        },
        "remote_screenshot": remote_screenshot,
    }
    return payload


# ---- 路径与 IO 薄壳 -----------------------------------------------------------


def layout_path_for(game_dir) -> Path | None:
    """游戏目录对应的布局文件路径（与四库同一 ``renpy_overlay_cache``）。

    无 game_dir（存储降级场景，如进程 exe 不可知）返回 None，调用方跳过
    读写并记日志。
    """
    if not game_dir:
        return None
    return Path(game_dir) / CACHE_DIR_NAME / LAYOUT_FILENAME


def load_layout(path: Path) -> LayoutState | None:
    """读取并解析布局文件；缺失/损坏/版本不支持返回 None（不抛异常）。"""
    target = Path(path)
    if not target.is_file():
        logger.debug("布局文件不存在（%s），按默认布局启动", target)
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取布局文件失败（%s），忽略整份布局：%s", target, exc)
        return None
    return parse_layout(raw)


def load_layout_raw(path: Path) -> dict | None:
    """读取 layout.json 的原始 dict（不做 schema 校验；缺失/损坏返回 None）。

    保存侧合并专用：本次运行遥控器未创建时，宿主以旧文件中的遥控器节点
    回填新 payload，避免一次未启用就把既有位置记录清空（需求三.2）。
    """
    target = Path(path)
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取布局文件失败（%s）：%s", target, exc)
        return None
    return raw if isinstance(raw, dict) else None


def save_layout(path: Path, payload: dict) -> bool:
    """把快照 dict 写盘（目录不存在自动创建）；失败只记日志返回 False。"""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("保存布局文件失败（%s）：%s", target, exc)
        return False
    logger.info("窗口布局已保存：%s", target)
    return True


# ---- 几何校验纯函数 ------------------------------------------------------------


def rect_intersects_any_screen(
    x: int,
    y: int,
    width: int,
    height: int,
    screen_rects: Sequence[tuple[int, int, int, int]],
) -> bool:
    """矩形是否与任一屏幕矩形相交（纯函数，离屏降级判定）。

    ``screen_rects`` 为 ``(x, y, w, h)`` 序列，单位必须与 ``x/y/width/height``
    同源（Qt 逻辑坐标对 Qt 逻辑屏幕；win32 物理像素对 win32 虚拟桌面）。
    边缘相切（零面积交集）不算相交。
    """
    for sx, sy, sw, sh in screen_rects:
        overlap_w = min(x + width, sx + sw) - max(x, sx)
        overlap_h = min(y + height, sy + sh) - max(y, sy)
        if overlap_w > 0 and overlap_h > 0:
            return True
    return False


def clamp_size(width: int, height: int, min_width: int, min_height: int) -> tuple[int, int]:
    """尺寸下限钳制（纯函数；最小值与各窗现行拉伸下限同口径）。"""
    return max(min_width, int(width)), max(min_height, int(height))
