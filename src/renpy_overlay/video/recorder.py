"""全屏录制控制器：PyAV gdigrab 录屏 + 环回录音双流，停止时合并。

需求（.raw_plans/future_视频录制.txt）：右键菜单「录制视频」开关式录制，
全屏保存为 ``renpy_overlay_cache/video/yyyyMMdd-hhmmss.mp4``（自动建目录）；
录制中菜单显示「录制视频 mm:ss」；开始 / 每 10 分钟 / 结束经标题窗提示。

双流方案（见 :mod:`.audio_capture` 模块说明）：

- 视频流：PyAV gdigrab 抓主屏 + libx264 编码（:mod:`.screen_capture`，
  不再依赖用户自备的 ffmpeg.exe），统一录到 **mkv 中间容器**（无 moov
  依赖、簇式容错封装：即使异常未 finalize，已写数据仍可被 remux）；
  音频流：``record_loopback`` 后台线程持续环回录音到临时 wav；停止时经
  :func:`.av_media.merge_streams` 封装为最终 mp4（双流：视频 copy + aac
  + 时长对齐；仅视频降级：单流 remux），并清理临时文件。

线程与信号约束：

- 本控制器在 Qt 主线程使用（信号与 QTimer 的归属线程要求）；录屏编码
  在 daemon 线程执行，结束经 ``pyqtSignal`` queued 回主线程收尾；
- 停止为协作式：置位 stop_event 后编码线程在 ≤1 帧间隔内退出并正常
  finalize mkv（原 ffmpeg 进程的 terminate / stdin 'q' / 强杀三层策略
  随进程消失而移除）；编码失败或未请求停止即退出按中止收尾（不保留
  产物，对应原 QProcess errorOccurred / CrashExit 语义）；
- 停止顺序固定：置停止事件 → 编码线程结束信号 → stop_event 停录音并
  join（等 wav 头写完）→ 封装 → 清理临时文件 → 提示结束；
- 录制状态判定与重复点击幂等：_stopping/_recording 双标志防重入，不重复
  触发封装/清理。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..translation_store import CACHE_DIR_NAME
from .audio_capture import RECORD_AVAILABLE, record_loopback
from .av_media import DEFAULT_RECORD_FPS, merge_streams
from .screen_capture import CAPTURE_AVAILABLE, record_screen

logger = logging.getLogger("renpy_overlay.video.recorder")

#: 录制中提示周期（毫秒）：需求「每录制10分钟提示一次」
PROGRESS_INTERVAL_MS = 10 * 60 * 1000
#: 每次周期提示的间隔（分钟，与 PROGRESS_INTERVAL_MS 对齐）
PROGRESS_STEP_MINUTES = 10

RECORD_START_TEXT = "开始录制视频，保存在renpy_overlay_cache/video"
RECORD_STOP_TEXT = "录制结束，保存在renpy_overlay_cache/video"

#: 降级提示（PyAV 缺失等不可录制情形；原文案提及的 ffmpeg.exe 已不适用）
RECORD_UNAVAILABLE_TEXT = "视频录制不可用：视频组件初始化失败"

#: 菜单项文案（需求：录制中显示「录制视频 mm:ss」，结束后恢复「录制视频」）
MENU_LABEL_IDLE = "录制视频"


def progress_text(minutes: int) -> str:
    """周期提示文案（需求给定措辞，mm 为经过的整分钟数）。"""
    return f"视频录制中，已经过 {minutes}分钟"


def menu_label(elapsed_seconds: int | None) -> str:
    """菜单项文案：``None``（未录制）→「录制视频」，否则「录制视频 mm:ss」。"""
    if elapsed_seconds is None:
        return MENU_LABEL_IDLE
    minutes, seconds = divmod(max(0, int(elapsed_seconds)), 60)
    return f"{MENU_LABEL_IDLE} {minutes:02d}:{seconds:02d}"


def default_video_dir(game_dir) -> Path:
    """录制产物目录：游戏目录缓存下的 ``video``（缺省回退当前工作目录）。"""
    anchor = Path(game_dir) if game_dir else Path.cwd()
    return anchor / CACHE_DIR_NAME / "video"


def physical_geometry(
    x: int, y: int, width: int, height: int, device_pixel_ratio: float
) -> tuple[int, int, int, int]:
    """Qt 逻辑像素几何 → gdigrab 物理像素几何（纯函数，离线测试用）。

    进程为 Per-Monitor DPI Aware（win32api 的 DPI awareness 设置），Qt 的
    ``QScreen.geometry()`` 返回逻辑像素；而 gdigrab 按桌面物理像素抓屏——
    直接把逻辑宽高当抓屏区域会只录到屏幕左上一块（DPR≠1 的屏幕上实测）。
    物理 = 逻辑 × devicePixelRatio（与 ``stream_window._virtual_screen_rect``
    同一口径）；DPR 非法（None/≤0）按 1.0 处理，宽高下限钳 1。
    """
    dpr = device_pixel_ratio if device_pixel_ratio and device_pixel_ratio > 0 else 1.0
    return (
        round(x * dpr),
        round(y * dpr),
        max(1, round(width * dpr)),
        max(1, round(height * dpr)),
    )


def default_geometry(screen=None) -> tuple[int, int, int, int]:
    """主屏几何（gdigrab 虚拟桌面物理像素坐标）。

    Per-Monitor DPI Aware 进程里 ``QScreen.geometry()`` 是 Qt 逻辑像素，
    须经 devicePixelRatio 换算为物理像素（gdigrab 只认物理）；``screen``
    仅供离线测试注入鸭子接口（geometry()/devicePixelRatio()）。
    """
    from PyQt6.QtGui import QGuiApplication

    screen = screen or QGuiApplication.primaryScreen()
    if screen is None:  # pragma: no cover - 无显示环境兜底
        return (0, 0, 1920, 1080)
    geo = screen.geometry()
    return physical_geometry(
        geo.x(), geo.y(), geo.width(), geo.height(), screen.devicePixelRatio()
    )


class VideoRecorder(QObject):
    """录制编排器：开始（双流）/ 停止（停录屏 → 停录音 → 合并）状态机。

    构造注入（同 QuickMenu/MouseLockController 的宿主能力注入约定）：
    ``notify`` 为标题窗提示回调；``game_dir`` 锚定缓存目录；其余工厂/
    函数参数仅供离线测试注入 stub，默认接真实实现。
    """

    #: 录屏编码线程完成（ok=正常收尾）；跨线程 emit 由 Qt queued 到主线程
    _capture_done = pyqtSignal(bool)

    def __init__(
        self,
        *,
        notify,
        game_dir,
        geometry_fn=default_geometry,
        capture_fn=record_screen,
        audio_recorder=record_loopback,
        audio_available=RECORD_AVAILABLE,
        available_fn=CAPTURE_AVAILABLE,
        merge_fn=merge_streams,
        clock_fn=datetime.now,
        monotonic_fn=time.monotonic,
        fps: int = DEFAULT_RECORD_FPS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._notify = notify
        self._video_dir = default_video_dir(game_dir)
        self._geometry_fn = geometry_fn
        self._capture_fn = capture_fn
        self._audio_recorder = audio_recorder
        self._audio_available = audio_available
        self._available_fn = available_fn
        self._merge_fn = merge_fn
        self._clock_fn = clock_fn
        self._monotonic_fn = monotonic_fn
        self._fps = fps

        self._recording = False
        self._stopping = False  # 停止流程进行中（防完成信号双触发重入）
        self._started_monotonic = 0.0
        self._progress_ticks = 0
        self._capture_geometry: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._capture_thread: threading.Thread | None = None
        self._capture_stop: threading.Event | None = None
        self._audio_thread: threading.Thread | None = None
        self._audio_stop = threading.Event()
        self._tmp_video: Path | None = None
        self._tmp_audio: Path | None = None
        self._final_path: Path | None = None
        self._with_audio = False

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        self._capture_done.connect(self._on_capture_done)

    # ---- 状态（快捷菜单文案 / 宿主退出清理用） ---------------------------------

    @property
    def recording(self) -> bool:
        return self._recording

    def elapsed_seconds(self) -> int | None:
        """已录制秒数；未录制返回 ``None``（菜单文案据此切换）。"""
        if not self._recording:
            return None
        return int(self._monotonic_fn() - self._started_monotonic)

    def menu_label(self) -> str:
        return menu_label(self.elapsed_seconds())

    # ---- 开关（右键菜单项调用） -----------------------------------------------

    def toggle(self) -> None:
        if self._recording:
            self.stop()
        else:
            self.start()

    def start(self) -> bool:
        """开始录制：拉起录屏编码线程（有录音库时同时拉起环回录音线程）。

        PyAV 缺失时提示降级并返回 False；成功返回 True。
        """
        if self._recording or self._stopping:
            logger.info("已在录制中，忽略重复开始请求")
            return False
        if not self._available_fn():
            logger.warning("视频组件不可用，视频录制不可用")
            self._notify(RECORD_UNAVAILABLE_TEXT)
            return False

        try:
            self._video_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("创建录制目录失败（%s）：%s", self._video_dir, exc)
            self._notify("视频录制失败：无法创建保存目录")
            return False

        timestamp = self._clock_fn().strftime("%Y%m%d-%H%M%S")
        self._final_path = self._video_dir / f"{timestamp}.mp4"
        self._with_audio = bool(self._audio_available)
        # 视频流统一录到 mkv 中间容器（无 moov 依赖、簇式容错封装：编码
        # 线程异常退出后已写数据仍可被 remux 为最终 mp4）；停止时经封装
        # 生成最终 mp4，封装失败回退保留 mkv 为最终产物。
        self._tmp_video = self._video_dir / f"{timestamp}.tmp.mkv"
        if self._with_audio:
            self._tmp_audio = self._video_dir / f"{timestamp}.tmp.wav"
        else:
            logger.warning("无可用录音方案，本次录制降级为仅视频")
            self._tmp_audio = None

        self._capture_geometry = self._geometry_fn()
        self._capture_stop = threading.Event()
        self._capture_thread = threading.Thread(
            target=self._capture_worker,
            name="video-screen-encode",
            daemon=True,
        )
        self._capture_thread.start()

        if self._with_audio:
            self._audio_stop = threading.Event()
            self._audio_thread = threading.Thread(
                target=self._audio_recorder,
                args=(self._tmp_audio, self._audio_stop),
                name="video-audio-loopback",
                daemon=True,
            )
            self._audio_thread.start()

        self._recording = True
        self._started_monotonic = self._monotonic_fn()
        self._progress_ticks = 0
        self._progress_timer.start()
        logger.info(
            "开始录制视频：%s（%s）",
            self._final_path,
            "视频+系统音频" if self._with_audio else "仅视频",
        )
        self._notify(RECORD_START_TEXT)
        return True

    def stop(self) -> None:
        """停止录制：置位录屏线程停止事件（协作式，≤1 帧间隔内退出并
        finalize mkv），收尾在完成信号回调中继续（:meth:`_on_capture_done`）。

        幂等：重复点击被 _stopping 守卫拦截，不重复触发收尾。
        """
        if not self._recording or self._stopping:
            return
        self._stopping = True
        self._progress_timer.stop()
        self._capture_stop.set()

    def shutdown(self) -> None:
        """宿主退出清理：录制中则置停止事件并同步等待编码线程收尾（不抛异常）。"""
        if not self._recording:
            return
        self._progress_timer.stop()
        self._capture_stop.set()
        thread = self._capture_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():  # pragma: no cover - 编码异常挂死兜底
                logger.error(
                    "录屏线程未在 5s 内退出，放弃等待（mkv 已写数据仍可被 remux）"
                )
        self._capture_thread = None
        self._finish_recording(aborted=False)

    # ---- 内部：编码线程与收尾 ---------------------------------------------------

    def _capture_worker(self) -> None:
        """录屏编码线程：record_screen 完成后经信号回主线程收尾。"""
        offset_x, offset_y, width, height = self._capture_geometry
        ok = self._capture_fn(
            self._tmp_video,
            self._capture_stop,
            offset_x=offset_x,
            offset_y=offset_y,
            width=width,
            height=height,
            fps=self._fps,
        )
        self._capture_done.emit(bool(ok))

    def _on_capture_done(self, ok: bool) -> None:
        """录屏线程结束（主线程）：按请求状态进入收尾流程。

        未请求停止即结束（编码线程意外退出）或编码失败：按中止收尾，
        不保留产物；迟到信号（shutdown 已同步收尾）被 _recording 拦截。
        """
        if not self._recording:
            return  # 幂等：shutdown 同步收尾后迟到的信号不再重入
        if not self._stopping:
            logger.warning("录屏线程意外退出（ok=%s），提前结束本次录制", ok)
            self._finish_recording(aborted=True)
            return
        if not ok:
            logger.warning("录屏编码失败，按中止收尾")
            self._finish_recording(aborted=True)
            return
        self._finish_recording(aborted=False)

    def _on_progress_tick(self) -> None:
        """每 10 分钟周期提示（需求措辞，含累计分钟数）。"""
        if not self._recording:
            self._progress_timer.stop()
            return
        self._progress_ticks += 1
        self._notify(progress_text(self._progress_ticks * PROGRESS_STEP_MINUTES))

    def _finish_recording(self, *, aborted: bool) -> None:
        """统一收尾：停录音 → 合并（或无声回退）→ 清理 → 提示与状态复位。"""
        if not self._recording and not self._stopping:
            return  # 幂等：重复回调不重入
        self._stopping = False
        self._recording = False
        self._progress_timer.stop()

        # 停录音并等待线程退出（块间检查 + join，等 wav 头写完）
        if self._audio_thread is not None:
            self._audio_stop.set()
            self._audio_thread.join(timeout=5.0)
            if self._audio_thread.is_alive():  # pragma: no cover - 设备阻塞兜底
                logger.error("录音线程未在 5s 内退出，放弃等待（wav 可能不完整）")
            self._audio_thread = None

        final_path = self._final_path
        tmp_video, tmp_audio = self._tmp_video, self._tmp_audio
        try:
            if aborted:
                logger.info("录制中止：%s 不保留", final_path)
                self._cleanup_tmp()
                return
            # 统一封装路径：双流 = 视频流 copy + aac 音轨；仅视频降级时
            # audio_path 为 None，退化为 mkv → mp4 单流 remux（均秒级完成）
            finalized = self._merge_fn(
                video_path=tmp_video,
                audio_path=tmp_audio,
                out_path=final_path,
            )
            if finalized:
                logger.info(
                    "录制完成（%s）：%s",
                    "含系统音频" if tmp_audio is not None else "仅视频",
                    final_path,
                )
            else:
                self._fallback_to_intermediate_video()
        finally:
            self._cleanup_tmp()
            self._notify(RECORD_STOP_TEXT)
            self._tmp_video = None
            self._tmp_audio = None
            self._capture_thread = None
            self._capture_stop = None
            self._final_path = None

    def _fallback_to_intermediate_video(self) -> None:
        """封装失败回退：mkv 中间文件改名为 .mkv 最终产物（不丢录像）。

        mkv 为簇式容错容器（异常未 finalize 也可读），改名后扩展名与内容
        匹配；正常路径的最终产物仍是 .mp4（同 Stem 的 .mp4 不存在，因为
        封装失败）。
        """
        fallback_path = (
            self._final_path.with_suffix(".mkv") if self._final_path else None
        )
        logger.warning("视频封装失败，保留中间容器为最终产物：%s", fallback_path)
        try:
            if self._tmp_video is not None and fallback_path is not None:
                self._tmp_video.replace(fallback_path)
        except OSError as exc:  # pragma: no cover - 文件系统竞态
            logger.error("保留中间容器视频失败：%s", exc)

    def _cleanup_tmp(self) -> None:
        """删除双流临时文件（合并成功或中止路径；改名回退后已不存在）。"""
        for path in (self._tmp_video, self._tmp_audio):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - 文件被占用等
                logger.warning("清理临时文件失败（%s）：%s", path, exc)
