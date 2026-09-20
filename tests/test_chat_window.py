"""AI 对话窗口的离线验证：需求文案常量与颜色标签纯函数（不实例化 QWidget）。

交互逻辑依赖宿主回调与 Qt 事件循环，遵循 GUI 逻辑离线测试约定：只对可
独立验证的纯部分（文案常量、颜色 → 标签映射）做单元测试；窗口行为由
人工联测覆盖。
"""

from __future__ import annotations

from PyQt6.QtGui import QColor

from renpy_overlay.screenshot.chat_window import (
    CHAT_SYSTEM_PROMPT,
    FRAME_COLOR_NAMES,
    INPUT_PLACEHOLDER,
    TITLE_OCR_FAILED,
    TITLE_OCR_OK,
    TITLE_OCR_RUNNING,
    TITLE_SEND_FAILED,
    TITLE_SEND_OK,
    TITLE_SENDING,
    color_label,
)
from renpy_overlay.screenshot.window import FRAME_COLORS


def test_title_texts_match_specification():
    # 需求给定的标题窗提示文案（经宿主写入流式窗标题窗）
    assert TITLE_SENDING == "对话发送中..."
    assert TITLE_SEND_FAILED == "发送失败"
    assert TITLE_SEND_OK == "发送成功"
    assert TITLE_OCR_RUNNING == "OCR中，速度会稍慢..."
    assert TITLE_OCR_OK == "OCR成功"
    assert TITLE_OCR_FAILED == "OCR失败"


def test_input_placeholder_matches_specification():
    # 需求给定：文本框为空时背景显示的占位文案
    assert INPUT_PLACEHOLDER == "按 Ctrl+Enter 即可发送。"


def test_chat_system_prompt_requests_plain_text():
    # 对话专用系统提示词：要求无标签无格式的纯文本，适配正文窗显示方式
    assert CHAT_SYSTEM_PROMPT.strip()
    assert "纯文本" in CHAT_SYSTEM_PROMPT


def test_color_labels_match_frame_color_order():
    # 八色边框按创建序命名：红橙黄绿青蓝紫黑（与 FRAME_COLORS 一一对应）
    labels = [color_label(color) for color in FRAME_COLORS]
    assert labels == list(FRAME_COLOR_NAMES)
    assert labels == ["红", "橙", "黄", "绿", "青", "蓝", "紫", "黑"]


def test_color_label_unknown_color_falls_back_to_hex():
    # 防御性兜底：非八色的边框色回退十六进制色值（下拉列表仍可区分窗口）
    assert color_label(QColor(1, 2, 3)) == "#010203"
