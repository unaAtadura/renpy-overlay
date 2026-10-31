"""流式悬浮窗 style=2 渲染层的离线验证。

不实例化 QWidget（离线测试约定）：验证样式工厂的类选择与回退、构造签名
兼容性，以及 style2 窗口类对宿主触点的接口契约（反射断言，防止宿主触点
演进时 style2 静默失配）。
"""

from __future__ import annotations

import inspect

from renpy_overlay.stream.windows import BodyWindow, TitleWindow
from renpy_overlay.stream.windows_style2 import (
    STYLE2_BG_ALPHA,
    STYLE2_BG_COLOR,
    STYLE2_RADIUS,
    STYLE2_TEXT_COLOR,
    Style2BodyWindow,
    Style2TitleWindow,
    resolve_window_classes,
)

# ---- resolve_window_classes：样式工厂 ---------------------------------------


def test_resolve_classes_style1():
    title_cls, body_cls = resolve_window_classes(1)
    assert title_cls is TitleWindow
    assert body_cls is BodyWindow


def test_resolve_classes_style2():
    title_cls, body_cls = resolve_window_classes(2)
    assert title_cls is Style2TitleWindow
    assert body_cls is Style2BodyWindow


def test_resolve_classes_invalid_falls_back():
    """非法值纵深防御：一律回退 style=1 类对（配置层已保证合法）。"""
    for bad in (0, 3, -1, "2", None, 2.5):
        title_cls, body_cls = resolve_window_classes(bad)
        assert title_cls is TitleWindow, f"{bad!r} 应回退 style=1 标题窗"
        assert body_cls is BodyWindow, f"{bad!r} 应回退 style=1 正文窗"


# ---- 继承与构造签名兼容 ------------------------------------------------------


def test_style2_subclasses_style1():
    """style2 类必须是 style1 类的子类（复用排版/滚动/交互逻辑的基础）。"""
    assert issubclass(Style2TitleWindow, TitleWindow)
    assert issubclass(Style2BodyWindow, BodyWindow)


def test_style2_constructor_signatures_match_style1():
    """构造签名与 style=1 完全一致（宿主装配点无需感知样式差异）。"""
    assert inspect.signature(Style2TitleWindow.__init__) == inspect.signature(
        TitleWindow.__init__
    )
    assert inspect.signature(Style2BodyWindow.__init__) == inspect.signature(
        BodyWindow.__init__
    )


# ---- 接口契约：宿主对窗口的全部触点 ------------------------------------------


def test_style2_body_class_contract():
    """宿主对正文窗触点全集（host_core/interaction/follow/video_flow/history_flow）。"""
    for attr in (
        "feed_text",
        "begin_stream",
        "clear",
        "set_anchor",
        "replace_stream",
        "begin_drag_scroll",
        "update_drag_scroll",
        "end_drag_scroll",
        "hwnd",
        "dpr",
        "show",
        "hide",
        "close",
    ):
        assert hasattr(Style2BodyWindow, attr), f"Style2BodyWindow 缺少 {attr}"


def test_style2_body_property_contract():
    """正文窗属性面：stream_text 为「捕获」按钮内容来源，drag_scroll_active 供宿主判定。"""
    assert isinstance(Style2BodyWindow.stream_text, property)
    assert isinstance(Style2BodyWindow.drag_scroll_active, property)


def test_style2_title_class_contract():
    """宿主对标题窗触点全集（_update_title 唯一出口 / prebuild 入口条 / 高度计算）。"""
    for attr in (
        "set_text",
        "set_title_entries",
        "content_width",
        "fm",
        "hwnd",
        "dpr",
        "show",
        "hide",
        "close",
    ):
        assert hasattr(Style2TitleWindow, attr), f"Style2TitleWindow 缺少 {attr}"


# ---- 视觉常量（已确认决议） --------------------------------------------------


def test_style2_visual_constants():
    """背景 rgba(0,0,0,160) / 白色文字 / 圆角 8px（如需调整仅改本模块常量）。"""
    assert STYLE2_BG_COLOR == "#000000"
    assert STYLE2_BG_ALPHA == 160
    assert STYLE2_TEXT_COLOR == "#FFFFFF"
    assert STYLE2_RADIUS == 8.0
