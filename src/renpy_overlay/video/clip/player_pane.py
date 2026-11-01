"""播放器面板：SDL2 画面 + 播放控制（剪辑主窗口中部嵌入组件）。

自原独立播放器窗口（player_window.py）迁入改造：QWidget 可嵌入主窗口
三区布局；打开入口移至资源窗口（双击载入经 :meth:`open_path`）；新增
全屏按钮（渲染器统一由 :mod:`.ui.editor_window` 管理，面板仅上报画布
就绪事件）。

保留全部引擎交互机制（与参考实现同口径）：seek 合并节流（按钮 250ms /
拖拽 150ms 窗口）、连点步进累加基准、音量条与引擎同步（构造期显式
推送初值）、空格快捷键（NoFocus 策略）、引擎指标心跳。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..av_media import av_available
from .player.engine import PlaybackState, VideoPlayer
from .player.sync_clock import DEFAULT_FPS

logger = logging.getLogger("renpy_overlay.video.clip.player_pane")

#: 步退/步进的跨度（毫秒）：按钮文案「步退1秒/步进1秒」与此一致
STEP_MS = 1000
#: 拖拽 seek 节流窗口（毫秒）
SEEK_THROTTLE_MS = 150
#: 按钮 seek 的专用节流窗口（毫秒）；本值兼作兜底 QTimer 周期
BUTTON_SEEK_THROTTLE_MS = 250
#: 后端活性心跳周期（毫秒）
LIVENESS_HEARTBEAT_MS = 2000
#: 音量滚轮的单次步进（0-100 刻度）
VOLUME_WHEEL_STEP = 5

PLAY_BUTTON_TEXT_PLAYING = "暂停"
PLAY_BUTTON_TEXT_PAUSED = "播放"


# ---------------------------------------------------------------- 纯函数（离线单测口径自原 player_window 沿袭）


def frame_duration_ms(fps: float) -> int:
    """单帧时长（毫秒）：fps 非正或缺失时按 30fps 回退，下限 1ms。"""
    if fps is None or fps <= 0:
        fps = DEFAULT_FPS
    return max(1, round(1000.0 / fps))


def ms_to_frame(position_ms: int, fps: float) -> int:
    """播放位置（毫秒）→ 帧号。"""
    return int(round(position_ms / frame_duration_ms(fps)))


def frame_to_ms(frame: int, fps: float) -> int:
    """帧号 → 播放位置（毫秒，帧首）；负帧钳零。"""
    return max(0, frame * frame_duration_ms(fps))


def max_frame_for(duration_ms: int, fps: float) -> int:
    """当前视频的最大帧号（duration 未知/为 0 时视为无有效范围）。"""
    if duration_ms <= 0:
        return -1  # 任何帧都越界：未打开视频时不响应
    return ms_to_frame(duration_ms, fps)


def format_duration(ms: int) -> str:
    """毫秒 → 时长文本：不足 1 小时为 mm:ss，否则 h:mm:ss；负值钳零。"""
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def is_playback_toggle_key(key: int, auto_repeat: bool) -> bool:
    """空格键且非自动重复 → 播放/暂停快捷键判定（离线测试用）。"""
    return key == int(Qt.Key.Key_Space) and not auto_repeat


def should_seek_now(
    now_ms: float, last_seek_ms: float, throttle_ms: int = SEEK_THROTTLE_MS
) -> bool:
    """拖拽 seek 节流判定：不足节流窗口则挂起；未 seek 过（<0）首拍放行。"""
    return last_seek_ms < 0 or now_ms - last_seek_ms >= throttle_ms


def slider_value_from_click(pos_x: float, width: float, maximum: int) -> int:
    """进度条单击位置 → 目标值（毫秒）：按宽度比例换算并钳到 [0, maximum]。"""
    if width <= 0 or maximum <= 0:
        return 0
    ratio = min(1.0, max(0.0, pos_x / width))
    return int(round(ratio * maximum))


def volume_from_wheel(delta_y: int, current: int, step: int = VOLUME_WHEEL_STEP) -> int:
    """音量条滚轮步进：上滚增大、下滚减小，钳到 [0, 100]。"""
    direction = 1 if delta_y > 0 else -1
    return max(0, min(100, current + direction * step))


# ---------------------------------------------------------------- 控件（自原 player_window 迁入）


class _ClickableSlider(QSlider):
    """进度条：左键单击任意位置跳转；按住左键拖动连续调节。"""

    def __init__(self, orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._dragging = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            value = slider_value_from_click(
                event.position().x(), self.width(), self.maximum()
            )
            self.setValue(value)
            self.sliderMoved.emit(value)
            self._dragging = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            value = slider_value_from_click(
                event.position().x(), self.width(), self.maximum()
            )
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.sliderMoved.emit(self.value())
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _VolumeSlider(QSlider):
    """音量条：滚轮线性步进（拖拽由 QSlider 原生支持），不响应键盘。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, 100)
        self.setValue(0)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta != 0:
            self.setValue(volume_from_wheel(delta, self.value()))
        event.accept()


