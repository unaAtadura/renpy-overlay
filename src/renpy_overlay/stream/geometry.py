"""流式悬浮窗的停靠几何纯函数：整体几何计算、双窗拆分与拖动参照系换算。

全部为纯函数，便于离线验证坐标计算（见 ``tests/test_stream_window.py``）。
"""

from __future__ import annotations

MARGIN = 12
MIN_STREAM_WIDTH = 240  # 整体宽度下限（正文窗可读下限，与 config 校验下限同口径）
MIN_STREAM_HEIGHT = 80  # 整体高度下限（含标题窗与间距）
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
    width = max(MIN_STREAM_WIDTH, min(size[0], game_w - 2 * margin))
    height = max(MIN_STREAM_HEIGHT, min(size[1], game_h // 2))

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


#: 八向拉伸热区宽度（逻辑像素）：与拖动启动阈值同量级；窄窗自适应下限由
#: 调用方以 min(edge_px, 短边×15%) 收窄（参考实现口径）
RESIZE_EDGE_PX = 12


def edge_at(
    x: int,
    y: int,
    width: int,
    height: int,
    edge_px: int = RESIZE_EDGE_PX,
    right_start: int | None = None,
    right_end: int | None = None,
) -> str | None:
    """窗口本地坐标 → 八向拉伸边缘/角（None = 非热区；纯函数，离线测试用）。

    - 角优先于边：两方向热区叠加判角（nw/ne/sw/se），否则退回单边
      （n/s/w/e）；
    - 东向热区带由 ``right_start``（左界）/``right_end``（右开界）给出：
      含垂直滚动条的窗口传「宽度 − 滚动条宽 − edge_px」与「宽度 − 滚动条
      宽」——滚动条区域不参与拉伸（保持原生事件）；滚动条隐藏时右带为
      最右 ``edge_px`` 窄带；
    - 窗口角色对边的可用性（标题窗不可南向 / 正文窗不可北向）由调用方
      经 :func:`window_resize_edge` 过滤。
    """
    rs = width - edge_px if right_start is None else right_start
    re = width if right_end is None else right_end
    near_n = y < edge_px
    near_s = y >= height - edge_px
    near_w = x < edge_px
    near_e = rs <= x < re
    if near_n and near_w:
        return "nw"
    if near_n and near_e:
        return "ne"
    if near_s and near_w:
        return "sw"
    if near_s and near_e:
        return "se"
    if near_n:
        return "n"
    if near_s:
        return "s"
    if near_w:
        return "w"
    if near_e:
        return "e"
    return None


def window_resize_edge(edge: str | None, window: str) -> str | None:
    """按窗口角色过滤拉伸边（纯函数，离线测试用）。

    双窗上下堆叠：标题窗下缘贴正文窗（不支持南向），正文窗上缘贴标题窗
    （不支持北向）；东西两边与两角由上/下窗分别承担（北/上角归标题窗，
    南/下角归正文窗）。过滤后为 None 表示该按下不启动拉伸。
    """
    if edge is None:
        return None
    if window == "title" and "s" in edge:
        return None
    if window == "body" and "n" in edge:
        return None
    return edge


def apply_resize_delta(
    x: int,
    y: int,
    w: int,
    h: int,
    edge: str,
    dx: int,
    dy: int,
    min_w: int,
    min_h: int,
) -> tuple[int, int, int, int]:
    """按边/角与拖动增量计算新 ``(x, y, w, h)``（纯函数，离线测试用）。

    与截图窗口 ``_do_resize`` 同逻辑：拖动某条边只改变该方向尺寸、对边
    固定；拖动角同时改两个方向、对角固定；涉及左/上方向时原点同步平移
    （被拖边/角始终跟随鼠标）；新尺寸经 ``max(min, ...)`` 钳制，钳制时
    原点补偿保证固定侧不动。
    """
    new_x, new_y, new_w, new_h = x, y, w, h
    if "e" in edge:
        new_w = max(min_w, w + dx)
    if "s" in edge:
        new_h = max(min_h, h + dy)
    if "w" in edge:
        new_w = max(min_w, w - dx)
        new_x = x + w - new_w
    if "n" in edge:
        new_h = max(min_h, h - dy)
        new_y = y + h - new_h
    return new_x, new_y, new_w, new_h


def resize_band_rects(
    width: int,
    height: int,
    band_px: int,
) -> tuple[
    tuple[int, int, int, int],
    tuple[int, int, int, int],
    tuple[int, int, int, int],
    tuple[int, int, int, int],
]:
    """未锁定态边缘可命中条带的四向矩形（上/下/左/右，窗口本地坐标）。

    分层窗口对 alpha=0 像素命中穿透：透明双窗未绘制区域收不到鼠标事件，
    沿四边绘制低 alpha 条带让拉伸热区确定性可达（悬停高亮叠加其上）。
    四条带在四角各重叠 band_px×band_px（同色同 alpha，叠加加深可忽略）。
    """
    return (
        (0, 0, width, band_px),
        (0, height - band_px, width, band_px),
        (0, 0, band_px, height),
        (width - band_px, 0, band_px, height),
    )


def finish_resize_dims(body_w: int, body_h: int, min_w: int) -> tuple[int, int]:
    """拉伸收尾：正文窗矩形（win32 读回，不含标题带）→ 宿主运行时尺寸。

    宽：正文窗与标题窗同宽联动，即总宽；高：正文窗高即正文高——
    **不得再减 title_h + gap**（body rect 已不含标题带，重复扣减会让
    每次拉伸手势固定少一个标题带高度，实测 121725.log 零位移 −74）。
    """
    return max(min_w, int(body_w)), max(1, int(body_h))
