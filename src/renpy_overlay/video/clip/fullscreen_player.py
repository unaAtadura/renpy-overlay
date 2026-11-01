"""全屏播放窗口与悬浮控制面板。

FullscreenPlayer（attach 保底路径）：单引擎双视图 + 底部悬浮控制条
+ 无鼠标活动 3 秒隐藏。决策 2（已定案）：全屏复用同一
:class:`~renpy_overlay.video.clip.player.engine.VideoPlayer`（渲染线程 rebind 本
窗口画布句柄），「视频播放与全屏播放互不干扰」落实为快捷键按焦点
窗口路由——本窗口持有焦点时 space/left/right 仅作用于全屏视图，
不会误操作嵌入面板。

FullscreenOverlay（SDL 独立全屏路径）：无边框置顶工具窗口承载悬浮
控制面板（按钮/时长/进度/退出），不抢焦点；鼠标活动双通道——面板
上 Qt 直驱，面板外经 SDL 事件桥接（renderer 节流转发）。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .player.engine import PlaybackState
from .player.sync_clock import DEFAULT_FPS
from .player_pane import SdlCanvas, format_duration, is_playback_toggle_key

logger = logging.getLogger("renpy_overlay.video.clip.fullscreen_player")

#: 悬浮控制条隐藏判定：鼠标无活动毫秒数
HIDE_IDLE_MS = 3_000
CONTROL_BAR_HEIGHT = 56


class FullscreenPlayer(QWidget):
    """全屏播放窗口（独立顶层窗口；渲染线程生命周期由 editor 统一切换）。"""

    exit_requested = pyqtSignal()
    seek_requested = pyqtSignal(int)  # 步进请求（毫秒；正进负退）

    def __init__(self, player, *, parent=None) -> None:
        # 不加 FramelessWindowHint：Windows 下 Frameless+FullScreen 组合
        # 存在窗口不置前/不显示的已知问题（实测两次修复后仍不可见）；
        # WindowFullScreen 状态本身即无边框
        super().__init__(None, Qt.WindowType.Window)
        self._player = player
        self._fps = DEFAULT_FPS
        self._dur_ms = 0
        self._scrubbing = False

        self.setWindowTitle("全屏播放")
        self.setWindowState(self.windowState() | Qt.WindowState.WindowFullScreen)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)  # 无按键移动也收 mouseMoveEvent

        self._canvas = SdlCanvas(self)
        self._canvas.setMouseTracking(True)

        # ---- 悬浮控制条 -------------------------------------------------------
        self._bar = QFrame(self)
        self._bar.setObjectName("fullscreenBar")
        self._bar.setStyleSheet(
            "#fullscreenBar { background: rgba(20,20,20,180); border-radius: 6px; }"
        )
        bar_layout = QHBoxLayout(self._bar)
        bar_layout.setContentsMargins(12, 6, 12, 6)
        self._play_button: QAbstractButton = QPushButton("暂停")
        self._play_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play_button.clicked.connect(self._toggle_play)
        self._time_label = QLabel("00:00 / 00:00")
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.sliderReleased.connect(self._commit_seek)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setFixedWidth(90)
        self._volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._volume_slider.setValue(int(round(player.volume() * 100)))
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        exit_button = QPushButton("退出全屏")
        exit_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        exit_button.clicked.connect(self.exit_requested.emit)
        bar_layout.addWidget(self._play_button)
        bar_layout.addWidget(self._time_label)
        bar_layout.addWidget(self._slider, stretch=1)
        bar_layout.addWidget(QLabel("音量"))
        bar_layout.addWidget(self._volume_slider)
        bar_layout.addWidget(exit_button)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._canvas)

        # ---- 计时器：悬浮条显隐 + 位置刷新 ---------------------------------------
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(HIDE_IDLE_MS)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._hide_bar)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(200)
        self._position_timer.timeout.connect(self._refresh_position_ui)

    # ---- 渲染挂载 ------------------------------------------------------------

    def canvas_hwnd(self) -> int:
        return self._canvas.native_hwnd()

    def sync_from_player(self) -> None:
        """从引擎同步按钮/音量/时长（进入全屏时状态继承显示）。"""
        playing = self._player.playbackState() == PlaybackState.Playing
        self._play_button.setText("暂停" if playing else "播放")
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(int(round(self._player.volume() * 100)))
        self._volume_slider.blockSignals(False)
        self._dur_ms = max(0, self._player.duration())
        self._slider.setRange(0, self._dur_ms)
        self._refresh_position_ui()

    # ---- 悬浮条显隐 ------------------------------------------------------------

    def show_bar(self) -> None:
        """显示悬浮条（全屏进入时显示一次；此后鼠标活动触发）。"""
        if not self._bar.isVisible():
            logger.info("悬浮条显示（进入全屏/鼠标活动）")
        self._place_bar()
        self._bar.show()
        self._bar.raise_()
        self._idle_timer.start()

    def _hide_bar(self) -> None:
        if self._bar.isVisible():
            logger.info("悬浮条隐藏（3s 无鼠标活动）")
            self._bar.hide()

    def _place_bar(self) -> None:
        width = max(420, min(640, self.width() // 2))
        self._bar.setGeometry(
            (self.width() - width) // 2,
            self.height() - CONTROL_BAR_HEIGHT - 24,
            width,
            CONTROL_BAR_HEIGHT,
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._bar.isVisible():
            self.show_bar()
        else:
            self._idle_timer.start()  # 活动重置 3s 计时
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 规格：点击也算鼠标活动（移动/点击/滚轮统一重现面板）
        self.show_bar()
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        self.show_bar()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        # _bar 在构造尾部才创建：全屏窗口构造期 setWindowState 触发的
        # resizeEvent 早于 _bar 初始化，需容错（实测崩溃）
        bar = getattr(self, "_bar", None)
        if bar is not None and bar.isVisible():
            self._place_bar()
        super().resizeEvent(event)

    # ---- 窗口层诊断（attach 路径专用：实锤"show 了但不可见"还是"从未 show"） ------

    def showEvent(self, event) -> None:  # noqa: N802
        logger.info(
            "全屏窗口 showEvent：visible=%s active=%s geometry=%s",
            self.isVisible(), self.isActiveWindow(), self.geometry(),
        )
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        logger.info("全屏窗口 hideEvent：visible=%s", self.isVisible())
        super().hideEvent(event)

    # ---- 播放控制（快捷键路由：焦点在本窗口时生效） --------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        auto = event.isAutoRepeat()
        if is_playback_toggle_key(key, auto):
            self._toggle_play()
        elif key == Qt.Key.Key_Escape and not auto:
            self.exit_requested.emit()
        elif key == Qt.Key.Key_Left and not auto:
            self.seek_requested.emit(-1_000)
        elif key == Qt.Key.Key_Right and not auto:
            self.seek_requested.emit(+1_000)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _toggle_play(self) -> None:
        if self._player.playbackState() == PlaybackState.Playing:
            self._player.pause()
        else:
            self._player.play()
        self.sync_from_player()

    # ---- 进度条 ----------------------------------------------------------------

    def _on_slider_moved(self, value: int) -> None:
        self._player.setPosition(max(0, value))
        self._time_label.setText(
            f"{format_duration(value)} / {format_duration(self._dur_ms)}"
        )

    def _commit_seek(self) -> None:
        self._player.setPosition(max(0, self._slider.value()))

    def _on_volume_changed(self, value: int) -> None:
        self._player.setVolume(value / 100.0)

    def _refresh_position_ui(self) -> None:
        """引擎位置 → 悬浮条（拖拽中不回写防回环）。"""
        position = self._player.position()
        self._time_label.setText(
            f"{format_duration(position)} / {format_duration(self._dur_ms)}"
        )
        if not self._scrubbing:
            self._slider.blockSignals(True)
            self._slider.setValue(position)
            self._slider.blockSignals(False)

    def start_ui_timer(self) -> None:
        self._position_timer.start()
        self.show_bar()

    def stop_ui_timer(self) -> None:
        self._position_timer.stop()
        self._idle_timer.stop()
        if not self._bar.isHidden():
            self._bar.hide()


class FullscreenOverlay(QWidget):
    """SDL 全屏路径的悬浮控制面板（无边框置顶工具窗口；不抢焦点）。

    规格（raw_plans 全屏章节）：播放/暂停、已播时长、总时长、进度条、
    退出全屏；进入全屏显示一次，3 秒无鼠标活动隐藏，鼠标活动（移动/
    点击/滚轮）重现并重新计时。

    承载决策：独立 Qt 无边框置顶工具窗口而非 SDL 渲染线程自绘——本机
    Qt 的 showFullScreen 顶层窗口曾出现"创建但从不映射到屏幕"，而普通
    窗口显示一直正常（主窗口/日志面板）；Tool + Frameless + StaysOnTop
    不进任务栏，WA_ShowWithoutActivating + 控件全 NoFocus 不抢 SDL 全
    屏窗口焦点，topmost 层内后 raise 者在上稳定浮于全屏窗口之上；
    退出全屏由 editor 调 shutdown 销毁，无残留窗口。键盘不经本窗口
    （SDL 桥接链路）；播放状态经 200ms 轮询同步，不新增第二套状态。
    """

    exit_requested = pyqtSignal()

    def __init__(self, player, *, parent=None) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._player = player
        self._fps = DEFAULT_FPS
        self._dur_ms = 0
        self._scrubbing = False

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)  # 无按键移动也收 mouseMoveEvent
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # ---- 控制条（与 FullscreenPlayer 同构：按钮/时长/进度/退出） ------------
        self._bar = QFrame(self)
        self._bar.setObjectName("fullscreenBar")
        self._bar.setStyleSheet(
            "#fullscreenBar { background: rgba(20,20,20,180); border-radius: 6px; }"
        )
        bar_layout = QHBoxLayout(self._bar)
        bar_layout.setContentsMargins(12, 6, 12, 6)
        self._play_button: QAbstractButton = QPushButton("暂停")
        self._play_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._play_button.clicked.connect(self._toggle_play)
        self._time_label = QLabel("00:00 / 00:00")
        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slider.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self._slider.sliderMoved.connect(self._on_slider_moved)
        self._slider.sliderReleased.connect(self._commit_seek)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setFixedWidth(90)
        self._volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._volume_slider.setValue(int(round(player.volume() * 100)))
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        exit_button = QPushButton("退出全屏")
        exit_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        exit_button.clicked.connect(self._request_exit)
        bar_layout.addWidget(self._play_button)
        bar_layout.addWidget(self._time_label)
        bar_layout.addWidget(self._slider, stretch=1)
        bar_layout.addWidget(QLabel("音量"))
        bar_layout.addWidget(self._volume_slider)
        bar_layout.addWidget(exit_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._bar)
        self.resize(480, CONTROL_BAR_HEIGHT)

        # ---- 计时器：3s 无活动隐藏 + 位置/状态刷新 ---------------------------
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(HIDE_IDLE_MS)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._hide_bar)
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(200)
        self._position_timer.timeout.connect(self._refresh_position_ui)

    # ---- 生命周期 -----------------------------------------------------------

    def launch_at(self, geo: tuple[int, int, int, int]) -> None:
        """定位到主屏底部居中并显示一次（进入全屏时调用）。

        SDL 回传的是物理像素几何（DPI 缩放下与 Qt 逻辑坐标不一致，
        直接用于 move() 会定位到屏幕外——实测 3120x2080 物理 vs 逻辑
        约 2080x1387@150%）。SDL 全屏固定落主显示器
        （WINDOWPOS_CENTERED_DISPLAY(0)），故用 Qt primaryScreen 的
        逻辑几何定位，两侧行内对齐；geo 仅留作日志对照。
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            logger.error("悬浮面板定位失败：无可用屏幕")
            return
        sg = screen.geometry()  # 逻辑像素
        width = max(420, min(640, sg.width() // 2))
        self.resize(width, CONTROL_BAR_HEIGHT)
        self.move(
            sg.x() + (sg.width() - width) // 2,
            sg.y() + sg.height() - CONTROL_BAR_HEIGHT - 36,
        )
        self.sync_from_player()
        self.show()
        self.raise_()
        self.start_ui_timer()
        logger.info(
            "悬浮面板已显示（SDL 全屏）：screen_logic=%s sdl_phys=%s "
            "pos=(%d,%d) size=%dx%d dpr=%.2f",
            sg, geo, self.x(), self.y(), width, CONTROL_BAR_HEIGHT,
            screen.devicePixelRatio(),
        )

    def shutdown(self) -> None:
        """退出全屏销毁（editor 调用；先停计时器防残留回调）。"""
        self.stop_ui_timer()
        self.close()
        logger.info("悬浮面板已销毁（退出 SDL 全屏）")

    # ---- 显隐（显示一次 / 3s 隐藏 / 活动重现） --------------------------------

    def notify_activity(self) -> None:
        """鼠标活动入口（SDL 桥接与本窗口 Qt 事件共用）：隐藏则重现并重计时。"""
        if not self.isVisible():
            self.show()
            self.raise_()
            logger.info("悬浮面板重现（鼠标活动）")
        self._idle_timer.start()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self.notify_activity()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.notify_activity()
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        self.notify_activity()
        event.accept()

    def _hide_bar(self) -> None:
        if self.isVisible():
            logger.info("悬浮面板隐藏（3s 无鼠标活动）")
            self.hide()

    def start_ui_timer(self) -> None:
        self._position_timer.start()
        self._idle_timer.start()

    def stop_ui_timer(self) -> None:
        self._position_timer.stop()
        self._idle_timer.stop()

    # ---- 播放控制（与 FullscreenPlayer 同一通路；seek/退出均既有链路） -------------

    def sync_from_player(self) -> None:
        self._dur_ms = max(0, self._player.duration())
        self._slider.setRange(0, self._dur_ms)
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(int(round(self._player.volume() * 100)))
        self._volume_slider.blockSignals(False)
        self._refresh_position_ui()

    def _toggle_play(self) -> None:
        if self._player.playbackState() == PlaybackState.Playing:
            self._player.pause()
        else:
            self._player.play()
        self.sync_from_player()

    def _request_exit(self) -> None:
        logger.info("退出全屏：悬浮面板按钮")
        self.exit_requested.emit()

    def _on_slider_moved(self, value: int) -> None:
        self._player.setPosition(max(0, value))
        self._time_label.setText(
            f"{format_duration(value)} / {format_duration(self._dur_ms)}"
        )

    def _commit_seek(self) -> None:
        self._player.setPosition(max(0, self._slider.value()))

    def _on_volume_changed(self, value: int) -> None:
        self._player.setVolume(value / 100.0)

    def _refresh_position_ui(self) -> None:
        """引擎位置/状态 → 面板（拖拽中不回写防回环；按钮文案随状态实时同步）。"""
        position = self._player.position()
        self._time_label.setText(
            f"{format_duration(position)} / {format_duration(self._dur_ms)}"
        )
        if not self._scrubbing:
            self._slider.blockSignals(True)
            self._slider.setValue(position)
            self._slider.blockSignals(False)
        playing = self._player.playbackState() == PlaybackState.Playing
        self._play_button.setText("暂停" if playing else "播放")