class SdlCanvas(QWidget):
    """SDL 渲染器宿主：原生窗口句柄交给 SDL_CreateWindowFrom（双重禁绘）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(320)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)

    def native_hwnd(self) -> int:
        return int(self.winId())

    def paintEvent(self, event) -> None:  # noqa: N802 - 画面完全由 SDL 绘制
        event.accept()


# ---------------------------------------------------------------- 面板


class PlayerPane(QWidget):
    """播放器面板：播放窗口/时长/进度/音量/步进按钮组/全屏按钮。"""

    fullscreen_requested = pyqtSignal()
    canvas_shown = pyqtSignal()  # 画布句柄就绪（editor 统一 attach 渲染器）

    def __init__(self, *, clock_ms=None, player_factory=None, parent=None) -> None:
        super().__init__(parent)
        self._video_path: str | None = None

        # ---- 播放引擎（SDL2 + PyAV） ----------------------------------------
        self._player = (
            player_factory(self) if player_factory is not None else VideoPlayer(self)
        )
        self._player.open_failed.connect(
            lambda text: self.set_status(f"打开失败：{text}")
        )
        self._fps = DEFAULT_FPS
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._last_seek_ms = -1.0
        self._pending_seek: int | None = None
        #: 最近一次 seek 请求目标：连点步进累加基准（position 异步更新防重复）
        self._last_target_ms: int | None = None
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.setInterval(BUTTON_SEEK_THROTTLE_MS)
        self._pending_timer.timeout.connect(self._on_pending_timer_timeout)
        self._liveness_timer = QTimer(self)
        self._liveness_timer.setInterval(LIVENESS_HEARTBEAT_MS)
        self._liveness_timer.timeout.connect(self._log_liveness)
        self._liveness_timer.start()

        # ---- 布局 ------------------------------------------------------------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._canvas = SdlCanvas()
        layout.addWidget(self._canvas, stretch=1)  # 客户区归 SDL present

        self._position_slider = _ClickableSlider(Qt.Orientation.Horizontal)
        self._position_slider.setRange(0, 0)
        self._position_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scrubbing = False
        self._position_slider.sliderPressed.connect(self._on_slider_pressed)
        self._position_slider.sliderReleased.connect(self._on_slider_released)
        self._position_slider.sliderMoved.connect(self._on_slider_moved)
        layout.addWidget(self._position_slider)

        controls_row = QHBoxLayout()
        self._time_label = QLabel("00:00 / 00:00")
        controls_row.addWidget(self._time_label)
        controls_row.addStretch(1)
        for text, handler in (
            ("步退1秒", lambda: self._seek_by(-STEP_MS)),
            ("上一帧", lambda: self._seek_by(-frame_duration_ms(self._fps))),
            (PLAY_BUTTON_TEXT_PAUSED, self._toggle_play),
            ("下一帧", lambda: self._seek_by(frame_duration_ms(self._fps))),
            ("步进1秒", lambda: self._seek_by(STEP_MS)),
            ("全屏", self.fullscreen_requested.emit),
        ):
            button = QPushButton(text)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(handler)
            if text in (PLAY_BUTTON_TEXT_PAUSED, PLAY_BUTTON_TEXT_PLAYING):
                self._play_button: QAbstractButton = button
            controls_row.addWidget(button)
        layout.addLayout(controls_row)

        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("音量"))
        self._volume_slider = _VolumeSlider()
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        volume_row.addWidget(self._volume_slider, stretch=1)
        self._status_label = QLabel("未打开视频")
        volume_row.addWidget(self._status_label)
        layout.addLayout(volume_row)

        # ---- 引擎信号 ----------------------------------------------------------
        self._player.position_changed.connect(self._on_position_changed)
        self._player.state_changed.connect(self._on_playback_state_changed)
        self._player.media_opened.connect(self._apply_media_info)
        # UI 初值显式同步到引擎（bug 修复：valueChanged 只在值变化时发射）
        self._player.setVolume(self._volume_slider.value() / 100.0)

    # ---- 打开视频 ---------------------------------------------------------------

    def open_path(self, path: str) -> None:
        """打开视频（文件存在才打开）；打开后暂停、进度归零、音量静音。"""
        target = Path(path.strip().strip('"'))
        if not target.is_file():
            logger.info("视频文件不存在，忽略打开请求：%s", target)
            self.set_status("文件不存在")
            return
        if not av_available():
            logger.warning("PyAV 不可用（import 失败），视频功能降级")
            self.set_status("PyAV 不可用，视频功能降级")
            return
        self._video_path = str(target)
        self._last_target_ms = None
        self._player.open(str(target))
        self._volume_slider.setValue(0)
        # 需求：完整地址过长会挤占音量条 → 状态栏只显示文件名
        # （完整路径已由编辑器日志记录，不再上屏）
        self.set_status(target.name)
        logger.info("已打开视频（暂停）：%s", target)

    def current_video_path(self) -> str | None:
        """当前已打开的视频路径；无效返回 None。"""
        if self._video_path and Path(self._video_path).is_file():
            return self._video_path
        return None

    def set_status(self, text: str) -> None:
        # 只显示文件名部分：完整路径过长会横向挤占音量条（需求去除地址显示）
        self._status_label.setText(Path(text).name if Path(text).suffix else text)

    def sync_volume_ui(self, volume: float) -> None:
        """引擎音量 → 音量条 UI（全屏面板改动后回同步；防信号回环）。"""
        self._volume_slider.blockSignals(True)
        self._volume_slider.setValue(int(round(max(0.0, min(1.0, volume)) * 100)))
        self._volume_slider.blockSignals(False)

    def player(self) -> VideoPlayer:
        """引擎门面（editor 的全屏切换/字幕/导出链路读取）。"""
        return self._player

    def canvas(self) -> SdlCanvas:
        return self._canvas

    # ---- 播放控制 ---------------------------------------------------------------

    def toggle_play(self) -> None:
        """播放/暂停切换（快捷键路由与按钮共用出口）。"""
        if self._player.playbackState() == PlaybackState.Playing:
            self._player.pause()
        else:
            self._player.play()

    def _toggle_play(self) -> None:
        self.toggle_play()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if is_playback_toggle_key(event.key(), event.isAutoRepeat()):
            self._toggle_play()
            event.accept()
            return
        super().keyPressEvent(event)

    def _seek_by(self, delta_ms: int) -> None:
        """步进（连点累加基准：挂起目标 → 最近请求目标 → 播放位置）。"""
        if self._pending_seek is not None:
            base = self._pending_seek
        elif self._last_target_ms is not None:
            base = self._last_target_ms
        else:
            base = self._player.position()
        logger.debug("步进请求 delta=%+dms（base=%d）", delta_ms, base)
        self._seek_to(base + delta_ms)

    def _seek_to(self, position_ms: int, source: str = "button") -> None:
        """统一 seek 合并入口（节流窗口内只挂起最新目标）。"""
        if not self._player.hasMedia():
            return
        self._pending_seek = max(0, position_ms)
        self._last_target_ms = self._pending_seek
        now = self._clock_ms()
        if should_seek_now(now, self._last_seek_ms, BUTTON_SEEK_THROTTLE_MS):
            self._commit_pending_seek(now)
        else:
            logger.debug("seek 挂起（source=%s target=%dms）", source, self._pending_seek)
        self._pending_timer.start()

    def _commit_pending_seek(self, now: float | None = None) -> None:
        """落定挂起 seek（按钮兜底 timer / 拖拽 sliderReleased 共用出口）。"""
        if self._pending_seek is None:
            return
        target, self._pending_seek = self._pending_seek, None
        self._pending_timer.stop()
        self._last_seek_ms = self._clock_ms() if now is None else now
        self._last_target_ms = target
        self._player.setPosition(target)
        logger.debug("seek 落定 → %dms", target)

    def _on_pending_timer_timeout(self) -> None:
        if self._scrubbing:
            return
        self._commit_pending_seek()

    def _on_playback_state_changed(self, state) -> None:
        playing = state == PlaybackState.Playing
        self._play_button.setText(PLAY_BUTTON_TEXT_PLAYING if playing else PLAY_BUTTON_TEXT_PAUSED)

    def _apply_media_info(self, info) -> None:
        """引擎元数据回报：帧率与时长（进度条范围）。"""
        self._fps = info.fps
        self._position_slider.setRange(0, max(0, info.duration_ms))
        self._update_time_label(self._player.position())
        logger.debug("视频元数据：fps=%s duration=%dms", info.fps, info.duration_ms)

    # ---- 进度条 -----------------------------------------------------------------

    def _on_slider_pressed(self) -> None:
        self._scrubbing = True

    def _on_slider_released(self) -> None:
        self._scrubbing = False
        self._commit_pending_seek()

    def _on_slider_moved(self, value: int) -> None:
        """拖拽进度：节流 seek（消除 seek 风暴导致的撕裂与卡死）。"""
        self._pending_seek = value
        now = self._clock_ms()
        if should_seek_now(now, self._last_seek_ms):
            self._last_seek_ms = now
            self._last_target_ms = None
            self._player.setPosition(value)
            logger.debug("拖拽 seek → %dms", value)

    def _on_position_changed(self, position_ms: int) -> None:
        self._update_time_label(position_ms)
        if self._last_target_ms is not None and abs(
            position_ms - self._last_target_ms
        ) <= frame_duration_ms(self._fps):
            self._last_target_ms = None
        if not self._scrubbing:
            self._position_slider.blockSignals(True)
            self._position_slider.setValue(position_ms)
            self._position_slider.blockSignals(False)

    def _update_time_label(self, position_ms: int) -> None:
        self._time_label.setText(
            f"{format_duration(position_ms)} / {format_duration(self._player.duration())}"
        )

    # ---- 音量条 -----------------------------------------------------------------

    def _on_volume_changed(self, value: int) -> None:
        self._player.setVolume(value / 100.0)

    # ---- 诊断 -------------------------------------------------------------------

    def _log_liveness(self) -> None:
        """引擎指标心跳（冻结/挂起/卡顿诊断数据链）。

        INFO 级：卡顿排查需要供给健康时序（水位/断供/时钟重跳），debug
        级默认不输出导致无数据可查（实测教训）。``heartbeat`` 标记
        （logging extra，不进格式化文本）供日志面板界面过滤：仅完整
        调试模式显示；文件日志内容与级别不变。
        """
        snap = self._player.stats_snapshot()
        logger.info(
            "引擎指标：presented=%d dropped=%d stale=%d 水位=%.1fms "
            "断供=%d 爆音弃置=%d 时钟重跳=%d次/%dms "
            "seek=%d次（最近 %.1fms）vqueue=%d",
            snap["presented"],
            snap["dropped"],
            snap["stale"],
            snap["audio_level_ms"],
            snap["underruns"],
            snap["audio_dropped"],
            snap["reset_count"],
            snap["reset_jump_ms"],
            snap["seek_count"],
            snap["seek_latency_ms"],
            snap["vqueue_depth"],
            extra={"heartbeat": True},
        )

    # ---- 渲染装配生命周期 -----------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._player.ensure_sdl()  # 渲染器 attach 前保证 SDL 已初始化
        self.canvas_shown.emit()  # 渲染器统一由 editor_window 管理

    def pause_engine(self) -> None:
        """全屏切换前暂停（engine 由 editor 统一管理渲染线程）。"""
        self._player.pause()

    def shutdown(self) -> None:
        """应用退出收尾：暂停 + 弃置渲染线程由 editor 完成；此处仅暂停。"""
        self._player.pause()
