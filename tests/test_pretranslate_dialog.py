"""注入相关入口（标题内嵌入口条）的纯逻辑离线验证。

不构造 QWidget：覆盖 TitleEntryStrip 的宽度/命中/去重、入口分发
（dispatch_action 的可用性与忽略路径）、图标热区纯函数与图标资产检查。
"""

from __future__ import annotations

from pathlib import Path

from renpy_overlay.bound_window import (
    BoundAction,
    TitleEntryStrip,
    dispatch_action,
    icon_button_size,
)


def _action(aid: str, label: str = "预构建翻译缓存", enabled=None) -> BoundAction:
    return BoundAction(id=aid, label=label, on_click=lambda: None, enabled=enabled)


# ---- 入口条宽度与去重 --------------------------------------------------------


def test_strip_width_empty_single_and_multiple():
    strip = TitleEntryStrip()
    assert strip.is_empty() and strip.width() == 0

    strip.set_actions([_action("a")])
    side = strip.hotzone()
    assert strip.width() == side  # 单入口：一条热区宽

    strip.set_actions([_action("b"), _action("c")])
    assert len(strip.actions) == 3  # 去重合并：b/c 追加，a 保留
    assert strip.width() == 3 * side + 2 * strip._gap  # n×热区 + (n-1)×间隔


def test_strip_set_actions_dedupes_same_id():
    strip = TitleEntryStrip()
    strip.set_actions([_action("prebuild", "旧文案")])
    strip.set_actions([_action("prebuild", "新文案")])
    assert len(strip.actions) == 1
    assert strip.actions[0].label == "新文案"  # 同 id 覆盖


# ---- 命中测试 -----------------------------------------------------------------


def test_action_at_hits_hotzone_and_skips_gap():
    strip = TitleEntryStrip()
    a, b = _action("a"), _action("b")
    strip.set_actions([a, b])
    side = strip.hotzone()
    slot = side + strip._gap

    assert strip.action_at(0) is a  # 第一个热区左缘
    assert strip.action_at(side - 1) is a  # 第一个热区右缘内
    assert strip.action_at(side) is None  # 入口之间的间隔：不命中
    assert strip.action_at(slot) is b  # 第二个热区
    assert strip.action_at(2 * slot) is None  # 越界
    assert strip.action_at(-1) is None  # 负坐标


def test_dispatch_action_none_and_disabled():
    disabled = _action("x", enabled=lambda: False)
    assert dispatch_action(None) is False
    assert dispatch_action(disabled) is False


def test_dispatch_action_enabled_invokes_callback():
    calls: list[str] = []
    action = _action("ok", enabled=lambda: True)
    action.on_click = lambda: calls.append("ok")
    assert dispatch_action(action) is True
    assert calls == ["ok"]


# ---- 图标热区与资产 -----------------------------------------------------------


def test_icon_button_hotzone_matches_title_strip():
    """热区纯函数与入口条常量一致：图标等比边长 + 四周留白。"""
    from renpy_overlay.bound_window import (
        ICON_BUTTON_PAD,
        ICON_DISPLAY_SIZE,
    )

    assert ICON_DISPLAY_SIZE == 24  # 视觉翻倍：与标题窗高度余量匹配（title_h ≈ 72）
    assert icon_button_size() == (ICON_DISPLAY_SIZE + 2 * ICON_BUTTON_PAD,) * 2
    assert icon_button_size()[0] >= 20  # 热区不小于原文字按钮高度
    assert icon_button_size()[0] <= 72  # 热区不得超过标题窗高度（图标溢出即被裁剪）


def test_prebuild_icon_asset_exists_and_tinted():
    """入口图标资产存在且描边已改为淡蓝 #ADD8E6（无残留黑色 #333）。"""
    import renpy_overlay

    svg = (
        Path(renpy_overlay.__file__).parent
        / "assets"
        / "icons"
        / "书籍1_book-one.svg"
    )
    assert svg.is_file(), f"图标资产缺失：{svg}"
    content = svg.read_text(encoding="utf-8")
    assert "#ADD8E6" in content
    assert "#333" not in content
