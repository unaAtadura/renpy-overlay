"""视频编辑播放器窗口：帧级定位 + ASR 片段选取（需求布局自上而下）：

    文本框（视频路径，回车打开）+ 选择视频按钮
    视频播放窗口（暂停时左键单击放大该帧，见 :mod:`.frame_viewer`）
    进度条（拖拽 + 单击跳转；打开新视频默认暂停）
    音量条（拖拽 + 滚轮线性改变；打开新视频默认最小静音）
    步退1秒 / 上一帧 / 暂停播放 / 下一帧 / 步进1秒（按钮组）
    定位起点（label + 帧号文本框 + 捕获 + 跳转）
    定位终点（同上）
    语音识别按钮（三态提示显示在窗口内状态标签）

交互规则（需求）：路径回车打开（文件存在才打开）；暂停时播放区左键单击
放大当前帧；起止点文本框记录帧位置、回车跳帧（越界不响应）、捕获取当前
播放位置、终点允许早于起点（片段整理在 ASR 链路完成）；帧率取媒体元数据
（读不到回退 30fps），帧号 ↔ 毫秒按帧率换算；空格键切换播放/暂停（窗口
激活且焦点不在输入框时生效，其余控件 NoFocus 不夺焦点）。

播放引擎：由 :mod:`.player` 自管完成（移植自参考实验项目 videoplayer 的
已验证方案：PyAV worker 线程单遍 demux 分流解码 + 关键帧索引 sidecar
缓存直跳 + 双缓冲预解码 + SDL2 音视频输出 + 音频主时钟同步）——
QMediaPlayer 的 FFmpeg 后端在连续 seek 下产出抢占态坏帧（花屏）且
慢帧风暴独占主线程（卡死），av_player 单进程音频链路存在音画同步
缺陷，均已由本方案整体取代。

稳定性防护：全部 seek（按钮/文本框/拖拽）经合并节流——窗口内只挂起最新
目标，由兜底 QTimer（按钮/文本框 250ms 专用窗口）或 sliderReleased（拖拽
150ms 窗口）落定；连点步进基于「挂起/最近请求目标」累加。引擎侧 seek
为异步 latch（最新胜出、包间隙中断、双缓冲重预填充），花屏与卡死从
机制上消除。

引擎指标心跳：每 2s 输出 presented/水位/underrun/seek 耗时（DEBUG）。
冻结判别器——主线程冻结则心跳日志消失（真空窗口即冻结时长）；引擎
挂起则心跳仍在但 presented 持续为 0，两类情况可区分。

离线测试约定：几何与播放器交互不构造真实 QWidget，本模块的换算/解析/
取值纯函数直接单测；窗口类依赖 player 引擎（av 解码/SDL 音视频链路），
仅人工联测。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PyQt6.QtCore import QRegularExpression, Qt, QTimer
from PyQt6.QtGui import QRegularExpressionValidator
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

from .player.engine import PlaybackState, VideoPlayer
from .player.renderer_sdl2 import SdlVideoRenderer
from .player.sync_clock import DEFAULT_FPS

logger = logging.getLogger("renpy_overlay.video.editor_window")

#: 步退/步进的跨度（毫秒）：按钮文案「步退1秒/步进1秒」与此一致
STEP_MS = 1000
#: 拖拽 seek 节流窗口（毫秒）：sliderMoved 高频触发时距上次真实 seek 不足
#: 该窗口则挂起（FFmpeg 后端 seek 是重操作，逐事件 seek 的风暴会撕裂画面
#: 甚至拖垮主线程），最终位置由 sliderReleased 落定
SEEK_THROTTLE_MS = 150
#: 按钮/文本框 seek 的专用节流窗口（毫秒）：比拖拽 150ms 更宽——150ms 窗口
#: + 180ms 兜底实测仍以 ~200ms 间隔产生连续 seek 流，FFmpeg 后端暂停态长
#: GOP seek 持续抢占即花屏；放宽到 250ms 给解码留出完成窗口。本值兼作兜底
#: QTimer 周期：超时必已过窗、挂起目标必落定
BUTTON_SEEK_THROTTLE_MS = 250
#: 后端活性心跳周期（毫秒）：定时输出帧/位置事件计数。
#: 冻结判别器——主线程冻结时心跳日志消失（真空窗口即冻结时长）；后端
#: 挂起时主线程存活但帧/位置计数持续为 0，两类情况可区分
LIVENESS_HEARTBEAT_MS = 2000
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


#: 帧号文本框输入校验模式：纯数字（空格/其他字符在输入层直接拒绝，
# 不进入文本也就不会干扰窗口级空格快捷键的判定）
FRAME_DIGITS_PATTERN = r"[0-9]*"


def format_duration(ms: int) -> str:
    """毫秒 → 时长文本：不足 1 小时为 mm:ss，否则 h:mm:ss；负值钳零。"""
    total = max(0, int(ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def is_playback_toggle_key(key: int, auto_repeat: bool) -> bool:
    """空格键且非自动重复 → 窗口级播放/暂停快捷键判定（离线测试用）。

    焦点在可编辑文本框（路径/起止点）时按键由其消费不会冒泡到窗口，
    自然保持文本输入语义；其余控件一律 NoFocus 不夺焦点。
    """
    return key == int(Qt.Key.Key_Space) and not auto_repeat


def should_seek_now(
    now_ms: float, last_seek_ms: float, throttle_ms: int = SEEK_THROTTLE_MS
) -> bool:
    """拖拽 seek 节流判定（纯函数，离线测试用）：距上次真实 seek 不足节流
    窗口则挂起本次请求；``last_seek_ms < 0`` 表示从未 seek（首拍必放行）。"""
    return last_seek_ms < 0 or now_ms - last_seek_ms >= throttle_ms


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
    """进度条：左键单击任意位置跳转；按住左键拖动连续调节。

    拖拽采用控件自管状态机（_dragging）：QSlider 内部拖拽需父类处理
    press 事件，而单击跳转必须接管 press（accept 后父类拖拽永远不会
    启动，表现为只有单击跳转、无连续拖拽）——因此 press/move/release
    全部自管：press 跳转并进入拖拽态，move 按 x 位置连续发 sliderMoved
    （窗口端节流 seek），release 发最终值与 sliderReleased（窗口端落定）。
    """

    def __init__(self, orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._dragging = False

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            # 单击跳转（需求：点击刻度任意位置直接跳转到对应进度）；
            # 随后进入拖拽跟踪，按住移动即连续调节（sliderMoved → 窗口端
            # 节流 seek + 挂起，sliderReleased 落定）
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
            self.sliderMoved.emit(value)  # 窗口端节流 seek + 挂起
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            # 松手落定：先发最终 moved（窗口更新挂起位置），再发 released
            # （窗口端把挂起位置补 seek 落定）
            self.sliderMoved.emit(self.value())
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


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


class SdlCanvas(QWidget):
    """SDL 渲染器宿主：原生窗口句柄交给 SDL_CreateWindowFrom。

    客户区内容完全归 SDL present（direct3d 翻转），Qt 侧必须双重禁绘：
    WA_OpaquePaintEvent（跳过 paintEvent 前的调色板擦底——否则任何 UI
    刷新引发的重绘都会白屏覆盖 SDL 画面）+ WA_NoSystemBackground（跳过
    WM_ERASEBKGND 系统擦底）；paintEvent 保持空实现。暂停态单击放大：
    mousePress/Release 归 Qt（≤4px 位移判定），回调 on_frame_click 不变。
    """

    def __init__(self, on_frame_click, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_frame_click = on_frame_click
        self._press_pos = None
        self.setMinimumHeight(320)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 点击画布不夺焦点（空格可达窗口）
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)

    def native_hwnd(self) -> int:
        return int(self.winId())

    def paintEvent(self, event) -> None:  # noqa: N802 - 画面完全由 SDL 绘制
        event.accept()

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
        clock_ms=None,
        player_factory=None,
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

        # ---- 播放引擎（SDL2 + PyAV，移植自 videoplayer 已验证方案） -------------
        #: 引擎工厂（离线测试 stub；默认 VideoPlayer，解码在 worker 线程）
        self._player = player_factory(self) if player_factory is not None else VideoPlayer(self)
        self._player.open_failed.connect(
            lambda text: logger.warning("媒体打开失败：%s", text)
        )
        self._renderer: SdlVideoRenderer | None = None
        self._fps = DEFAULT_FPS
        #: 毫秒单调钟（构造注入，离线测试可控）：拖拽 seek 节流的时钟
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        #: 拖拽 seek 节流状态：上次真实 seek 时刻与挂起中的最终位置
        self._last_seek_ms = -1.0
        self._pending_seek: int | None = None
        #: 最近一次 seek 请求的目标（挂起与已落定均算）：连点步进的累加基准。
        #: position() 在落定后异步更新，直接读它连点会重复算出同一目标（实测
        #: 连续两次落定同一位置）；位置回放追平（差 ≤ 一帧）或用户拖拽后失效
        self._last_target_ms: int | None = None
        #: 挂起 seek 的兜底落定 timer（按钮/文本框 seek 无 sliderReleased 兜底）
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.setInterval(BUTTON_SEEK_THROTTLE_MS)
        self._pending_timer.timeout.connect(self._on_pending_timer_timeout)
        #: 引擎指标心跳（冻结/挂起判别，见 _log_liveness；口径取引擎 stats）
        self._liveness_timer = QTimer(self)
        self._liveness_timer.setInterval(LIVENESS_HEARTBEAT_MS)
        self._liveness_timer.timeout.connect(self._log_liveness)
        self._liveness_timer.start()

        # ---- 布局（需求自上而下） ----------------------------------------------
        layout = QVBoxLayout(self)

        path_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("输入视频路径后回车打开")
        self._path_edit.returnPressed.connect(self._open_from_path_edit)
        browse_button = QPushButton("选择视频")
        browse_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 空格快捷键焦点策略
        browse_button.clicked.connect(self._open_via_dialog)
        path_row.addWidget(self._path_edit, stretch=1)
        path_row.addWidget(browse_button)
        layout.addLayout(path_row)

        self._canvas = SdlCanvas(self._open_frame_viewer)
        layout.addWidget(self._canvas, stretch=1)  # 客户区归 SDL present（双重禁绘）

        self._position_slider = _ClickableSlider(Qt.Orientation.Horizontal)
        self._position_slider.setRange(0, 0)
        self._position_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 防夺焦点致空格失灵
        self._scrubbing = False  # 拖拽中不回写播放位置（防回环抖动）
        self._position_slider.sliderPressed.connect(self._on_slider_pressed)
        self._position_slider.sliderReleased.connect(self._on_slider_released)
        self._position_slider.sliderMoved.connect(self._on_slider_moved)
        layout.addWidget(self._position_slider)

        # 时间显示：当前播放时长 / 视频总时长（随播放与 seek 实时刷新）
        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._time_label)

        self._volume_slider = _VolumeSlider()
        self._volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 防夺焦点致空格失灵
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        layout.addWidget(self._volume_slider)

        controls_row = QHBoxLayout()
        for text, handler in (
            ("步退1秒", lambda: self._seek_by(-STEP_MS)),
            ("上一帧", lambda: self._seek_by(-frame_duration_ms(self._fps))),
            (PLAY_BUTTON_TEXT_PAUSED, self._toggle_play),
            ("下一帧", lambda: self._seek_by(frame_duration_ms(self._fps))),
            ("步进1秒", lambda: self._seek_by(STEP_MS)),
        ):
            button = QPushButton(text)
            # NoFocus：点击按钮不夺焦点——空格始终切换播放，而不是重复触发
            # 上次点击的那个按钮（Qt 按钮 Space/回车默认激活自身）
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(handler)
            if text in (PLAY_BUTTON_TEXT_PAUSED, PLAY_BUTTON_TEXT_PLAYING):
                self._play_button: QAbstractButton = button
            controls_row.addWidget(button)
        layout.addLayout(controls_row)

        self._start_edit = self._add_marker_row(layout, "定位起点")
        self._end_edit = self._add_marker_row(layout, "定位终点")

        self._asr_button = QPushButton("语音识别")
        self._asr_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 空格快捷键焦点策略
        self._asr_button.clicked.connect(self._request_asr)
        layout.addWidget(self._asr_button)

        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        # ---- 引擎信号 ------------------------------------------------------------
        self._player.position_changed.connect(self._on_position_changed)
        self._player.state_changed.connect(self._on_playback_state_changed)
        self._player.media_opened.connect(self._apply_media_info)

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
        from .av_media import av_available  # 局部导入：与录制/ASR 同一降级口径

        target = Path(path.strip().strip('"'))
        if not target.is_file():
            logger.info("视频文件不存在，忽略打开请求：%s", target)
            self._status_label.setText("文件不存在")
            return
        if not av_available():
            logger.warning("PyAV 不可用（import 失败），视频编辑降级")
            self._status_label.setText("PyAV 不可用，视频功能降级")
            return
        self._path_edit.setText(str(target))
        self._last_target_ms = None  # 新视频：步进累加基准失效
        self._player.open(str(target))  # 引擎：worker 打开，完成即暂停态+首帧
        self._volume_slider.setValue(0)  # 最小静音（需求）
        self.setFocus()  # 焦点移离路径框：空格快捷键立即可用（输入框不捕获空格）
        logger.info("已打开视频（暂停）：%s", target)

    # ---- 播放控制 ---------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """窗口级空格快捷键：切换播放/暂停（仅窗口焦点链路触发，非全局热键）。

        焦点在路径/起止点输入框时按键由其消费（文本输入语义保持）；
        其余控件一律 NoFocus，事件冒泡到本窗口统一判定。
        """
        if is_playback_toggle_key(event.key(), event.isAutoRepeat()):
            self._toggle_play()
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_play(self) -> None:
        if self._player.playbackState() == PlaybackState.Playing:
            self._player.pause()
        else:
            self._player.play()

    def _seek_by(self, delta_ms: int) -> None:
        # 连点累加基准：挂起目标 → 最近请求目标 → 播放位置。position() 在
        # seek 落定后异步更新，直接读它连点会重复算出同一目标（实测连续两次
        # 落定同一位置），点 N 次也退不足 N 秒
        if self._pending_seek is not None:
            base = self._pending_seek
        elif self._last_target_ms is not None:
            base = self._last_target_ms
        else:
            base = self._player.position()
        logger.debug("步进请求 delta=%+dms（base=%d）", delta_ms, base)
        self._seek_to(base + delta_ms)

    def _seek_to(self, position_ms: int, source: str = "button") -> None:
        """统一 seek 合并入口：节流窗口内的连续请求只挂起最新目标，由兜底
        timer 落定（拖拽路径由 sliderReleased 落定）。

        按钮路径原先直连 setPosition：步退/步进改 1 秒跨度后连点频率大增，
        高频 seek 风暴触发 FFmpeg 后端抢占，解码帧交错花屏 + 主线程
        toImage 竞争卡死（实测）；经合并节流后任意时刻在途真实 seek 至多一次。
        """
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
        """落定挂起 seek（按钮兜底 timer / 拖拽 sliderReleased 共用出口）。

        引擎 setPosition 为异步投递（最新胜出，worker 串行消费），耗时无
        诊断意义；完成帧与位置回正由引擎信号异步回报。
        """
        if self._pending_seek is None:
            return
        target, self._pending_seek = self._pending_seek, None
        self._pending_timer.stop()
        self._last_seek_ms = self._clock_ms() if now is None else now
        self._last_target_ms = target  # 步进累加基准更新为本次落定目标
        self._player.setPosition(target)
        logger.debug("seek 落定 → %dms", target)

    def _on_pending_timer_timeout(self) -> None:
        if self._scrubbing:  # 拖拽中由 sliderReleased 落定，timer 不抢
            return
        self._commit_pending_seek()

    def _on_playback_state_changed(self, state) -> None:
        playing = state == PlaybackState.Playing
        self._play_button.setText(PLAY_BUTTON_TEXT_PLAYING if playing else PLAY_BUTTON_TEXT_PAUSED)

    def _apply_media_info(self, info) -> None:
        """引擎元数据回报：帧率（帧位置精确到帧）与时长（进度条范围）。"""
        self._fps = info.fps
        self._on_duration_changed(info.duration_ms)
        self._update_time_label(self._player.position())  # 打开新视频：归零显示
        logger.debug("视频元数据：fps=%s duration=%dms", info.fps, info.duration_ms)

    # ---- 进度条 -----------------------------------------------------------------

    def _on_slider_pressed(self) -> None:
        self._scrubbing = True

    def _on_slider_released(self) -> None:
        self._scrubbing = False
        # 拖拽终点保证落定：节流期间挂起的最终位置补一次 seek（共用出口）
        self._commit_pending_seek()

    def _on_slider_moved(self, value: int) -> None:
        """拖拽进度：节流 seek（高频 sliderMoved 不逐个触发 FFmpeg seek）。

        每次 moved 都记录挂起位置；距上次真实 seek 超过节流窗口才落一次
        seek，最终位置由 sliderReleased 补齐——消除 seek 风暴导致的画面
        撕裂与主线程被解码竞争拖垮（实测卡死）。
        """
        self._pending_seek = value
        now = self._clock_ms()
        if should_seek_now(now, self._last_seek_ms):
            self._last_seek_ms = now
            self._last_target_ms = None  # 用户拖拽主动定位：步进累加基准失效
            self._player.setPosition(value)
            logger.debug("拖拽 seek → %dms", value)

    def _on_position_changed(self, position_ms: int) -> None:
        self._update_time_label(position_ms)
        if self._last_target_ms is not None and abs(
            position_ms - self._last_target_ms
        ) <= frame_duration_ms(self._fps):
            self._last_target_ms = None  # 位置回放已追平请求目标：步进改以实际位置为基准
        if not self._scrubbing:
            self._position_slider.blockSignals(True)
            self._position_slider.setValue(position_ms)
            self._position_slider.blockSignals(False)

    def _update_time_label(self, position_ms: int) -> None:
        """时间显示：当前播放时长 / 视频总时长（播放/seek/打开新视频均刷新）。"""
        self._time_label.setText(
            f"{format_duration(position_ms)} / {format_duration(self._player.duration())}"
        )

    def _on_duration_changed(self, duration_ms: int) -> None:
        self._position_slider.setRange(0, max(0, duration_ms))

    # ---- 音量条 -----------------------------------------------------------------

    def _on_volume_changed(self, value: int) -> None:
        self._player.setVolume(value / 100.0)

    # ---- 起止点（捕获 / 跳转 / 文本框回车） ---------------------------------------

    def _add_marker_row(self, layout: QVBoxLayout, label_text: str) -> QLineEdit:
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        frame_edit = QLineEdit("0")
        # 纯数字校验（帧号类文本框合规输入）：空格/非数字在输入层拒绝
        frame_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(FRAME_DIGITS_PATTERN), self)
        )
        frame_edit.returnPressed.connect(
            lambda: self._jump_to_frame_text(frame_edit)
        )
        capture_button = QPushButton("捕获")
        capture_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 空格快捷键焦点策略
        capture_button.clicked.connect(lambda: self._capture_frame(frame_edit))
        jump_button = QPushButton("跳转")
        jump_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 空格快捷键焦点策略
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
        self._seek_to(frame_to_ms(frame, self._fps), "marker")

    # ---- 帧放大（暂停态单击播放区） -------------------------------------------------

    def set_locked_hwnd_provider(self, provider) -> None:
        """注入截图窗口句柄提供者（帧放大窗压低层级的参照来源）。"""
        self._locked_hwnd_provider = provider

    def _close_frame_viewer(self) -> None:
        """关闭并弃用当前帧放大窗（幂等；容错 C++ 对象已被异步销毁）。

        closeEvent 内的 deleteLater 是异步销毁：销毁执行后 PyQt 包装对象
        失效，但本窗口的 Python 属性仍残留失效引用——对已删除对象调
        close() 会 RuntimeError 崩溃（实测 logs/renpy_overlay_20261011_
        193239.log）。先置空引用再容错关闭。
        """
        old, self._frame_viewer = self._frame_viewer, None
        if old is None:
            return
        try:
            old.close()
        except RuntimeError:
            logger.debug("旧帧放大窗 C++ 对象已销毁，跳过关闭")

    def _on_frame_viewer_destroyed(self) -> None:
        """帧放大窗 C++ 对象销毁（deleteLater 异步执行）后同步清引用。"""
        self._frame_viewer = None

    def _open_frame_viewer(self) -> None:
        """暂停时单击播放区：放大当前帧（播放中单击忽略，需求）。"""
        if self._player.playbackState() == PlaybackState.Playing:
            return
        frame = self._player.grab_frame_image()  # 最近显示帧按需 yuv→QImage
        if frame is None or frame.isNull():
            return
        factory = self._frame_viewer_factory
        if factory is None:
            from .frame_viewer import FrameViewerWindow

            factory = FrameViewerWindow
        self._close_frame_viewer()  # 关闭旧实例（容错已销毁引用）
        viewer = factory(frame, lower_than=self._locked_hwnd_provider)
        # 必须持有引用：顶层窗口所有权归 Python，局部变量出栈后即被销毁
        self._frame_viewer = viewer
        # C++ 对象销毁（deleteLater 异步执行）时同步清引用，避免残留失效包装
        viewer.destroyed.connect(self._on_frame_viewer_destroyed)
        viewer.show_frame()
        logger.info("已打开帧放大窗（帧号 %d）", ms_to_frame(self._player.position(), self._fps))

    def _log_liveness(self) -> None:
        """引擎指标心跳（DEBUG）：presented/水位/underrun/seek 耗时周期汇总。

        冻结判别器：主线程冻结时本日志消失（真空窗口即冻结时长）；引擎
        挂起时主线程存活但 presented 持续为 0——两类情况可区分。
        """
        snap = self._player.stats_snapshot()
        logger.debug(
            "引擎指标：presented=%d dropped=%d stale=%d 水位=%.1fms "
            "underrun=%d seek=%d次（最近 %.1fms）vqueue=%d",
            snap["presented"],
            snap["dropped"],
            snap["stale"],
            snap["audio_level_ms"],
            snap["underruns"],
            snap["seek_count"],
            snap["seek_latency_ms"],
            snap["vqueue_depth"],
        )

    # ---- SDL 渲染装配与生命周期（画布句柄就绪后一次性） -----------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._renderer is None:
            self._player.ensure_sdl()  # 渲染器 attach 前保证 SDL 已初始化
            wiring = self._player.renderer_wiring()
            renderer = SdlVideoRenderer(**wiring)
            if renderer.attach_window(self._canvas.native_hwnd()):
                self._player.set_renderer(renderer)
                renderer.start()
                logger.info("SDL 渲染线程已启动（挂载画布句柄）")
            else:
                self._status_label.setText("SDL 渲染器初始化失败（见日志）")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._pause_engine_and_drop_renderer()
        super().closeEvent(event)

    def _pause_engine_and_drop_renderer(self) -> None:
        """窗口 close 的引擎收尾：暂停 + 弃置渲染线程（离线可测的纯编排）。

        不调 player.close()：解码线程不可重启，close 杀 worker 会导致再
        次从菜单打开编辑窗后选视频无帧无声（实测）；引擎随宿主生命周期
        常驻，窗口重开时渲染线程由 showEvent 自动重建。
        """
        self._player.pause()
        if self._renderer is not None:
            self._renderer.stop()
            self._renderer.wait(timeout=2.0)
            self._renderer.destroy_sdl()
            self._renderer = None  # 下次 showEvent 自动重建渲染线程
        logger.info("编辑窗口关闭：引擎已暂停（worker 保留，再次打开自愈）")

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
        from .av_media import normalize_range

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
