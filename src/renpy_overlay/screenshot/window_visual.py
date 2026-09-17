"""截图窗口的绘制与边缘检测纯函数（从参考实现 recognize_window.py 提取）。

只做绘制与几何判定，不持有状态：``ScreenshotWindow.paintEvent`` 与鼠标事件
按需调用，便于离线验证（阈值逻辑见 ``tests/test_screenshot_window_visual.py``）。
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen

#: 边缘拉伸判定阈值（像素）：动态上限，不超过窗口短边的 15%
EDGE_THRESHOLD = 14
#: 鼠标靠近可拉伸边缘时的亮白高亮色（与参考实现一致）
HOVER_EDGE_COLOR = QColor(255, 255, 255, 200)
#: 四角手柄颜色（常驻视觉提示的半透明白）
GRIP_COLOR = QColor(255, 255, 255, 60)
#: 拉伸中尺寸标签的底色与文字色
SIZE_LABEL_BG = QColor(0, 0, 0, 170)
SIZE_LABEL_TEXT = QColor(255, 255, 255, 230)


def edge_at(widget, pos) -> str | None:
    """检测 ``pos``（widget 局部坐标）落在哪个拉伸边缘；内部返回 None。

    动态阈值 ``min(EDGE_THRESHOLD, 短边×15%)``：小窗口下避免边缘区侵占内容。
    """
    x, y = pos.x(), pos.y()
    w, h = widget.width(), widget.height()
    t = min(EDGE_THRESHOLD, int(min(w, h) * 0.15))
    top = y < t
    bottom = y > h - t
    left = x < t
    right = x > w - t
    if top and left:
        return "nw"
    if top and right:
        return "ne"
    if bottom and left:
        return "sw"
    if bottom and right:
        return "se"
    if top:
        return "n"
    if bottom:
        return "s"
    if left:
        return "w"
    if right:
        return "e"
    return None


def draw_grip_handles(painter: QPainter, rect: QRect) -> None:
    """四角缩放手柄：常驻的白色半透明短线（视觉提示可拉伸）。"""
    grip = 12
    gap = 2
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(GRIP_COLOR)
    r = rect
    corners = [
        ("nw", r.left() + gap, r.top() + gap),
        ("ne", r.right() - gap - grip, r.top() + gap),
        ("sw", r.left() + gap, r.bottom() - gap - grip),
        ("se", r.right() - gap - grip, r.bottom() - gap - grip),
    ]
    for corner, cx, cy in corners:
        if "n" in corner:
            painter.drawRect(cx, cy, grip, 3)
        if "s" in corner:
            painter.drawRect(cx, cy + grip - 3, grip, 3)
        if "w" in corner:
            painter.drawRect(cx, cy, 3, grip)
        if "e" in corner:
            painter.drawRect(cx + grip - 3, cy, 3, grip)


def draw_edge_highlight(painter: QPainter, rect: QRect, edge: str) -> None:
    """鼠标靠近可拉伸边缘时的亮白反馈线（八向）。"""
    hw = 3
    hl_pen = QPen(HOVER_EDGE_COLOR)
    hl_pen.setWidth(hw)
    painter.setBrush(hl_pen.color())
    painter.setPen(hl_pen)
    r = rect
    if edge == "n":
        painter.drawLine(r.topLeft().x() + hw, r.top() + 1, r.topRight().x() - hw, r.top() + 1)
    elif edge == "s":
        painter.drawLine(
            r.bottomLeft().x() + hw, r.bottom() - 1, r.bottomRight().x() - hw, r.bottom() - 1
        )
    elif edge == "w":
        painter.drawLine(r.left() + 1, r.topLeft().y() + hw, r.left() + 1, r.bottomLeft().y() - hw)
    elif edge == "e":
        painter.drawLine(
            r.right() - 1, r.topRight().y() + hw, r.right() - 1, r.bottomRight().y() - hw
        )
    elif edge == "nw":
        painter.drawLine(r.topLeft().x() + 1, r.top() + 1, r.topLeft().x() + 30, r.top() + 1)
        painter.drawLine(r.left() + 1, r.topLeft().y() + 1, r.left() + 1, r.topLeft().y() + 30)
    elif edge == "ne":
        painter.drawLine(r.topRight().x() - 30, r.top() + 1, r.topRight().x() - 1, r.top() + 1)
        painter.drawLine(r.right() - 1, r.topRight().y() + 1, r.right() - 1, r.topRight().y() + 30)
    elif edge == "sw":
        painter.drawLine(
            r.bottomLeft().x() + 1, r.bottom() - 1, r.bottomLeft().x() + 30, r.bottom() - 1
        )
        painter.drawLine(r.left() + 1, r.bottomLeft().y() - 30, r.left() + 1, r.bottomLeft().y() - 1)
    elif edge == "se":
        painter.drawLine(
            r.bottomRight().x() - 30, r.bottom() - 1, r.bottomRight().x() - 1, r.bottom() - 1
        )
        painter.drawLine(
            r.right() - 1, r.bottomRight().y() - 30, r.right() - 1, r.bottomRight().y() - 1
        )


def draw_size_label(painter: QPainter, rect: QRect, width: int, height: int) -> None:
    """拉伸中在窗口中央显示实时尺寸 ``宽 × 高``。"""
    size_text = f"{width} × {height}"
    font = QFont("Consolas", 11)
    painter.setFont(font)
    fm = QFontMetrics(font)
    text_w = fm.horizontalAdvance(size_text)
    text_h = fm.height()
    pad = 6
    bg_rect = QRect(0, 0, text_w + pad * 2, text_h + pad)
    bg_rect.moveCenter(rect.center())
    painter.setBrush(SIZE_LABEL_BG)
    pen = QPen(QColor(255, 255, 255, 80))
    pen.setWidth(1)
    painter.setPen(pen)
    painter.drawRoundedRect(bg_rect, 4, 4)
    painter.setPen(SIZE_LABEL_TEXT)
    painter.drawText(bg_rect, Qt.AlignmentFlag.AlignCenter, size_text)
