"""全屏录制控制器：ffmpeg gdigrab 视频 + 环回录音双流，停止时合并。

需求（.raw_plans/future_视频录制.txt）：右键菜单「录制视频」开关式录制，
全屏保存为 ``renpy_overlay_cache/video/yyyyMMdd-hhmmss.mp4``（自动建目录）；
录制中菜单显示「录制视频 mm:ss」；开始 / 每 10 分钟 / 结束经标题窗提示。

双流方案（见 :mod:`.audio_capture` 模块说明）：

- 视频流：QProcess 驱动用户自备的 ffmpeg.exe（gdigrab 抓主屏），统一录到
  **mkv 中间容器**（无 moov 依赖、簇式容错封装：即使进程被强杀，已写数据
  仍可被 ffmpeg 读取）；音频流：``record_loopback`` 后台线程持续环回录音到
  临时 wav；停止时 ffmpeg 封装为最终 mp4（双流：视频 copy + aac + -shortest；
  仅视频降级：单流 remux），并清理临时文件。

线程与进程约束：

- 本控制器在 Qt 主线程使用（QProcess 信号与 QTimer 的归属线程要求）；
- ffmpeg 是无窗口控制台进程，QProcess.terminate() 投递的 WM_CLOSE 没有接收
  者、单独使用无效（实测录制无法结束）——停止采用三层策略：terminate →
  stdin 写 'q'（ffmpeg 交互退出命令，正常 finalize）→ 超时强杀兜底（mkv
  中间容器保证强杀后产物仍可封装）；进程异常退出同样收尾；
- 停止顺序固定：优雅退出 → stop_event 停录音并 join（等 wav 头写完）→
  封装 → 清理临时文件 → 提示结束；
- 录制状态判定与重复点击幂等：_stopping/_recording 双标志防重入，不重复
  触发封装/清理。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer

from ..translation_store import CACHE_DIR_NAME
from .audio_capture import RECORD_AVAILABLE, record_loopback
from .ffmpeg import DEFAULT_RECORD_FPS, find_ffmpeg, merge_streams, video_record_args

logger = logging.getLogger("renpy_overlay.video.recorder")

#: 录制中提示周期（毫秒）：需求「每录制10分钟提示一次」
PROGRESS_INTERVAL_MS = 10 * 60 * 1000
#: 每次周期提示的间隔（分钟，与 PROGRESS_INTERVAL_MS 对齐）
PROGRESS_STEP_MINUTES = 10
#: 优雅退出等待超时（毫秒）：terminate + stdin 'q' 均未生效时强杀录制进程
STOP_GRACE_TIMEOUT_MS = 10_000

RECORD_START_TEXT = "开始录制视频，保存在renpy_overlay_cache/video"
RECORD_STOP_TEXT = "录制结束，保存在renpy_overlay_cache/video"

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


def default_geometry() -> tuple[int, int, int, int]:
    """主屏几何 ``(x, y, width, height)``（gdigrab 虚拟桌面坐标）。"""
    from PyQt6.QtGui import QGuiApplication

    screen = QGuiApplication.primaryScreen()
    if screen is None:  # pragma: no cover - 无显示环境
        return (0, 0, 1920, 1080)
    geo = screen.geometry()
    return (geo.x(), geo.y(), geo.width(), geo.height())


class VideoRecorder(QObject):
    """录制编排器：开始（双流）/ 停止（收视频 → 停录音 → 合并）状态机。

    构造注入（同 QuickMenu/MouseLockController 的宿主能力注入约定）：
    ``notify`` 为标题窗提示回调；``game_dir`` 锚定缓存目录；其余工厂/
    函数参数仅供离线测试注入 stub，默认接真实实现。
    """

    def __init__(
        self,
        *,
        notify,
        game_dir,
        geometry_fn=default_geometry,
        process_factory=None,
        audio_recorder=record_loopback,
        audio_available=RECORD_AVAILABLE,
        ffmpeg_path: str | None = None,
        find_fn=find_ffmpeg,
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
        self._process_factory = process_factory
        self._audio_recorder = audio_recorder
        self._audio_available = audio_available
        self._ffmpeg_path = ffmpeg_path
        self._find_fn = find_fn
        self._merge_fn = merge_fn
        self._clock_fn = clock_fn
        self._monotonic_fn = monotonic_fn
        self._fps = fps

        self._recording = False
        self._stopping = False  # 停止流程进行中（防 finished/error 双触发重入）
        self._started_monotonic = 0.0
        self._progress_ticks = 0
        self._ffmpeg: str | None = None
        self._process = None  # QProcess / stub：视频流进程
        self._audio_thread: threading.Thread | None = None
        self._audio_stop = threading.Event()
        self._tmp_video: Path | None = None
        self._tmp_audio: Path | None = None
        self._final_path: Path | None = None
        self._with_audio = False

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(PROGRESS_INTERVAL_MS)
        self._progress_timer.timeout.connect(self._on_progress_tick)
        # 优雅退出超时兜底：terminate + stdin 'q' 均未生效时强杀（单发）
        self._stop_grace_timer = QTimer(self)
        self._stop_grace_timer.setSingleShot(True)
        self._stop_grace_timer.setInterval(STOP_GRACE_TIMEOUT_MS)
        self._stop_grace_timer.timeout.connect(self._on_stop_grace_timeout)

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
        """开始录制：拉起 ffmpeg 视频流（有录音库时同时拉起环回录音线程）。

        ffmpeg 缺失时提示降级并返回 False；成功返回 True。
        """
        if self._recording or self._stopping:
            logger.info("已在录制中，忽略重复开始请求")
            return False
        ffmpeg = self._ffmpeg_path or self._find_fn()
        if not ffmpeg:
            logger.warning("未找到 ffmpeg.exe，视频录制不可用")
            self._notify("视频录制不可用：未找到 ffmpeg.exe")
            return False
        self._ffmpeg = ffmpeg

        try:
            self._video_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("创建录制目录失败（%s）：%s", self._video_dir, exc)
            self._notify("视频录制失败：无法创建保存目录")
            return False

        timestamp = self._clock_fn().strftime("%Y%m%d-%H%M%S")
        self._final_path = self._video_dir / f"{timestamp}.mp4"
        self._with_audio = bool(self._audio_available)
        # 视频流统一录到 mkv 中间容器（无 moov 依赖、簇式容错封装：进程被
        # 强杀后已写数据仍可被 ffmpeg 读取并 remux 为最终 mp4）；停止时经
        # 封装生成最终 mp4，封装失败回退保留 mkv 为最终产物。
        self._tmp_video = self._video_dir / f"{timestamp}.tmp.mkv"
        video_out = self._tmp_video
        if self._with_audio:
            self._tmp_audio = self._video_dir / f"{timestamp}.tmp.wav"
        else:
            logger.warning("无可用录音方案，本次录制降级为仅视频")
            self._tmp_audio = None

        offset_x, offset_y, width, height = self._geometry_fn()
        args = video_record_args(
            ffmpeg,
            offset_x=offset_x,
            offset_y=offset_y,
            width=width,
            height=height,
            out_path=video_out,
            fps=self._fps,
        )
        process = self._process_factory() if self._process_factory else _make_qprocess()
        process.finished.connect(self._on_process_finished)
        process.errorOccurred.connect(self._on_process_error)
        # PyQt6 的 QProcess.start 只接受 start(program, arguments) 两参形式
        # （PyQt5 的 start(command) 单参数重载已移除，传单个列表会 TypeError
        # 导致进程崩溃，实测见 logs/renpy_overlay_20261011_154701.log）
        process.start(args[0], args[1:])
        self._process = process

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
        """停止录制：优雅三连结束 ffmpeg，录音停止与封装在 finished 回调中
        继续（:meth:`_finish_recording`）。

        ffmpeg 是无窗口控制台进程，terminate() 投递的 WM_CLOSE 没有接收者、
        单独使用无效（实测录制无法结束）——补 stdin 写 'q'（ffmpeg 交互
        退出命令，正常 finalize），并加超时强杀兜底；幂等：重复点击被
        _stopping 守卫拦截，不重复触发收尾。
        """
        if not self._recording or self._stopping:
            return
        self._stopping = True
        self._progress_timer.stop()
        self._process.terminate()
        try:
            self._process.write(b"q")  # ffmpeg 交互命令：正常收尾退出
        except Exception:  # pragma: no cover - 进程已退出等竞态
            logger.debug("写入 stdin 退出命令失败", exc_info=True)
        self._stop_grace_timer.start()

    def _on_stop_grace_timeout(self) -> None:
        """优雅退出超时（terminate + stdin 'q' 均未生效）：强杀兜底。

        中间容器为 mkv（无 moov 依赖、簇式容错封装），强杀产物仍可被
        ffmpeg 读取并封装为最终 mp4，不会丢失已录制内容。
        """
        if not self._stopping:
            return
        logger.warning("优雅退出超时（%dms），强杀录制进程", STOP_GRACE_TIMEOUT_MS)
        try:
            self._process.kill()
        except Exception:  # pragma: no cover - 进程已退出等竞态
            logger.debug("强杀录制进程失败（进程可能已退出）", exc_info=True)

    def shutdown(self) -> None:
        """宿主退出清理：录制中则同步终止并完成收尾（不抛异常）。"""
        if not self._recording:
            return
        self._progress_timer.stop()
        try:
            try:
                self._process.write(b"q")  # 先尝试 ffmpeg 交互退出（正常收尾）
            except Exception:  # pragma: no cover - 进程已退出等竞态
                logger.debug("写入 stdin 退出命令失败", exc_info=True)
            self._process.terminate()
            if not self._process.waitForFinished(5000):
                self._process.kill()
                self._process.waitForFinished(1000)
        except Exception:  # pragma: no cover - 进程已退出等竞态
            logger.debug("停止录制进程时出现竞态", exc_info=True)
        self._finish_recording(aborted=False)

    # ---- 内部：进程回调与收尾 ---------------------------------------------------

    def _on_process_finished(self, *args) -> None:
        """ffmpeg 退出（正常停止或自行退出）：进入收尾流程。"""
        if not self._stopping:
            logger.warning("录制进程意外退出，提前结束本次录制")
        self._finish_recording(aborted=not self._stopping)

    def _on_process_error(self, error) -> None:
        """QProcess 错误：FailedToStart 不会伴随 finished（无后续回调），
        按中止处理；CrashExit 的 finished 回调由幂等保护吸收，不重入。"""
        if self._recording and not self._stopping:
            logger.warning("录制进程错误（error=%s），中止本次录制", error)
            self._finish_recording(aborted=True)

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
        self._stop_grace_timer.stop()  # 正常收尾后撤销强杀兜底（幂等守卫亦可拦）

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
                self._ffmpeg,
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
            self._process = None
            self._final_path = None

    def _fallback_to_intermediate_video(self) -> None:
        """封装失败回退：mkv 中间文件改名为 .mkv 最终产物（不丢录像）。

        mkv 为簇式容错容器（强杀也可读），改名后扩展名与内容匹配；正常
        路径的最终产物仍是 .mp4（同 Stem 的 .mp4 不存在，因为封装失败）。
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


def _make_qprocess():
    """真实 QProcess 工厂（局部导入：非 Qt 运行环境不触发）。"""
    from PyQt6.QtCore import QProcess

    process = QProcess()
    return process
