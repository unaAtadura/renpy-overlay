"""mspaint 风格调色盘取色对话框（PyQt6 移植版）。

移植自参考实现 ``D:/Workspace/Trae_Project/test/palette/color_picker.py``
（tkinter 版，交互与视觉语义保持一致），作为设置窗口颜色配置项
（``body_fill_color``）的取色控件：

- 左侧大区域：色调(Hue) × 饱和度(Saturation) 渐变面，可点选/拖拽取色
  （x=色调 0-239，y=饱和度反向 240-0，与 mspaint 取值范围一致）；
- 右侧竖条：明度(Value) 渐变条，可拖动调节；
- Color | Pure 预览色块（Pure 为饱和度拉满的纯色）；
- RGB / HSV / 十六进制数值面板，与取色实时双向同步（``_updating`` 防回环）；
- 底部一排基本颜色，可一键选中；确定回写、取消放弃。

模块顶部为纯函数区（无 Qt 依赖，离线测试见 ``tests/test_color_picker.py``）；
渐变面构造一次基准位图（明度缩放经 ``CompositionMode_Multiply`` 合成，
拖动全程流畅），明度条用线性渐变（Value 为线性通道，视觉精确等价）。
"""

from __future__ import annotations

import colorsys

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import config

# mspaint 风格的取值范围：色调 0-239，饱和度/明度 0-240（与参考实现一致）
HUE_MAX = 239
SV_MAX = 240

# 取色区域尺寸（像素）：色调/饱和度/明度均为每步 1 像素
AREA_W = 240
AREA_H = 240
BAR_W = 30  # 明度竖条宽度

BASIC_COLORS = [
    "#000000", "#808080", "#800000", "#808000",
    "#008000", "#008080", "#000080", "#800080",
    "#ffffff", "#c0c0c0", "#ff0000", "#ffff00",
    "#00ff00", "#00ffff", "#0000ff", "#ff00ff",
]


# ---------------------------------------------------------------- 颜色转换 --
def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def hsv_to_rgb255(h, s, v):
    """HSV(0-239, 0-240, 0-240) -> (r, g, b)，各分量 0-255。"""
    r, g, b = colorsys.hsv_to_rgb(h / HUE_MAX, s / SV_MAX, v / SV_MAX)
    return round(r * 255), round(g * 255), round(b * 255)


def rgb255_to_hsv(r, g, b):
    """(r, g, b) 0-255 -> HSV(0-239, 0-240, 0-240)。"""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return round(h * HUE_MAX), round(s * SV_MAX), round(v * SV_MAX)


