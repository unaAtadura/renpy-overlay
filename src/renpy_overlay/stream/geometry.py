"""流式悬浮窗的停靠几何纯函数：整体几何计算、双窗拆分与拖动参照系换算。

全部为纯函数，便于离线验证坐标计算（见 ``tests/test_stream_window.py``）。
"""

from __future__ import annotations

MARGIN = 12
DOCK_CHOICES = (
    "top-center",
    "top-left",
    "top-right",
    "bottom-center",
    "bottom-left",
    "bottom-right",
)


def compute_geometry(
    game_rect: tuple[int, int, int, int],
    size: tuple[int, int],
    dock: str,
    user_offset: tuple[int, int] | None = None,
    margin: int = MARGIN,
) -> tuple[int, int, int, int]:
    """计算悬浮窗的目标几何 ``(x, y, 宽, 高)``（屏幕物理像素）。

    - ``user_offset`` 为 None：按 ``dock`` 贴靠游戏窗口；
    - ``user_offset`` 存在：用户手动拖动过，位置 = 游戏窗口左上角 + 偏移，
      尺寸仍按游戏窗口约束缩放 —— 游戏窗口移动/改变大小时悬浮窗跟随，
      但保持用户设定的相对位置。

    抽成纯函数便于离线验证坐标计算（见 ``tests/test_stream_window.py``）。
    """
    left, top, right, bottom = game_rect
    game_w, game_h = max(1, right - left), max(1, bottom - top)
    width = max(240, min(size[0], game_w - 2 * margin))
    height = max(80, min(size[1], game_h // 2))

    if user_offset is not None:
        return left + user_offset[0], top + user_offset[1], width, height

    if dock.startswith("top"):
        y = top + margin
    else:
        y = bottom - height - margin
    if dock.endswith("left"):
        x = left + margin
    elif dock.endswith("right"):
        x = right - width - margin
    else:
        x = left + (game_w - width) // 2
    return x, y, width, height


def pair_layout(
    total: tuple[int, int, int, int],
    title_h: int,
    gap: int,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """把整体几何 ``(x, y, 宽, 高)`` 拆成 ``(标题窗几何, 正文窗几何)``。

    两窗水平左对齐；标题窗在正文窗正上方，中间空出 ``gap`` 像素。
    抽成纯函数便于离线验证（见 ``tests/test_stream_window.py``）。
    """
    x, y, width, height = total
    title = (x, y, width, max(1, title_h))
    body = (x, y + title_h + gap, width, max(1, height - title_h - gap))
    return title, body


def pair_offset_from_body(
    body_xy: tuple[int, int],
    game_xy: tuple[int, int],
    title_h: int,
    gap: int,
) -> tuple[int, int]:
    """由正文窗左上角换算 ``user_offset``（参照整体几何顶点 = 标题窗左上角）。

    跟随循环的 ``compute_geometry`` 把偏移加在整体几何上，再经 :func:`pair_layout`
    把正文窗放在 ``y + title_h + gap``；若拖动结束时以正文窗为参照记偏移，
    松手后首次重定位会整体下移「标题高 + 间距」（表现为一次向下跳变）。
    抽成纯函数便于离线验证两套逻辑参照系一致（见 ``tests/test_stream_window.py``）。
    绑定入口横向追加在标题窗右侧且由标题内容宽驱动让位（同一渲染帧内绘制），
    不参与本换算，故双窗参照系对标题/正文窗始终成立。
    """
    return (body_xy[0] - game_xy[0], body_xy[1] - title_h - gap - game_xy[1])
