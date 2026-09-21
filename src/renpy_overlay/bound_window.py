"""标题窗内嵌的「注入相关入口条」：渲染、宽度与命中测试。

设计（替代独立的 BoundWindow 顶层窗口）：入口图标直接绘制在标题窗自绘
内容中、紧跟标题文本右侧 —— 同一窗口同一渲染帧，标题文本长度变化时图标
实时跟随，锁定/拖动/自动跟随各状态行为一致，不再存在跨窗口定位同步。

- **入口抽象层**：:class:`BoundAction` 描述一个入口（id / 文案 / 可用谓词 /
  回调 / 可选图标路径），新增注入相关功能 = 宿主调用 :meth:`TitleEntryStrip.set_actions`
  注册，绘制与命中零改动；图标路径由宿主接线层传入，不写死；
- **命中与分发**：:meth:`TitleEntryStrip.action_at` 按局部 x 坐标做区间命中，
  命中后经 :func:`dispatch_action` 分发（可用性判定与文字/图标形态一致）；
- 本模块为纯逻辑（仅 paint 需要调用方传入 QPainter），不建 QWidget，
  可离线单测宽度与命中路径。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QIcon, QPainter

logger = logging.getLogger("renpy_overlay.bound_window")

#: 绑定窗口与标题/正文窗整体的间距（物理像素，与 _title_gap 同量级）
BOUND_GAP = 8
#: 图标之间的间隔 = 入口热区之间的留白（逻辑像素）
ENTRY_GAP = 8
#: 图标入口的显示边长与热区留白（逻辑像素）：图标按当前取值的 0.5 倍等比
#: 显示（24 → 12）；SVG 矢量渲染无缩放失真
ICON_DISPLAY_SIZE = 12
ICON_BUTTON_PAD = 10
#: 图标热区边长（逻辑像素）：不小于原文字按钮高度（20），点击手感稳定
ENTRY_HOTZONE = ICON_DISPLAY_SIZE + 2 * ICON_BUTTON_PAD


def icon_button_size(
    icon_size: int = ICON_DISPLAY_SIZE, pad: int = ICON_BUTTON_PAD
) -> tuple[int, int]:
    """图标入口的热区尺寸（纯函数，离线测试用）：图标等比边长 + 四周留白。"""
    side = icon_size + 2 * pad
    return side, side


@dataclass
class BoundAction:
    """一个注入相关功能入口（渲染层无关的功能描述）。"""

    id: str  # 稳定标识（日志定位用）
    label: str  # 文案（图标形态下兼作悬浮/无障碍描述）
    on_click: Callable[[], None]
    enabled: Callable[[], bool] | None = None  # None = 恒可用
    icon: str | None = None  # 图标资源路径（SVG/图片）

    def is_enabled(self) -> bool:
        if self.enabled is None:
            return True
        try:
            return bool(self.enabled())
        except Exception:  # pragma: no cover - 谓词异常按不可用处理
            return False


def dispatch_action(action: BoundAction | None) -> bool:
    """统一分发：未命中/不可用时忽略并返回 False，命中分发返回 True。"""
    if action is None:
        return False
    if not action.is_enabled():
        logger.debug("绑定窗口入口不可用，忽略点击：%s", action.id)
        return False
    logger.info("绑定窗口入口点击：%s", action.id)
    action.on_click()
    return True


class TitleEntryStrip:
    """标题文本右侧的图标入口条：宽度、绘制与命中测试（非 QWidget）。"""

    def __init__(self, gap: int = ENTRY_GAP) -> None:
        self._gap = gap
        self._actions: list[BoundAction] = []

    # ---- 入口管理 -----------------------------------------------------------

    def set_actions(self, actions: list[BoundAction]) -> None:
        """整体重建入口（重复 id 覆盖；空列表即清空 —— 跳过注入模式不注入）。"""
        unique: dict[str, BoundAction] = {item.id: item for item in self._actions}
        for action in actions:
            unique[action.id] = action
        self._actions = list(unique.values())

    @property
    def actions(self) -> list[BoundAction]:
        return list(self._actions)

    def is_empty(self) -> bool:
        return not self._actions

    # ---- 布局与命中 ---------------------------------------------------------

    def hotzone(self) -> int:
        """单个入口热区边长（逻辑像素）。"""
        return ENTRY_HOTZONE

    def width(self) -> int:
        """入口条总宽（逻辑像素）：n×热区 + (n-1)×间隔；空条为 0。"""
        n = len(self._actions)
        if n == 0:
            return 0
        return n * ENTRY_HOTZONE + (n - 1) * self._gap

    def action_at(self, local_x: int) -> BoundAction | None:
        """按条内局部 x 做区间命中：落在某入口热区返回该 action，否则 None。"""
        if local_x < 0:
            return None
        slot = ENTRY_HOTZONE + self._gap
        index, offset = divmod(local_x, slot)
        if index >= len(self._actions) or offset >= ENTRY_HOTZONE:
            return None  # 落在入口之间的间隔上：不命中
        return self._actions[index]

    # ---- 绘制 ---------------------------------------------------------------

    def paint(self, painter: QPainter, x: int, y: int) -> None:
        """在标题窗画布上绘制全部入口图标（调用方保证在 paintEvent 内）。"""
        for index, action in enumerate(self._actions):
            if not action.icon:  # pragma: no cover - 现有入口均带图标
                continue
            icon = QIcon(action.icon)
            left = x + index * (ENTRY_HOTZONE + self._gap)
            top = y
            icon.paint(
                painter,
                QRect(
                    left + ICON_BUTTON_PAD,
                    top + ICON_BUTTON_PAD,
                    ICON_DISPLAY_SIZE,
                    ICON_DISPLAY_SIZE,
                ),
            )
