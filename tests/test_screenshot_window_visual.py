"""截图窗口边缘检测的离线验证（八向拉伸阈值逻辑，不实例化 QWidget）。"""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtCore import QPoint

from renpy_overlay.screenshot.window_visual import EDGE_THRESHOLD, edge_at


def _widget(width: int, height: int):
    """duck-typed 替身：edge_at 只依赖 width()/height()。"""
    return SimpleNamespace(width=lambda: width, height=lambda: height)


def test_center_is_not_edge():
    widget = _widget(300, 200)
    assert edge_at(widget, QPoint(150, 100)) is None


def test_four_cardinal_edges():
    widget = _widget(300, 200)
    assert edge_at(widget, QPoint(150, 2)) == "n"
    assert edge_at(widget, QPoint(150, 197)) == "s"
    assert edge_at(widget, QPoint(2, 100)) == "w"
    assert edge_at(widget, QPoint(297, 100)) == "e"


def test_four_corner_edges():
    widget = _widget(300, 200)
    assert edge_at(widget, QPoint(2, 2)) == "nw"
    assert edge_at(widget, QPoint(297, 2)) == "ne"
    assert edge_at(widget, QPoint(2, 197)) == "sw"
    assert edge_at(widget, QPoint(297, 197)) == "se"


def test_threshold_precedence_corner_over_edge():
    widget = _widget(300, 200)
    # 同时满足 top+left 时返回角（nw），而不是先扫到的边
    assert edge_at(widget, QPoint(1, 1)) == "nw"
    assert edge_at(widget, QPoint(298, 198)) == "se"


def test_dynamic_threshold_shrinks_for_small_windows():
    widget = _widget(80, 60)  # 短边 60 → 阈值 = min(14, 9) = 9
    assert EDGE_THRESHOLD == 14
    assert edge_at(widget, QPoint(10, 30)) is None  # 10 >= 9：不在边缘区
    assert edge_at(widget, QPoint(5, 30)) == "w"  # 5 < 9：命中边缘
