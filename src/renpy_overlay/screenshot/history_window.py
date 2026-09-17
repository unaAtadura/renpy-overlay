"""截图历史浏览窗口：常规窗口浏览 ``screenshot.db`` 中的截图与译文。

布局（需求给定）：左侧自上而下为「日期选择（首末记录日期标签 + 年/月/日
下拉）→ 缩略图条带 → 译文文本框」，右侧为选中截图的原始图片。

缩略图条带（:class:`_ThumbStrip`）交互：
- 从左到右按时间排列，左侧早期、右侧后期；滚轮上滚看早期、下滚看后期；
- 按住左键左右拖动浏览；
- 条带中垂线扫到的缩略图为候选，停留 1 秒后自动选中（读译文与原图）；
- 左键点击缩略图跳过延迟直接选中；
- 日期下拉选择后跳转到该日期后的第一张（无则最后一张），居中并按同一
  延迟逻辑自动选中。

性能策略：固定槽宽布局（无需解码即可排版）；仅解码可见槽 ±2 范围的
BLOB；QPixmap 与 bytes 双层 LRU 缓存，大库不卡顿。窗口关闭 = 隐藏
（单例复用，由宿主持有）。
"""

from __future__ import annotations

import calendar
import logging
from bisect import bisect_left
from datetime import datetime

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .store import ScreenshotStore
from .viewer_window import ImageViewerWindow

logger = logging.getLogger("renpy_overlay.screenshot.history_window")

#: 缩略图条带槽尺寸与间距（固定槽宽：不解码即可排版，图片 contain 居中）
SLOT_W = 200
SLOT_H = 140
SLOT_GAP = 6
#: 中垂线自动选中的停留时长（毫秒，需求：1 秒延迟）
AUTO_SELECT_DELAY_MS = 1000
#: 点击 vs 拖动的判定阈值（像素）
CLICK_MOVE_TOLERANCE = 4
#: QPixmap / bytes 缓存上限（LRU 语义：超限丢弃最早访问的条目）
PIXMAP_CACHE_LIMIT = 64
BYTES_CACHE_LIMIT = 128
#: 时间显示格式（记录列表与日期标签共用）
TIME_FORMAT = "%Y-%m-%d %H:%M"


