"""正文窗滚动与蒙版条带的纯计算：滚动目标、滚轮、拖拽速率、蒙版行聚合。

全部为纯函数，便于离线验证（见 ``tests/test_stream_window.py``）；
蒙版条带几何依赖 :mod:`.visual` 的边距与内边距常量。
"""

from __future__ import annotations

import math

from .visual import BODY_MARGIN, MASK_PAD_X, MASK_PAD_Y

WHEEL_LINES_PER_NOTCH = 3.0  # 滚轮每格回退/推进的行数
#: 锁定态拖拽滚动（按住正文文字上下滑动）：以按下点为基准的垂直偏移分档
DRAG_SCROLL_DEBOUNCE_PX = 12.0  # 防抖区：不触发滚动（过滤手抖与误触微动）
DRAG_SCROLL_FAST_PX = 120.0  # 超过该偏移进入快速滚动档
DRAG_SCROLL_SLOW_NOTCHES_PER_SEC = 1.0  # 慢速档：每秒滚动 1 格滚轮
DRAG_SCROLL_FAST_FACTOR = 5.0  # 快速档：慢速的 5 倍（每 0.2 秒 1 格滚轮）
DRAG_SCROLL_INTERVAL_MS = 16  # 拖拽滚动步进周期（与渲染节拍一致）


def body_mask_bands(
    spans: list[tuple[int, float, float]],
    line_h: float,
    box_h: float,
) -> list[tuple[float, float, float, float]]:
    """按行聚合已排版字符，返回正文蒙版条带矩形 ``(x, y, w, h)``（内容坐标系）。

    ``spans`` 为 ``(行号, 字符 x 起点, 字符前进宽)`` 列表；条带只覆盖实际有
    文字的行（打字机过程中天然只盖住已出现的字符），高度基于
    ``min(字形盒高, 行高)`` 并向内收 ``MASK_PAD_Y``，保证相邻行蒙版之间留出
    透明空隙。抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    if not spans:
        return []
    band_h = max(1.0, min(box_h, line_h) - 2 * MASK_PAD_Y)
    rows: dict[int, list[float]] = {}
    for row, x, advance in spans:
        span = rows.get(row)
        if span is None:
            rows[row] = [x, x + advance]
        else:
            span[0] = min(span[0], x)
            span[1] = max(span[1], x + advance)
    bands: list[tuple[float, float, float, float]] = []
    for row in sorted(rows):
        x0, x1 = rows[row]
        center_y = BODY_MARGIN + row * line_h + box_h / 2.0
        bands.append(
            (x0 - MASK_PAD_X, center_y - band_h / 2.0, (x1 - x0) + 2 * MASK_PAD_X, band_h)
        )
    return bands


def body_scroll_target(
    follow: bool, scroll_target: float, max_scroll: float, anchor: str = "start"
) -> float:
    """计算正文窗本帧的滚动目标（内容坐标系像素）。

    ``follow`` 为 False（滚轮回看中）：把既有目标收敛回 ``[0, max_scroll]``，
    防止内容清空或变短后目标越界。``follow`` 为 True 时按锚点：

    - ``"start"``（默认）：固定显示第一行（偏移 0）—— 大段文字流式输出时
      不再因自动滚屏被推出可视区，1-2 行短文本（本就不足一屏）观感不变；
    - ``"tail"``（实时语音识别）：跟随内容末尾（偏移 = max_scroll）——
      倒数第 2 个字符始终可见，与语音同步显示（需求）。

    抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    if not follow:
        return min(max(scroll_target, 0.0), max_scroll)
    if anchor == "tail":
        return max_scroll
    return 0.0


def body_wheel_target(
    scroll_target: float,
    notches: float,
    line_h: float,
    max_scroll: float,
    anchor: str = "start",
) -> tuple[float, bool]:
    """由滚轮事件计算新的 ``(滚动目标, 跟随态)``。

    每格滚动 ``WHEEL_LINES_PER_NOTCH`` 行。start 锚点：下翻（notches < 0）
    向底部推进回看后文并暂停固定首行；上翻回退，滚回顶部（目标归零）即
    恢复固定显示第一行。tail 锚点（实时语音识别）为镜像语义：上翻回看
    上文并暂停尾部跟随，下翻滚回底部（目标到底）即恢复尾部跟随。
    抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    target = min(
        max(scroll_target - notches * WHEEL_LINES_PER_NOTCH * line_h, 0.0),
        max_scroll,
    )
    if anchor == "tail":
        return target, notches < 0 and target >= max_scroll - 0.5
    return target, notches > 0 and target <= 0.5


def body_drag_scroll_rate(dy: float, line_h: float) -> float:
    """由按住后鼠标的垂直偏移量计算拖拽滚动速率（逻辑像素/秒，含方向）。

    锁定态拖拽滚动的速度分档：防抖区（|dy| <= DRAG_SCROLL_DEBOUNCE_PX）
    返回 0 不滚动；慢速区每秒滚动 1 格滚轮（WHEEL_LINES_PER_NOTCH 行）；
    超过 DRAG_SCROLL_FAST_PX 进入快速档（慢速 5 倍）。方向与触屏直觉一致：
    上滑（dy < 0）看后文（偏移增大），下滑（dy > 0）回看上文（偏移减小）。
    抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    if abs(dy) <= DRAG_SCROLL_DEBOUNCE_PX:
        return 0.0
    notches_per_sec = DRAG_SCROLL_SLOW_NOTCHES_PER_SEC
    if abs(dy) > DRAG_SCROLL_FAST_PX:
        notches_per_sec *= DRAG_SCROLL_FAST_FACTOR
    return -math.copysign(notches_per_sec * WHEEL_LINES_PER_NOTCH * line_h, dy)