def to_hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def from_hex(text):
    """解析 #RGB / #RRGGBB（大小写不敏感），失败返回 None。"""
    stripped = text.strip().lstrip("#")
    if len(stripped) == 3:
        stripped = "".join(ch * 2 for ch in stripped)
    if len(stripped) != 6:
        return None
    try:
        return tuple(int(stripped[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def is_valid_hex_color(text) -> bool:
    """颜色配置值合法性：与 config.COLOR_HEX_PATTERN 同口径（# 前缀必需）。

    注意与 :func:`from_hex`（对话框输入解析，宽容无 # 前缀）区分：
    配置值判定必须同源正则，避免双口径。
    """
    return (
        isinstance(text, str)
        and config.COLOR_HEX_PATTERN.match(text.strip() or "") is not None
    )


def normalize_hex_color(text) -> str:
    """规范化为 #RRGGBB 大写（3 位扩展为 6 位）；非法返回空串（调用方兜底）。"""
    rgb = from_hex(text) if isinstance(text, str) else None
    return to_hex(rgb) if rgb is not None else ""


def build_hue_sat_plane(v: int = SV_MAX) -> QImage:
    """生成色调×饱和度渐变面位图（x=色调，y=饱和度反向；明度固定 ``v``）。

    返回深拷贝位图（数据与临时 bytearray 断开依赖）；调用方在绘制时经
    ``CompositionMode_Multiply`` 按当前明度缩放（HSV→RGB 的明度通道线性，
    数学上等价于逐像素 ``hsv_to_rgb255(h, s, v)``，避免拖动时全量重算）。
    """
    buf = bytearray(AREA_W * AREA_H * 4)
    index = 0
    for y in range(AREA_H):
        s = SV_MAX - y
        for x in range(AREA_W):
            r, g, b = hsv_to_rgb255(x, s, v)
            # BGRA 字节序（Format_RGB32 小端布局）
            buf[index], buf[index + 1], buf[index + 2], buf[index + 3] = b, g, r, 255
            index += 4
    return QImage(bytes(buf), AREA_W, AREA_H, QImage.Format.Format_RGB32).copy()


class _HueSatPlane(QWidget):
    """HSV 渐变取色面：点选/拖拽发 ``pick(h, s)``；标记环随当前值移动。"""

    pick = pyqtSignal(int, int)

    def __init__(self, dialog: ColorPickerDialog) -> None:
        super().__init__()
        self._dialog = dialog
        self._base = build_hue_sat_plane()
        self.setFixedSize(AREA_W, AREA_H)

    def refresh(self) -> None:
        """明度变化后重绘（Multiply 合成依赖当前明度值）。"""
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        scaled = self._base.copy()
        taper = QPainter(scaled)
        taper.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        gray = round(self._dialog.v / SV_MAX * 255)
        taper.fillRect(scaled.rect(), QColor(gray, gray, gray))
        taper.end()
        painter.drawImage(0, 0, scaled)
        # 当前位置标记环：亮色取黑环、暗色取白环（与参考实现一致）
        outline = "#000" if self._dialog.v >= 128 else "#fff"
        painter.setPen(QColor(outline))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        cy = AREA_H - 1 - self._dialog.s
        painter.drawEllipse(self._dialog.h - 5, cy - 5, 10, 10)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._emit_pick(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._emit_pick(event)

    def _emit_pick(self, event) -> None:
        pos = event.position()
        self.pick.emit(
            int(clamp(round(pos.x()), 0, HUE_MAX)),
            int(clamp(SV_MAX - round(pos.y()), 0, SV_MAX)),
        )


class _ValueBar(QWidget):
    """明度竖条：当前色调/饱和度下的线性明度渐变，拖动发 ``pick(v)``。"""

    pick = pyqtSignal(int)

    def __init__(self, dialog: ColorPickerDialog) -> None:
        super().__init__()
        self._dialog = dialog
        self.setFixedSize(BAR_W, AREA_H)

    def refresh(self) -> None:
        """色调/饱和度变化后重绘（渐变端点颜色依赖二者）。"""
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        h, s = self._dialog.h, self._dialog.s
        top = QColor(*hsv_to_rgb255(h, s, SV_MAX))
        bottom = QColor(*hsv_to_rgb255(h, s, 0))
        # 逐像素渐变的线性等价（Value 通道线性）：线性渐变端点高效且精确
        gradient = QLinearGradient(0, 0, 0, AREA_H)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, bottom)
        painter.fillRect(self.rect(), gradient)
        # 当前位置标记（横条，与参考实现一致）
        y = round((SV_MAX - self._dialog.v) * (AREA_H - 1) / SV_MAX)
        painter.setPen(QColor("#000"))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, y - 2, BAR_W - 1, 4)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._emit_pick(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._emit_pick(event)

    def _emit_pick(self, event) -> None:
        pos = event.position()
        v = SV_MAX - round(pos.y() * SV_MAX / (AREA_H - 1))
        self.pick.emit(int(clamp(v, 0, SV_MAX)))


class ColorPickerDialog(QDialog):
    """调色盘取色对话框：选色 →「确定」回写（selected_color）→ 取消放弃。

    内部状态为 HSV 三元组（初值由 ``initial`` 十六进制解析；非法回退
    红色，同 mspaint 默认）；RGB/HSV/Hex 面板与取色实时双向同步，
    ``_updating`` 标志防止输入框 <-> 状态循环刷新（与参考实现一致）。
    """

    def __init__(self, initial: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择颜色")
        self.setModal(True)

        rgb = from_hex(initial) if is_valid_hex_color(initial) else (255, 0, 0)
        self.h, self.s, self.v = rgb255_to_hsv(*rgb)
        self._updating = False  # 输入框 <-> 状态双向同步的回环抑制
        self._spins: dict[str, QSpinBox] = {}

        self._plane = _HueSatPlane(self)
        self._plane.pick.connect(self._set_hs)
        self._bar = _ValueBar(self)
        self._bar.pick.connect(self._set_v)

        top_row = QHBoxLayout()
        top_row.addWidget(self._plane)
        bar_box = QVBoxLayout()
        bar_box.addWidget(self._bar)
        bar_box.addWidget(QLabel("明度"), alignment=Qt.AlignmentFlag.AlignHCenter)
        top_row.addSpacing(12)
        top_row.addLayout(bar_box)

        # ---- Color | Pure 预览 + 十六进制输入 ------------------------------
        self._color_box = QLabel()
        self._pure_box = QLabel()
        for box in (self._color_box, self._pure_box):
            box.setFixedSize(64, 28)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hex_edit = QLineEdit()
        self._hex_edit.setFixedWidth(80)
        self._hex_edit.editingFinished.connect(self._apply_hex)
        mid_row = QHBoxLayout()
        mid_row.addWidget(self._color_box)
        mid_row.addWidget(self._pure_box)
        mid_row.addStretch(1)
        mid_row.addWidget(QLabel("十六进制"))
        mid_row.addWidget(self._hex_edit)

        # ---- RGB / HSV 数值面板 -------------------------------------------
        nums = QHBoxLayout()
        for _title, fields in (
            ("RGB", (("红", "r", 255), ("绿", "g", 255), ("蓝", "b", 255))),
            ("HSV", (("色调", "h", HUE_MAX), ("饱和度", "s", SV_MAX), ("明度", "v", SV_MAX))),
        ):
            grid = QGridLayout()
            for row, (label, key, hi) in enumerate(fields):
                grid.addWidget(QLabel(label), row, 0)
                spin = QSpinBox()
                spin.setRange(0, hi)
                spin.valueChanged.connect(self._on_spin)
                self._spins[key] = spin
                grid.addWidget(spin, row, 1)
            nums.addLayout(grid)
            nums.addSpacing(12)

        # ---- 基本颜色（16 色，每行 8 个，一键选中）-------------------------
        palette_grid = QGridLayout()
        for i, color in enumerate(BASIC_COLORS):
            button = QPushButton()
            button.setFixedSize(36, 22)
            button.setStyleSheet(f"background: {color};")
            button.setToolTip(color)
            button.clicked.connect(
                lambda _checked=False, c=color: self._apply_hex_text(c)
            )
            palette_grid.addWidget(button, i // 8, i % 8)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top_row)
        layout.addLayout(mid_row)
        layout.addLayout(nums)
        layout.addLayout(palette_grid)
        layout.addWidget(buttons)

        self._refresh_all()

    # ---- 状态更新（取色/输入共用唯一入口） ---------------------------------

    def _set_hs(self, h: int, s: int) -> None:
        self.h, self.s = h, s
        self._bar.refresh()  # 明度条渐变依赖 h、s
        self._refresh_all()

    def _set_v(self, v: int) -> None:
        self.v = v
        self._plane.refresh()  # 取色面明度合成依赖 v
        self._refresh_all()

    def _on_spin(self) -> None:
        if self._updating:
            return
        rgb = tuple(self._spins[key].value() for key in ("r", "g", "b"))
        self._apply_rgb(rgb)

    def _apply_hex(self) -> None:
        if self._updating:
            return
        self._apply_hex_text(self._hex_edit.text())

    def _apply_hex_text(self, text: str) -> None:
        rgb = from_hex(text)
        if rgb:
            self._apply_rgb(rgb)

    def _apply_rgb(self, rgb) -> None:
        self.h, self.s, self.v = rgb255_to_hsv(*rgb)
        self._plane.refresh()
        self._bar.refresh()
        self._refresh_all()

    def _refresh_all(self) -> None:
        """由当前 HSV 状态刷新预览块、十六进制框和各数值输入框。"""
        self._updating = True
        try:
            rgb = hsv_to_rgb255(self.h, self.s, self.v)
            self._color_box.setStyleSheet(f"background: {to_hex(rgb)};")
            pure = hsv_to_rgb255(self.h, SV_MAX, SV_MAX)
            self._pure_box.setStyleSheet(f"background: {to_hex(pure)};")
            for key, value in (
                ("r", rgb[0]),
                ("g", rgb[1]),
                ("b", rgb[2]),
                ("h", self.h),
                ("s", self.s),
                ("v", self.v),
            ):
                spin = self._spins[key]
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)
            self._hex_edit.setText(to_hex(rgb))
        finally:
            self._updating = False

    def selected_color(self) -> str:
        """当前选中的颜色（#RRGGBB 大写；确认后由调用方读取回写）。"""
        return to_hex(hsv_to_rgb255(self.h, self.s, self.v))