class _ThumbStrip(QWidget):
    """自绘水平缩略图条带：滚动 / 拖动浏览 + 中垂线延迟选中。

    选中模式（需求）：默认自动选中（中垂线停留 1 秒）；左键点击某张缩略图
    立即强制选中并**关闭自动选中**（此后中垂线移动不再改变选中项）；滚动
    滚轮时**关闭强制选中、重新启用自动选中**。日期跳转（center_on）同样
    开启新一轮自动选中。
    """

    def __init__(self, store: ScreenshotStore | None, on_selected) -> None:
        super().__init__()
        self._store = store
        self._on_selected = on_selected  # 回调 (ocr_text, original_bytes) | None
        self._entries: list[tuple[int, int]] = []  # (id, ts) 按 ts 升序
        self._offset = 0  # 水平滚动偏移（像素，0 = 最早期）
        self._selected_index = -1
        self._hover_candidate = -1  # 中垂线当前命中槽（-1 = 无）
        self._forced = False  # True = 点击强制选中：中垂线不再自动改变选中项
        self._pixmap_cache: dict[int, QPixmap] = {}
        self._pixmap_order: list[int] = []
        self._bytes_cache: dict[int, bytes] = {}
        self._bytes_order: list[int] = []
        self._dragging = False
        self._press_pos: QPoint | None = None
        self._select_timer = QTimer(self)
        self._select_timer.setSingleShot(True)
        self._select_timer.setInterval(AUTO_SELECT_DELAY_MS)
        self._select_timer.timeout.connect(self._on_auto_select_timeout)
        self.setMouseTracking(True)
        self.setMinimumHeight(SLOT_H + 2 * SLOT_GAP)

    # ---- 数据与布局 ---------------------------------------------------------

    def set_entries(self, entries: list[tuple[int, int]]) -> None:
        self._entries = list(entries)
        self._offset = 0
        self._selected_index = -1
        self._hover_candidate = -1
        self._forced = False
        self._select_timer.stop()
        self.update()

    def total_width(self) -> int:
        if not self._entries:
            return 0
        return len(self._entries) * SLOT_W + (len(self._entries) - 1) * SLOT_GAP

    def slot_rects(self) -> list[tuple[int, int, int, int]]:
        """每个槽的内容坐标 ``(x, y, w, h)``（x 随 _offset 平移由调用方处理）。"""
        return [
            (i * (SLOT_W + SLOT_GAP), SLOT_GAP, SLOT_W, SLOT_H)
            for i in range(len(self._entries))
        ]

    def _index_at_content_x(self, content_x: float) -> int:
        """内容坐标 → 槽序号；不在任何槽内（含空隙）返回 -1。"""
        if not self._entries:
            return -1
        step = SLOT_W + SLOT_GAP
        index = int(content_x // step)
        if 0 <= index < len(self._entries) and content_x - index * step <= SLOT_W:
            return index
        return -1

    def _center_index(self) -> int:
        return self._index_at_content_x(self.width() / 2 + self._offset)

    def _clamp_offset(self) -> None:
        max_offset = max(0, self.total_width() - self.width())
        self._offset = min(max(self._offset, 0), max_offset)

    # ---- 缓存与懒加载 -------------------------------------------------------

    def _pixmap_for(self, record_id: int) -> QPixmap | None:
        if record_id in self._pixmap_cache:
            return self._pixmap_cache[record_id]
        raw = self._bytes_for(record_id)
        if not raw:
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            return None
        self._pixmap_cache[record_id] = pixmap
        self._pixmap_order.append(record_id)
        self._evict(self._pixmap_cache, self._pixmap_order, PIXMAP_CACHE_LIMIT)
        return pixmap

    def _bytes_for(self, record_id: int) -> bytes | None:
        if record_id in self._bytes_cache:
            return self._bytes_cache[record_id]
        if self._store is None:
            return None
        raw = self._store.thumbnail(record_id)
        if not raw:
            return None
        self._bytes_cache[record_id] = raw
        self._bytes_order.append(record_id)
        self._evict(self._bytes_cache, self._bytes_order, BYTES_CACHE_LIMIT)
        return raw

    @staticmethod
    def _evict(cache: dict, order: list[int], limit: int) -> None:
        while len(order) > limit:
            oldest = order.pop(0)
            cache.pop(oldest, None)

    # ---- 选中 ---------------------------------------------------------------

    def select(self, index: int) -> None:
        """立即选中第 ``index`` 槽：读译文与原图并回调展示。"""
        if not 0 <= index < len(self._entries):
            return
        self._selected_index = index
        record_id = self._entries[index][0]
        payload = None
        if self._store is not None:
            payload = self._store.record(record_id)
        logger.debug("选中截图记录 id=%s（%s）", record_id, "命中" if payload else "读取失败")
        if callable(self._on_selected):
            self._on_selected(payload)
        self.update()

    def select_forced(self, index: int) -> None:
        """左键点击选中：立即选中并关闭自动选中（中垂线此后不再改变选中项）。"""
        if not 0 <= index < len(self._entries):
            return
        self._forced = True
        self._select_timer.stop()
        self._hover_candidate = index
        self.select(index)

    def _restore_auto_select(self) -> None:
        """滚轮浏览：关闭强制选中，重新启用中垂线自动选中。"""
        self._forced = False

    def center_on(self, index: int) -> None:
        """把第 ``index`` 槽滚动到中垂线位置，并按自动选中逻辑处理（1s 延迟）。"""
        if not 0 <= index < len(self._entries):
            return
        self._forced = False  # 日期跳转 = 新一轮浏览：恢复自动选中
        self._offset = index * (SLOT_W + SLOT_GAP) + SLOT_W // 2 - self.width() // 2
        self._clamp_offset()
        self._hover_candidate = self._center_index()
        if self._hover_candidate != -1:
            self._select_timer.start()  # 居中后按中垂线同一逻辑延迟自动选中
        self.update()

    def _on_auto_select_timeout(self) -> None:
        if self._forced:
            return  # 强制选中生效期间中垂线不自动改选
        if self._center_index() == self._hover_candidate and self._hover_candidate != -1:
            self.select(self._hover_candidate)

    # ---- 鼠标 / 滚轮 --------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0 and event.pixelDelta() is not None:
            delta = int(event.pixelDelta().y() * 8)
        if delta != 0:
            self._restore_auto_select()  # 滚轮浏览 = 重新启用自动选中（与横向滚动共存）
            # 上滚（delta>0）= 浏览早期（offset 减小）；下滚 = 后期
            self._offset += (-1 if delta > 0 else 1) * SLOT_W
            self._clamp_offset()
            self.update()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._press_pos = QPoint(event.position().toPoint())
            self._select_timer.stop()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._dragging and self._press_pos is not None:
            self._offset -= pos.x() - self._press_pos.x()
            self._press_pos = QPoint(pos)
            self._clamp_offset()
            self.update()
            event.accept()
            return
        # 悬停中：跟随中垂线更新候选并重启延迟计时（同一张不重启 → 1 秒后选中）
        if self._forced:
            event.accept()
            return  # 强制选中期间：中垂线移动不再改变候选与选中项
        candidate = self._center_index()
        if candidate != self._hover_candidate:
            self._hover_candidate = candidate
            if candidate == -1:
                self._select_timer.stop()
            else:
                self._select_timer.start()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        was_click = (
            self._dragging
            and self._press_pos is not None
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            <= CLICK_MOVE_TOLERANCE
        )
        self._dragging = False
        self._press_pos = None
        if was_click:
            # 左键点击缩略图：跳过延迟立即强制选中，并关闭自动选中（需求）
            index = self._index_at_content_x(event.position().toPoint().x() + self._offset)
            if index != -1:
                self.select_forced(index)
        event.accept()

    # ---- 绘制 ---------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtGui import QPainter

        try:
            painter = QPainter(self)
        except Exception:  # pragma: no cover - 窗口销毁竞态
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(24, 24, 24))
        if not self._entries:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无截图记录")
            painter.end()
            return
        for index, (record_id, ts) in enumerate(self._entries):
            x = index * (SLOT_W + SLOT_GAP) - self._offset
            if x + SLOT_W < 0 or x > self.width():
                continue  # 可视区外不绘制
            rect_y, rect_h = SLOT_GAP, SLOT_H
            if index == self._selected_index:
                painter.setPen(QColor(255, 255, 255))
            elif index == self._hover_candidate:
                painter.setPen(QColor(120, 180, 255))
            else:
                painter.setPen(QColor(70, 70, 70))
            painter.drawRect(x - 1, rect_y - 1, SLOT_W + 2, rect_h + 2)
            pixmap = self._pixmap_for(record_id)
            if pixmap is not None:
                scaled = pixmap.scaled(
                    SLOT_W,
                    SLOT_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawPixmap(
                    x + (SLOT_W - scaled.width()) // 2,
                    rect_y + (SLOT_H - scaled.height()) // 2,
                    scaled,
                )
            else:
                painter.setPen(QColor(120, 120, 120))
                painter.drawText(
                    x,
                    rect_y,
                    SLOT_W,
                    rect_h,
                    Qt.AlignmentFlag.AlignCenter,
                    datetime.fromtimestamp(ts / 1000).strftime(TIME_FORMAT),
                )
        painter.end()


class _PanImageScroll(QScrollArea):
    """原图浏览区：左键拖拽平移查看；位移极小的 press-release 判为单击（弹窗看原图）。

    与条带点击/弹窗关闭共用同一容差惯例（``CLICK_MOVE_TOLERANCE``）：
    拖拽平移与单击弹窗互不误触。
    """

    def __init__(self, on_click=None) -> None:
        super().__init__()
        self._on_click = on_click
        self._panning = False
        self._press_global: QPoint | None = None
        self._last_global: QPoint | None = None
        # 鼠标事件落在 viewport 上而非 QScrollArea 本身：用事件过滤器统一接管
        self.viewport().installEventFilter(self)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._press_global = QPoint(event.globalPosition().toPoint())
            self._last_global = QPoint(self._press_global)
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return True
        if etype == QEvent.Type.MouseMouseMove and self._panning and self._last_global is not None:
            current = event.globalPosition().toPoint()
            delta = current - self._last_global
            self._last_global = QPoint(current)
            # 拖拽平移：内容跟手反向滚动（与拖动方向一致）
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return True
        if etype == QEvent.Type.MouseButtonRelease and self._panning:
            self._panning = False
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            press_global = self._press_global
            self._press_global = None
            self._last_global = None
            if press_global is not None:
                moved = event.globalPosition().toPoint() - press_global
                if moved.manhattanLength() <= CLICK_MOVE_TOLERANCE:
                    if callable(self._on_click):
                        self._on_click()  # 单击（非拖拽）：弹出原图查看窗口
            event.accept()
            return True
        return super().eventFilter(obj, event)


class ScreenshotHistoryWindow(QWidget):
    """截图历史浏览窗口（单例复用：close = hide，refresh 重建数据）。"""

    def __init__(self, store: ScreenshotStore | None) -> None:
        super().__init__()
        self._store = store
        self.setWindowTitle("截图历史")
        self.resize(1280, 720)

        self._strip = _ThumbStrip(store, self._show_record)
        self._text_view = QPlainTextEdit()
        self._text_view.setReadOnly(True)
        self._text_view.setPlaceholderText("选中截图的译文显示在这里")
        self._image_label = QLabel("选择左侧缩略图查看原图\n（拖拽平移；单击弹窗看原图）")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._image_scroll = _PanImageScroll(on_click=self._open_viewer)
        self._image_scroll.setWidget(self._image_label)
        self._image_scroll.setWidgetResizable(True)
        self._viewer: ImageViewerWindow | None = None  # 原图全尺寸弹窗（单例复用）

        self._first_label = QLabel("最早记录：—")
        self._last_label = QLabel("最新记录：—")
        self._year_combo = QComboBox()
        self._month_combo = QComboBox()
        self._day_combo = QComboBox()
        for combo in (self._year_combo, self._month_combo, self._day_combo):
            combo.currentIndexChanged.connect(self._on_date_changed)
        date_row = QHBoxLayout()
        date_row.addWidget(self._year_combo)
        date_row.addWidget(self._month_combo)
        date_row.addWidget(self._day_combo)
        date_row.addStretch(1)

        date_box = QVBoxLayout()
        date_box.addWidget(self._first_label)
        date_box.addLayout(date_row)
        date_box.addWidget(self._last_label)

        left = QVBoxLayout()
        left.addLayout(date_box)
        left.addWidget(self._strip, stretch=1)
        left.addWidget(self._text_view, stretch=1)

        layout = QHBoxLayout(self)
        left_widget = QWidget()
        left_widget.setLayout(left)
        layout.addWidget(left_widget, stretch=3)
        layout.addWidget(self._image_scroll, stretch=2)

        self._entries: list[tuple[int, int]] = []
        self._updating_date = False  # 程序性填充下拉时抑制 _on_date_changed

    # ---- 对外接口 -----------------------------------------------------------

    def refresh(self) -> None:
        """从数据库重建记录列表、日期范围与下拉选项（每次打开前调用）。"""
        self._entries = self._store.entries() if self._store is not None else []
        self._strip.set_entries(self._entries)
        first_last = self._store.first_last_ts() if self._store is not None else None
        has_data = bool(self._entries)
        for combo in (self._year_combo, self._month_combo, self._day_combo):
            combo.setEnabled(has_data)
        if not has_data:
            self._first_label.setText("最早记录：暂无截图记录")
            self._last_label.setText("最新记录：—")
            self._text_view.setPlainText("")
            self._image_label.setText("暂无截图记录")
            logger.info("截图历史为空（数据库 %s）", self._store.path if self._store else "<无>")
            return
        self._first_label.setText("最早记录：" + datetime.fromtimestamp(first_last[0] / 1000).strftime(TIME_FORMAT))
        self._last_label.setText("最新记录：" + datetime.fromtimestamp(first_last[1] / 1000).strftime(TIME_FORMAT))
        self._rebuild_year_combo(int(datetime.fromtimestamp(first_last[0] / 1000).year), int(datetime.fromtimestamp(first_last[1] / 1000).year))
        self._rebuild_day_combo()
        # 默认定位到最新一条并居中（自动选中逻辑生效）
        self._strip.center_on(len(self._entries) - 1)

    # ---- 日期联动 -----------------------------------------------------------

    def _rebuild_year_combo(self, first_year: int, last_year: int) -> None:
        self._updating_date = True
        try:
            self._year_combo.blockSignals(True)
            self._year_combo.clear()
            for year in range(first_year, last_year + 1):
                self._year_combo.addItem(str(year), year)
            self._year_combo.setCurrentIndex(self._year_combo.count() - 1)  # 默认最新年
            self._month_combo.blockSignals(True)
            self._month_combo.clear()
            for month in range(1, 13):  # 月固定 1-12（需求）
                self._month_combo.addItem(f"{month:02d}", month)
            self._month_combo.setCurrentIndex(11)
            self._month_combo.blockSignals(False)
        finally:
            self._year_combo.blockSignals(False)
            self._updating_date = False

    def _rebuild_day_combo(self) -> None:
        """按当前年月重建日期下拉（calendar.monthrange 自动处理平年闰年）。"""
        year = self._year_combo.currentData()
        month = self._month_combo.currentData()
        if year is None or month is None:
            return
        days = calendar.monthrange(int(year), int(month))[1]
        self._day_combo.blockSignals(True)
        current = self._day_combo.currentData()
        self._day_combo.clear()
        for day in range(1, days + 1):
            self._day_combo.addItem(f"{day:02d}", day)
        if current is not None and int(current) <= days:
            self._day_combo.setCurrentIndex(int(current) - 1)
        else:
            self._day_combo.setCurrentIndex(days - 1)
        self._day_combo.blockSignals(False)

    def _on_date_changed(self, _index: int) -> None:
        if self._updating_date:
            return
        self._rebuild_day_combo()
        self._go_to_date()

    def _go_to_date(self) -> None:
        """跳转到所选日期后的第一张；无则跳日期前最后一张，居中并延迟自动选中。"""
        year = self._year_combo.currentData()
        month = self._month_combo.currentData()
        day = self._day_combo.currentData()
        if year is None or month is None or day is None or not self._entries:
            return
        try:
            target_ms = int(datetime(int(year), int(month), int(day)).timestamp() * 1000)
        except ValueError:  # pragma: no cover - 下拉数据受控，不该发生
            return
        timestamps = [ts for _id, ts in self._entries]
        index = bisect_left(timestamps, target_ms)
        if index >= len(self._entries):
            index = len(self._entries) - 1  # 日期晚于全部记录 → 最后一张
        self._strip.center_on(index)
        logger.debug("日期跳转：%s → 记录 id=%s", (year, month, day), self._entries[index][0])

    # ---- 记录展示 -----------------------------------------------------------

    def _show_record(self, payload: tuple[str, bytes] | None) -> None:
        """缩略图选中回调：更新译文框与原图区。"""
        if not payload:
            self._text_view.setPlainText("（读取记录失败）")
            return
        ocr_text, original_bytes = payload
        self._text_view.setPlainText(ocr_text)
        pixmap = QPixmap()
        if pixmap.loadFromData(original_bytes):
            self._image_label.setPixmap(pixmap)
            self._image_label.setText("")
        else:
            self._image_label.setText("原图解码失败")

    def _open_viewer(self) -> None:
        """单击原图区：用当前原图弹出全尺寸查看窗口（拖拽移动，单击任意处关闭）。"""
        pixmap = self._image_label.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        if self._viewer is None:
            self._viewer = ImageViewerWindow()
        self._viewer.show_pixmap(pixmap)
        logger.debug("已打开原图查看弹窗（%dx%d）", pixmap.width(), pixmap.height())

    # ---- 生命周期 -----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()  # 单例复用：关闭 = 隐藏，宿主退出时统一销毁
        if self._viewer is not None:
            self._viewer.hide()  # 原图弹窗随历史窗口一同隐藏
