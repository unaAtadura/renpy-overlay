"""系统托盘图标：全部悬浮窗一键隐藏/恢复的第二入口（需求 future_托盘图标.txt）。

独立实现（需求备注：代码放在 quick_menu.py 同级）：不依赖宿主类型，全部能力
（受管窗口清单、退出动作）经构造回调注入——与 QuickMenu /
RemoteScreenshotController 的宿主注入约定一致。实现分两层，GUI 与状态机分离
使状态机可离线测试（项目测试不构造真实 QWidget）：

- ``TrayVisibilityController``：纯逻辑状态机。以「点击隐藏瞬间的 isVisible()
  实测值」为唯一判据生成全量快照，恢复时仅放回快照中可见的条目，并把隐藏
  期间被业务流程复活的窗口压回（校正式恢复）——预构建后台弹窗、布局锁定
  隐藏的截图窗、自动化压制的提示框在快照时刻均不可见，天然不被恢复，
  无需逐窗口白名单；
- ``TrayIconController``：QSystemTrayIcon + QMenu 薄壳。菜单仅两项
  （显示窗口/隐藏窗口、退出），退出复用宿主 request_close（与快捷菜单同一
  函数引用，单一退出路径）；不调用 showMessage、不 setToolTip、不连
  activated 信号（需求：无需任何提示）。

全部方法仅在 Qt 主线程调用（托盘菜单动作本就在主线程分发）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger("renpy_overlay.tray_icon")

#: 菜单项文案（需求：初始为「隐藏窗口」，隐藏后切换为「显示窗口」）
HIDE_LABEL = "隐藏窗口"
SHOW_LABEL = "显示窗口"
#: 退出菜单项文案（与快捷菜单「退出」同文案、同行为）
QUIT_LABEL = "退出"

#: 图标多分辨率档位：QIcon 按 DPI 的小图标尺寸自动取档
TRAY_ICON_SIZES = (16, 24, 32, 48, 64)
#: 浅蓝色圆形底色（需求）
TRAY_CIRCLE_COLOR = QColor(135, 206, 250)
#: 圆形中央的白色大写字母（需求）
TRAY_TEXT = "RO"


def make_ro_icon() -> QIcon:
    """绘制托盘图标：浅蓝色圆形 + 白色加粗大写 "RO"（纯绘制，无资产文件）。

    QPainter 动态绘制，进程内一次调用即弃；各档位经 ``QIcon.addPixmap``
    收录，Windows 按 DPI 的小图标尺寸自动取档，无需额外多分辨率代码。
    """
    icon = QIcon()
    for size in TRAY_ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(TRAY_CIRCLE_COLOR)
        painter.drawEllipse(0, 0, size, size)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(8, int(size * 0.42)))
        painter.setFont(font)
        painter.setPen(QPen(Qt.GlobalColor.white))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, TRAY_TEXT)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class ManagedItem:
    """托盘受管条目适配：三个回调统一「单个 QWidget」与「窗口组/宿主状态」。

    取值示例（宿主 ``_tray_managed_items`` 组装）：

    - QWidget：``w.isVisible`` / ``w.hide`` / ``w.show``；
    - 流式双窗（合一条目，必须走 ``_set_visible`` 维护 ``_visible`` 标志，
      否则截图翻译流程的 was_visible 判定与跟随循环状态失真）：
      ``lambda: host._visible`` / ``lambda: host._set_visible(False)`` /
      ``lambda: host._set_visible(True)``；
    - 框选提示窗组：QuickMenu 的 ``frame_overlay_shown`` /
      ``hide_frame_overlay`` / ``show_frame_overlay``（恢复序列含自动化
      压制帧重放，抓屏时序不得复活被压制的提示框）。
    """

    __slots__ = ("is_visible", "hide", "restore")

    def __init__(
        self,
        is_visible: Callable[[], bool],
        hide: Callable[[], None],
        restore: Callable[[], None],
    ) -> None:
        self.is_visible = is_visible
        self.hide = hide
        self.restore = restore


def _safe(call: Callable[[], None]) -> bool:
    """调用窗口原语；已销毁 Qt 对象（RuntimeError）静默跳过并返回 False。"""
    try:
        call()
        return True
    except RuntimeError:  # wrapped C/C++ object deleted（销毁竞态）
        logger.debug("托盘操作遇到已销毁的 Qt 对象，已跳过", exc_info=True)
        return False


def _is_visible_safe(item: ManagedItem) -> bool:
    try:
        return bool(item.is_visible())
    except RuntimeError:  # 已销毁按不可见处理（不会去恢复它）
        return False


class TrayVisibilityController:
    """全部受管窗口的隐藏/恢复状态机（仅 Qt 主线程使用）。

    构造注入 ``items_provider``（每次现取受管条目清单，不持有陈旧窗口
    引用）与 ``set_label``（菜单项文案回写）。状态写位/清空时机：快照仅在
    点击「隐藏窗口」时刻生成，恢复分支执行完毕立即清空。

    幂等：隐藏态重复触发隐藏、非隐藏态直接触发恢复均 no-op。
    """

    def __init__(
        self,
        items_provider: Callable[[], list[ManagedItem]],
        set_label: Callable[[str], None],
    ) -> None:
        self._items_provider = items_provider
        self._set_label = set_label
        self._hidden = False
        self._snapshot: list[tuple[ManagedItem, bool]] = []

    @property
    def hidden(self) -> bool:
        """当前是否处于「托盘隐藏全部窗口」状态（宿主跟随循环短路用）。"""
        return self._hidden

    def menu_label(self) -> str:
        """菜单项当前文案（与状态一一对应）。"""
        return SHOW_LABEL if self._hidden else HIDE_LABEL

    def toggle(self) -> None:
        """菜单唯一入口：非隐藏态隐藏全部，隐藏态按快照恢复。"""
        if self._hidden:
            self.restore()
        else:
            self.hide_all()

    def hide_all(self) -> None:
        """隐藏分支：全量快照（isVisible 实测值）后隐藏当前可见的条目。"""
        if self._hidden:
            return
        items = self._items_provider()
        self._snapshot = []
        visible_count = 0
        for item in items:
            visible = _is_visible_safe(item)
            self._snapshot.append((item, visible))
            if visible:
                _safe(item.hide)
                visible_count += 1
        self._hidden = True
        self._set_label(self.menu_label())
        logger.info("托盘已隐藏全部窗口（受管 %d 项，其中 %d 项可见）", len(items), visible_count)

    def restore(self) -> None:
        """恢复分支（校正式恢复）：先压回、后放回，不依赖条目对象身份。

        宿主清单每次现取**重建** ManagedItem 对象（实测缺陷 211630：以
        条目对象 id 匹配快照永不命中，放回恒 0 项——流式窗看似恢复实为
        跟随循环短路解除后的自动重显，框选提示窗组与遥控 A/B 则无此
        机制，永久丢失）。修复后的两步式恢复：

        1. 压回：此刻全部窗口仍处隐藏态，现取清单中凡当前可见（隐藏期间
           被业务流程复活，如锁定期热键截图翻译放出的提示窗）即压回——
           放回尚未发生，不会误伤即将恢复的窗口；
        2. 放回：直接对快照条目调用 restore（回调闭包持有隐藏前的窗口/
           宿主存活对象，作用于当时的窗口；销毁竞态由 _safe 兜底）。
        """
        if not self._hidden:
            return
        snapshot_visible = {id(item) for item, visible in self._snapshot if visible}
        suppressed = 0
        for item in self._items_provider():
            if id(item) not in snapshot_visible and _is_visible_safe(item):
                _safe(item.hide)
                suppressed += 1
        restored = sum(
            _safe(item.restore) for item, visible in self._snapshot if visible
        )
        self._snapshot = []
        self._hidden = False
        self._set_label(self.menu_label())
        logger.info(
            "托盘已恢复窗口（放回 %d 项，压回隐藏期间复活的 %d 项）", restored, suppressed
        )


class TrayIconController:
    """QSystemTrayIcon 薄壳：图标、两项目菜单与动作分发（仅 Qt 主线程使用）。

    托盘不可用（``isSystemTrayAvailable`` 为 False，远程会话/组策略）或
    构造异常时降级为 no-op：``hidden`` 恒 False，宿主跟随循环短路检查自然
    旁路，主程序功能不受影响。
    """

    def __init__(
        self,
        items_provider: Callable[[], list[ManagedItem]],
        on_quit: Callable[[], None],
    ) -> None:
        self.visibility = TrayVisibilityController(items_provider, self._set_menu_label)
        self._menu: QMenu | None = None
        self._toggle_action = None
        self._tray: QSystemTrayIcon | None = None
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                logger.warning("系统托盘不可用，托盘图标功能降级为无（主程序不受影响）")
                return
            menu = QMenu()
            toggle_action = menu.addAction(self.visibility.menu_label())
            toggle_action.triggered.connect(self.visibility.toggle)
            # 退出与快捷菜单同一动作：on_quit 即宿主 request_close（单一退出路径）
            quit_action = menu.addAction(QUIT_LABEL)
            quit_action.triggered.connect(on_quit)
            tray = QSystemTrayIcon(make_ro_icon())
            tray.setContextMenu(menu)
            tray.show()
            self._menu = menu
            self._toggle_action = toggle_action
            self._tray = tray
            logger.info("托盘图标已创建（右键菜单：%s/%s）", HIDE_LABEL, QUIT_LABEL)
        except Exception:  # pragma: no cover - 平台差异兜底
            logger.exception("托盘图标创建失败，功能降级为无（主程序不受影响）")
            self.shutdown()

    @property
    def hidden(self) -> bool:
        """当前是否处于「托盘隐藏全部窗口」状态（宿主对外的唯一状态读点）。

        代理到内层状态机：跟随循环（follow.py）以 ``self._tray.hidden``
        短路周期定位与置顶重申——外壳必须暴露该属性，否则 AttributeError
        使跟随循环每 tick 报错（实测缺陷回归：薄壳对宿主的状态出口必须
        与宿主访问面对齐，与 shutdown 同为外壳公有接口）。
        """
        return self.visibility.hidden

    def _set_menu_label(self, text: str) -> None:
        """状态机文案回写出口：菜单项 setText 切换（无气泡/悬浮提示）。"""
        if self._toggle_action is not None:
            self._toggle_action.setText(text)

    def shutdown(self) -> None:
        """宿主退出时调用：移除托盘图标并销毁菜单（幂等）。"""
        if self._tray is not None:
            try:
                self._tray.hide()
                self._tray.deleteLater()
            except RuntimeError:  # pragma: no cover - 已销毁
                pass
            self._tray = None
        if self._menu is not None:
            try:
                self._menu.deleteLater()
            except RuntimeError:  # pragma: no cover - 已销毁
                pass
            self._menu = None
        self._toggle_action = None
