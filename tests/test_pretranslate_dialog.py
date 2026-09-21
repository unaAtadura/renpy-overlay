"""预构建弹窗与绑定窗口的纯逻辑离线验证（遵循 GUI 离线测试约定）。

不构造 QWidget / QDialog：只验证模块级纯函数（勾选收集、进度文案、终态
文案）与 BoundAction / 渲染规格（鸭子层，无需 QApplication）。
"""

from __future__ import annotations

from renpy_overlay.bound_window import BoundAction, button_specs
from renpy_overlay.pretranslate_dialog import (
    collect_checked,
    finished_text,
    format_counting,
    format_progress,
)

# ---- 弹窗纯函数 ------------------------------------------------------------


def test_collect_checked_keeps_display_order():
    items = [("b.rpy", False), ("a.rpy", True), ("c.rpy", True), ("d.rpy", False)]
    assert collect_checked(items) == ["a.rpy", "c.rpy"]
    assert collect_checked([]) == []
    assert collect_checked([("x.rpy", False)]) == []


def test_format_counting_discovered():
    assert format_counting(0) == "统计中…已发现 0 条原文"
    assert format_counting(12345) == "统计中…已发现 12345 条原文"


def test_format_progress_base_and_extras():
    assert format_progress(0, 10000, 0, 0) == "0/10000"
    assert format_progress(12, 340, 0, 0) == "12/340"
    assert format_progress(12, 340, 3, 0) == "12/340（失败 3）"
    assert format_progress(12, 340, 3, 2) == "12/340（失败 3，跳过 2）"
    assert format_progress(340, 340, 0, 5) == "340/340（跳过 5）"


def test_finished_text_known_and_unknown():
    assert finished_text("completed") == "本轮预构建完成。"
    assert finished_text("cancelled") == "已取消（已入库条目保留，可重新进入继续）。"
    assert "熔断" in finished_text("circuit_break")
    assert finished_text("error") == "预构建异常终止，详见日志。"
    assert finished_text("mystery") == finished_text("error")  # 未知状态回退


# ---- 绑定窗口入口抽象 -------------------------------------------------------


def test_bound_action_enabled_default_and_predicates():
    calls = []
    action = BoundAction("a", "入口", on_click=lambda: calls.append(1))
    assert action.is_enabled() is True  # 默认恒可用

    gated = BoundAction("b", "入口", on_click=lambda: None, enabled=lambda: False)
    assert gated.is_enabled() is False

    def boom():
        raise RuntimeError("谓词异常")

    broken = BoundAction("c", "入口", on_click=lambda: None, enabled=boom)
    assert broken.is_enabled() is False  # 谓词异常按不可用处理


def test_button_specs_view():
    actions = [
        BoundAction("a", "预构建翻译缓存", on_click=lambda: None),
        BoundAction("b", "禁用项", on_click=lambda: None, enabled=lambda: False),
    ]
    assert button_specs(actions) == [("预构建翻译缓存", True), ("禁用项", False)]
    assert button_specs([]) == []


def test_bound_action_click_dispatch_via_renderer_helper():
    """TextButtonRenderer._dispatch 的可用性与分发逻辑（不建真实按钮）。"""
    from renpy_overlay.bound_window import TextButtonRenderer

    clicks: list[str] = []
    enabled = BoundAction("a", "A", on_click=lambda: clicks.append("a"))
    disabled = BoundAction("b", "B", on_click=lambda: clicks.append("b"), enabled=lambda: False)
    runner = TextButtonRenderer._dispatch(enabled)
    runner()
    assert clicks == ["a"]
    TextButtonRenderer._dispatch(disabled)()  # 不可用：不分发
    assert clicks == ["a"]


def test_bound_window_declares_own_painting():
    """绑定窗口必须自带渲染（paintEvent/showEvent）。

    全透明外壳若不进入 Qt 渲染管线，真实合成器下可能永远不可见 ——
    实测缺陷（日志已 show 但屏幕无窗）：以方法覆写存在性钉住，防回退。
    """

    from renpy_overlay.bound_window import BoundWindow

    assert "paintEvent" in BoundWindow.__dict__, "缺少自绘底板的 paintEvent"
    assert "showEvent" in BoundWindow.__dict__, "缺少显示即重绘的 showEvent"


# ---- 尺寸换算与按钮宽度（振荡与截断缺陷的回归） ------------------------------


def test_logical_to_physical_scales_and_clamps():
    """逻辑→物理换算：缩放屏按 dpr 放大，dpr<1 钳为 1（不再衰减振荡）。"""
    from renpy_overlay.bound_window import logical_to_physical

    assert logical_to_physical(112, 100, 1.0) == (112, 100)
    assert logical_to_physical(112, 100, 1.25) == (140, 125)
    assert logical_to_physical(112, 100, 1.5) == (168, 150)
    assert logical_to_physical(112, 100, 0.8) == (112, 100)  # 钳制下限


def test_text_button_width_fits_long_labels():
    """按钮最小宽 = 文字宽 + 左右留白 + 余量，长文案不再被截断。"""
    from renpy_overlay.bound_window import BUTTON_WIDTH_SLACK, MARGIN, text_button_width

    assert text_button_width(0) == 2 * MARGIN + BUTTON_WIDTH_SLACK
    # 「预构建翻译缓存」7 字 × 16px ≈ 112 → 最小宽应明显大于文字宽
    assert text_button_width(112) == 112 + 2 * MARGIN + BUTTON_WIDTH_SLACK
