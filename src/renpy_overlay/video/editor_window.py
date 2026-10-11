"""视频编辑播放器窗口：帧级定位 + ASR 片段选取（需求布局自上而下）：

    文本框（视频路径，回车打开）+ 选择视频按钮
    视频播放窗口（暂停时左键单击放大该帧，见 :mod:`.frame_viewer`）
    进度条（拖拽 + 单击跳转；打开新视频默认暂停）
    音量条（拖拽 + 滚轮线性改变；打开新视频默认最小静音）
    步退5秒 / 上一帧 / 暂停播放 / 下一帧 / 步进5秒（按钮组）
    定位起点（label + 帧号文本框 + 捕获 + 跳转）
    定位终点（同上）
    语音识别按钮（三态提示显示在窗口内状态标签）

交互规则（需求）：路径回车打开（文件存在才打开）；暂停时播放区左键单击
放大当前帧；起止点文本框记录帧位置、回车跳帧（越界不响应）、捕获取当前
播放位置、终点允许早于起点（片段整理在 ASR 链路完成）；帧率取媒体元数据
（读不到回退 30fps），帧号 ↔ 毫秒按帧率换算。

离线测试约定：几何与播放器交互不构造真实 QWidget，本模块的换算/解析/
取值纯函数直接单测；窗口类依赖 QMediaPlayer 等 Qt Multimedia，仅人工联测。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QImage
from PyQt6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer, QVideoSink
from PyQt6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("renpy_overlay.video.editor_window")

#: 帧率回退值：媒体元数据未提供 VideoFrameRate 时的帧步进取值
DEFAULT_FPS = 30.0
#: 步退/步进的跨度（毫秒）
STEP_MS = 5000
#: 进度条单击跳转/拖拽的取值上限（毫秒；durationChanged 时更新）
#: 音量滚轮的单次步进（0-100 刻度）
VOLUME_WHEEL_STEP = 5

WINDOW_WIDTH = 960
WINDOW_HEIGHT = 640

#: 状态标签提示（需求给定措辞）
ASR_START_TEXT = "开始语音识别，请稍后..."
ASR_SUCCESS_TEXT = "语音识别成功"
ASR_FAIL_TEXT = "语音识别失败"

PLAY_BUTTON_TEXT_PLAYING = "暂停"
PLAY_BUTTON_TEXT_PAUSED = "播放"


# ---------------------------------------------------------------- 纯函数（离线单测）


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


def parse_frame_text(text: str, max_frame: int) -> int | None:
    """起止点文本框解析：非负整数且未超出视频范围返回帧号，否则 None（不响应）。"""
    text = (text or "").strip()
    if not text.isdigit():
        return None
    frame = int(text)
    if frame > max_frame:
        return None
    return frame


def max_frame_for(duration_ms: int, fps: float) -> int:
    """当前视频的最大帧号（duration 未知/为 0 时视为无有效范围）。"""
    if duration_ms <= 0:
        return -1  # 任何帧都越界：未打开视频时不响应
    return ms_to_frame(duration_ms, fps)


def fps_from_metadata(numeric_value) -> float:
    """媒体元数据帧率 → fps（纯函数，离线测试用）：非法/缺失回退 30fps。"""
    try:
        value = float(numeric_value)
    except (TypeError, ValueError):
        return DEFAULT_FPS
    return value if value > 0 else DEFAULT_FPS


def slider_value_from_click(pos_x: float, width: float, maximum: int) -> int:
    """进度条单击位置 → 目标值（毫秒）：按宽度比例换算并钳到 [0, maximum]。"""
    if width <= 0 or maximum <= 0:
        return 0
    ratio = min(1.0, max(0.0, pos_x / width))
    return int(round(ratio * maximum))


def volume_from_wheel(delta_y: int, current: int, step: int = VOLUME_WHEEL_STEP) -> int:
    """音量条滚轮步进（纯函数）：上滚增大、下滚减小，钳到 [0, 100]。"""
    direction = 1 if delta_y > 0 else -1
    return max(0, min(100, current + direction * step))


# ---------------------------------------------------------------- 窗口


class _ClickableSlider(QSlider):
    """进度条：单击任意位置直接跳转（QSlider 默认只按页步进）。"""

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            value = slider_value_from_click(
                event.position().x(), self.width(), self.maximum()
            )
            self.setValue(value)
            self.sliderMoved.emit(value)  # 与拖拽同一通路驱动播放位置
            event.accept()
            return
        super().mousePressEvent(event)


class _VolumeSlider(QSlider):
    """音量条：滚轮线性步进（需求只允许拖拽与滚轮），不响应键盘。"""

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


class _VideoCanvas(QWidget):
    """视频播放区：QVideoSink 帧绘制 + 暂停态单击放大当前帧。"""

    def __init__(self, on_frame_click, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_frame_click = on_frame_click
        self._frame: QImage | None = None
        self.setMinimumHeight(320)
        self._press_pos = None

    def current_frame(self) -> QImage | None:
        return self._frame

    def set_frame(self, frame: QImage) -> None:
        """缓存最近一帧并重绘（videoFrameChanged 信号驱动）。"""
        if frame.isNull():
            return
        self._frame = frame
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        from PyQt6.QtGui import QPainter

        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        if self._frame is not None:
            scaled = self._frame.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._press_pos is not None
            and (event.position().toPoint() - self._press_pos).manhattanLength() <= 4
        ):
            self._on_frame_click()
        self._press_pos = None
        event.accept()


class VideoEditorWindow(QWidget):
    """视频编辑播放器窗口（单例复用：宿主持有，show 时重置交互状态）。"""

    def __init__(
        self,
        *,
        video_dir: Path | None = None,
        pick_video=None,
        frame_viewer_factory=None,
        on_asr_request=None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("视频编辑")
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        #: ASR 请求回调（宿主注入）：``(start_s, end_s)``，片段整理在链路内完成
        self._on_asr_request = on_asr_request
        #: 视频保存目录（选择对话框默认目录；None 用系统默认位置）
        self._video_dir = video_dir
        #: IFileOpenDialog 注入点（离线测试 stub；默认 folder_dialog.pick_file）
        if pick_video is None:
            from .. import folder_dialog  # 局部导入：仅 Windows 存在

            def pick_video():
                default_dir = str(self._video_dir) if self._video_dir else None
                return folder_dialog.pick_file(int(self.winId()), default_dir)

        self._pick_video = pick_video
        #: 帧放大窗工厂（离线测试 stub；默认 FrameViewerWindow，lower_than 由
        #: set_locked_hwnd_provider 注入宿主的截图窗口池）
        self._frame_viewer_factory = frame_viewer_factory
        self._locked_hwnd_provider = None
        #: 帧放大窗引用。PyQt 顶层窗口（无父）所有权归 Python：不持有引用，
        #: 局部变量出栈后窗口会被垃圾回收立即销毁（表现为"日志显示已显示但
        #: 界面看不到"，实测）。
        self._frame_viewer = None

        # ---- 播放器（Qt Multimedia）：默认暂停 + 音量 0（静音，需求） ----------
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(0.0)
        self._player.setAudioOutput(self._audio_output)
        self._fps = DEFAULT_FPS

        # ---- 布局（需求自上而下） ----------------------------------------------
        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("输入视频路径后回车打开")
        self._path_edit.returnPressed.connect(self._open_from_path_edit)
        browse_button = QPushButton("选择视频")
        browse_button.clicked.connect(self._open_via_dialog)
        path_row.addWidget(self._path_edit, stretch=1)
        path_row.addWidget(browse_button)
        layout.addLayout(path_row)

        self._canvas = _VideoCanvas(self._open_frame_viewer)
        layout.addWidget(self._canvas, stretch=1)
        # QVideoSink 取帧自绘（暂停帧放大需要 QImage；播放区即画布）
        self._video_sink = QVideoSink(self)
        self._player.setVideoSink(self._video_sink)
        self._video_sink.videoFrameChanged.connect(self._on_video_frame_changed)

        self._position_slider = _ClickableSlider(Qt.Orientation.Horizontal)
        self._position_slider.setRange(0, 0)
        self._scrubbing = False  # 拖拽中不回写播放位置（防回环抖动）
        self._position_slider.sliderPressed.connect(self._on_slider_pressed)
        self._position_slider.sliderReleased.connect(self._on_slider_released)
        self._position_slider.sliderMoved.connect(self._on_slider_moved)
        layout.addWidget(self._position_slider)

        self._volume_slider = _VolumeSlider()
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        layout.addWidget(self._volume_slider)

        controls_row = QHBoxLayout()
        for text, handler in (
            ("步退5秒", lambda: self._seek_by(-STEP_MS)),
            ("上一帧", lambda: self._seek_by(-frame_duration_ms(self._fps))),
            (PLAY_BUTTON_TEXT_PAUSED, self._toggle_play),
            ("下一帧", lambda: self._seek_by(frame_duration_ms(self._fps))),
            ("步进5秒", lambda: self._seek_by(STEP_MS)),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            if text in (PLAY_BUTTON_TEXT_PAUSED, PLAY_BUTTON_TEXT_PLAYING):
                self._play_button: QAbstractButton = button
            controls_row.addWidget(button)
        layout.addLayout(controls_row)

        self._start_edit = self._add_marker_row(layout, "定位起点")
        self._end_edit = self._add_marker_row(layout, "定位终点")

        self._asr_button = QPushButton("语音识别")
        self._asr_button.clicked.connect(self._request_asr)
        layout.addWidget(self._asr_button)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        # ---- 播放器信号 ---------------------------------------------------------
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.metaDataChanged.connect(self._on_meta_changed)

        logger.debug("视频编辑窗口已创建")

    # ---- 打开视频 ---------------------------------------------------------------

    def _open_from_path_edit(self) -> None:
        self.open_path(self._path_edit.text())

    def _open_via_dialog(self) -> None:
        try:
            path = self._pick_video()
        except Exception as exc:  # pragma: no cover - COM 环境异常已内部收敛
            logger.warning("选择视频对话框异常：%s", exc)
            return
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        """打开视频（需求：文件存在才打开）；打开后暂停、进度归零、音量静音。"""
        target = Path(path.strip().strip('"'))
        if not target.is_file():
            logger.info("视频文件不存在，忽略打开请求：%s", target)
            self._status_label.setText("文件不存在")
            return
        self._path_edit.setText(str(target))
        self._player.setSource(QUrl.fromLocalFile(str(target)))
        self._player.pause()
        self._volume_slider.setValue(0)  # 最小静音（需求）
        logger.info("已打开视频（暂停）：%s", target)

    # ---- 播放控制 ---------------------------------------------------------------

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _seek_by(self, delta_ms: int) -> None:
        self._player.setPosition(
            max(0, self._player.position() + delta_ms) if self._player.source().isValid() else 0
        )

    def _on_playback_state_changed(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_button.setText(PLAY_BUTTON_TEXT_PLAYING if playing else PLAY_BUTTON_TEXT_PAUSED)

    def _on_meta_changed(self) -> None:
        """帧率取媒体元数据（需求：帧位置精确到帧）；读不到回退 30fps。

        PyQt6 的 metaDataChanged 信号无参数：槽必须无参，元数据经
        :meth:`QMediaPlayer.metaData` 主动读取（带参槽会 TypeError
        使进程崩溃，实测见 logs/renpy_overlay_20261011_162520.log）。
        """
        metadata = self._player.metaData()
        value = None
        if metadata is not None:
            try:
                value = metadata.numericValue(QMediaMetaData.Key.VideoFrameRate)
            except Exception:  # pragma: no cover - 元数据缺失/后端差异
                value = None
        self._fps = fps_from_metadata(value)
        logger.debug("视频帧率：%s fps", self._fps)

    # ---- 进度条 -----------------------------------------------------------------

    def _on_slider_pressed(self) -> None:
        self._scrubbing = True

    def _on_slider_released(self) -> None:
        self._scrubbing = False

    def _on_slider_moved(self, value: int) -> None:
        self._player.setPosition(value)

    def _on_position_changed(self, position_ms: int) -> None:
        if not self._scrubbing:
            self._position_slider.blockSignals(True)
            self._position_slider.setValue(position_ms)
            self._position_slider.blockSignals(False)

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._position_slider.setRange(0, max(0, duration_ms))

    # ---- 音量条 -----------------------------------------------------------------

    def _on_volume_changed(self, value: int) -> None:
        self._audio_output.setVolume(value / 100.0)

    # ---- 起止点（捕获 / 跳转 / 文本框回车） ---------------------------------------

    def _add_marker_row(self, layout: QVBoxLayout, label_text: str) -> QLineEdit:
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        frame_edit = QLineEdit("0")
        frame_edit.returnPressed.connect(
            lambda: self._jump_to_frame_text(frame_edit)
        )
        capture_button = QPushButton("捕获")
        capture_button.clicked.connect(lambda: self._capture_frame(frame_edit))
        jump_button = QPushButton("跳转")
        jump_button.clicked.connect(lambda: self._jump_to_frame_text(frame_edit))
        row.addWidget(frame_edit, stretch=1)
        row.addWidget(capture_button)
        row.addWidget(jump_button)
        layout.addLayout(row)
        return frame_edit

    def _capture_frame(self, frame_edit: QLineEdit) -> None:
        """捕获当前播放位置写入文本框（需求：点击后记录当前播放位置）。"""
        frame_edit.setText(str(ms_to_frame(self._player.position(), self._fps)))

    def _jump_to_frame_text(self, frame_edit: QLineEdit) -> None:
        """跳到文本框帧号（越界/非法不响应，需求）。"""
        frame = parse_frame_text(frame_edit.text(), max_frame_for(self._player.duration(), self._fps))
        if frame is None:
            logger.info("帧号越界或非法，忽略跳转：%r", frame_edit.text())
            return
        self._player.setPosition(frame_to_ms(frame, self._fps))

    # ---- 帧放大（暂停态单击播放区） -------------------------------------------------

    def set_locked_hwnd_provider(self, provider) -> None:
        """注入截图窗口句柄提供者（帧放大窗压低层级的参照来源）。"""
        self._locked_hwnd_provider = provider

    def _open_frame_viewer(self) -> None:
        """暂停时单击播放区：放大当前帧（播放中单击忽略，需求）。"""
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return
        frame = self._canvas.current_frame()
        if frame is None or frame.isNull():
            return
        factory = self._frame_viewer_factory
        if factory is None:
            from .frame_viewer import FrameViewerWindow

            factory = FrameViewerWindow
        if self._frame_viewer is not None:
            self._frame_viewer.close()  # 关闭旧实例（closeEvent 已 deleteLater）
        viewer = factory(frame, lower_than=self._locked_hwnd_provider)
        # 必须持有引用：顶层窗口所有权归 Python，局部变量出栈后即被销毁
        self._frame_viewer = viewer
        viewer.show_frame()
        logger.info("已打开帧放大窗（帧号 %d）", ms_to_frame(self._player.position(), self._fps))

    def _on_video_frame_changed(self, frame) -> None:
        self._canvas.set_frame(frame.toImage())

    # ---- 语音识别 ----------------------------------------------------------------

    def current_video_path(self) -> str | None:
        """当前已打开的视频路径（供 ASR 音轨截取）；无效返回 None。"""
        text = self._path_edit.text().strip().strip('"')
        return text if text and Path(text).is_file() else None

    def set_status(self, text: str) -> None:
        """状态标签（ASR 三态提示；供宿主在识别完成回调中更新）。"""
        self._status_label.setText(text)

    def asr_range_seconds(self) -> tuple[float, float] | None:
        """读取起止点片段（秒）；终点早于起点自动交换，间隔为 0 返回 None。"""
        from .ffmpeg import normalize_range

        max_frame = max_frame_for(self._player.duration(), self._fps)
        start = parse_frame_text(self._start_edit.text(), max_frame)
        end = parse_frame_text(self._end_edit.text(), max_frame)
        if start is None or end is None:
            self._status_label.setText("起止点未就绪")
            return None
        rng = normalize_range(frame_to_ms(start, self._fps) / 1000.0, frame_to_ms(end, self._fps) / 1000.0)
        if rng is None:
            return None
        return rng

    def _request_asr(self) -> None:
        """语音识别按钮：整理片段并交宿主链路（互斥/异步/上屏在宿主侧）。"""
        rng = self.asr_range_seconds()
        if rng is None:
            return
        if self._on_asr_request is None:
            self._status_label.setText("语音识别不可用")
            return
        self._status_label.setText(ASR_START_TEXT)
        self._on_asr_request(*rng)
