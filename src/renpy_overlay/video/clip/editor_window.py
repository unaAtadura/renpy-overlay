"""剪辑主窗口：三区布局 + 快捷键路由 + 命令流 + 字幕/导出流程。

布局（future_*.txt 外观）：

    上部：左 资源窗口 | 中 视频播放器 | 右 日志窗口
    下部：快捷按钮行（分割/删除/合并/退回/标记切片/上一帧/下一帧/
          制作字幕/加载字幕/应用）→ 时间轴 → 声波图

渲染管理（决策 2 演进）：单引擎双视图——渲染线程常驻，SDL 窗口/
渲染器/纹理全部在渲染线程内经 rebind 命令创建切换（创建线程=
操作线程，杜绝跨线程 SDL 窗口调用）；全屏默认走 SDL 独立顶层
窗口（config.fullscreen_mode="sdl"），"qt" 回退 attach 保底路径
（Qt 顶层窗口 + 画布重绑，本机存在窗口不显示风险）。

快捷键（future_*.txt 快捷按钮章节，全部窗口级局部快捷键）：
- space 暂停/播放；left/right 步退/步进 1 秒；ctrl+left/right 步进 1 帧；
  以上均支持长按 2 秒后 0.5 秒间隔连发（自管节拍，过滤键盘自动重复）；
- f 标记切片（状态机）；x 分割 / del 删除（选中声波图才生效）；
- c 合并（紧凑重排）；ctrl+z 退回（长按连发）。

字幕（制作/加载）与「应用」导出均经二次弹窗确认，后台线程执行，
进度与结果同步输出到日志区域。窗口不强制置顶。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...translator import TranslationError, translate_text
from ..asr import recognize_audio, resolve_asr_options
from ..av_media import extract_wav
from .editing.clip_marker import ClipMarkerModel
from .editing.commands import (
    DeleteCommand,
    JoinCommand,
    RestoreCommand,
    SplitCommand,
    UndoStack,
)
from .editing.exporter import (
    ExportAborted,
    SizeEstimate,
    default_export_path,
    estimate_export_size,
    export_clips,
)
from .editing.model import TimelineModel
from .editing.subtitle import (
    ASS_HEADER,
    SubtitleEntry,
    build_ass_dialogue,
    entries_to_segments,
    parse_ass,
)
from .fullscreen_player import FullscreenOverlay, FullscreenPlayer
from .log_panel import LogPanel
from .player.engine import PlaybackState
from .player.renderer_sdl2 import SdlVideoRenderer
from .player_pane import STEP_MS, PlayerPane, frame_duration_ms
from .resource_panel import ResourcePanel
from .timeline_view import TimelineView
from .view_state import ViewState
from .waveform import cached_waveform
from .waveform_view import WaveformView

logger = logging.getLogger("renpy_overlay.video.clip.editor_window")

#: 长按连发节拍（future_*.txt：长按 2 秒后以 0.5 秒间隔连续触发）
REPEAT_DELAY_MS = 2_000
REPEAT_INTERVAL_MS = 500

WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 900

#: 制作字幕文件命名：与视频同名（.ass，双语 ASS 字幕）
SUBTITLE_SUFFIX = ".ass"


class _WaveformWorker(QThread):
    """波形提取后台线程（载入视频后启动；结果经信号回主线程）。"""

    waveform_ready = pyqtSignal(object)  # WaveformData | None

    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self._path = path

    def run(self) -> None:  # noqa: D102 - QThread 入口
        self.waveform_ready.emit(cached_waveform(self._path))


class _SubtitleWorker(QThread):
    """制作字幕后台线程：逐切片 extract_wav → ASR → 翻译 → 追加写 ASS。

    文档规则：制作前删除同名字幕（失败中止，调用方保证）；文件头
    一次 + Dialogue 逐条追加落盘（中断保留已写内容）；双语两行
    （中文第一行/原文第二行，``\\N`` 换行）。

    API 互斥：制作开始经 ``api_acquire`` 占用宿主互斥槽（与实时语音
    识别同模式：批量任务独占，期间其余 API 入口被拦截），结束/失败/
    中断在 ``finally`` 释放；回调未注入时跳过（离线测试便利）。

    进度可见性：逐条进度经 ``entry_progress`` 信号回主线程写入日志面板
    的进度通道（携带第 N/总数、当前切片起止时间与 ASR/翻译阶段）——
    INFO 级恒可见，不依赖 logging 传播链，简洁模式默认可见且切换显示
    等级不丢失。
    """

    entry_progress = pyqtSignal(int, int, str)  # (已完成数, 总数, 进度说明)
    finished_ok = pyqtSignal(int)  # 成功条数
    failed = pyqtSignal(str)

    def __init__(
        self,
        segments,  # list[ClipSegment]（源时间）
        video_path: str,
        subtitle_path: str,
        config,  # 宿主 Config（翻译/ASR 三键回退链 + 超时/思考方言字段）
        api_acquire=None,
        api_release=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._segments = segments
        self._video_path = video_path
        self._subtitle_path = subtitle_path
        self._config = config
        self._api_acquire = api_acquire
        self._api_release = api_release
        self._stop = False

    def stop(self) -> None:
        """制作中断（再次点击制作字幕按钮）：保留已落盘内容。"""
        self._stop = True

    def run(self) -> None:
        done = 0
        if self._api_acquire is not None:
            self._api_acquire()
        try:
            # ASR 三键回退链（空值跟随主链路）与翻译主链路三键，一次解析
            asr_base_url, asr_model, asr_api_key = resolve_asr_options(self._config)
            with open(self._subtitle_path, "a", encoding="utf-8") as sub_file:
                sub_file.write(ASS_HEADER)  # 头段一次（文件已由调用方清空）
                for index, seg in enumerate(self._segments, start=1):
                    if self._stop:
                        # 中断状态经进度信号回主线程上屏（worker 不直接操作控件）
                        self.entry_progress.emit(
                            done,
                            len(self._segments),
                            f"制作已中断，保留已落盘 {done} 条",
                        )
                        break
                    total = len(self._segments)
                    seg_label = (
                        f"切片 {index}/{total} [{seg.start_ms}-{seg.end_ms}ms]"
                    )
                    wav = extract_wav(
                        video_path=self._video_path,
                        start_s=seg.start_ms / 1000.0,
                        end_s=seg.end_ms / 1000.0,
                    )
                    if wav is None:
                        raise RuntimeError(f"切片 {index} 音频截取失败")
                    src_text = recognize_audio(
                        wav,
                        base_url=asr_base_url,
                        api_key=asr_api_key,
                        model=asr_model,
                        timeout=self._config.api_timeout,
                        enable_thinking=self._config.enable_thinking,
                        reasoning_effort=self._config.reasoning_effort,
                    )
                    # 阶段进度（INFO 无心跳标记）：ASR 慢时也能看到切片推进
                    self.entry_progress.emit(
                        done, total, f"{seg_label} ASR 完成，翻译中"
                    )
                    zh_text = translate_text(
                        src_text,
                        base_url=self._config.api_base_url,
                        api_key=self._config.api_key,
                        model=self._config.model,
                        timeout=self._config.api_timeout,
                        enable_thinking=self._config.enable_thinking,
                        reasoning_effort=self._config.reasoning_effort,
                    )
                    entry = SubtitleEntry(
                        seg.start_ms, seg.end_ms, zh_text, src_text
                    )
                    sub_file.write(build_ass_dialogue(entry))
                    sub_file.flush()  # 逐条落盘（中断保留）
                    done += 1
                    self.entry_progress.emit(
                        done, total, f"{seg_label} ASR+翻译完成，已落盘"
                    )
        except (TranslationError, RuntimeError) as exc:
            # EndpointUnavailable 是 TranslationError 子类，一并收敛
            self.failed.emit(f"{exc}（已落盘 {done} 条）")
            return
        finally:
            if self._api_release is not None:
                self._api_release()
        self.finished_ok.emit(done)


class _ExportDialog(QDialog):
    """导出策略确认弹窗：预估信息 + 策略二选一（Radio）+ CRF 下拉。

    决策 2（已定案）：重编码场景提供「关键帧吸附」（无损，边界对齐关键
    帧，切口可能偏移 ≤半个 GOP）与「用户自配置 CRF」（帧精确）两个互斥
    选项；默认选中 CRF（帧精确，与切割语义一致），CRF 默认 18。
    """

    def __init__(
        self,
        segment_count: int,
        duration_s: float,
        out_path: str,
        estimate_line: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("应用剪辑")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"将按当前播放序列导出 {segment_count} 个片段"
                f"（总时长 {duration_s:.1f}s）：\n{out_path}\n{estimate_line}"
            )
        )
        layout.addWidget(QLabel("导出策略（二选一）："))
        self._radio_crf = QRadioButton("重编码（帧精确）—— CRF：")
        self._radio_crf.setChecked(True)  # 默认帧精确（与切割语义一致）
        layout.addWidget(self._radio_crf)
        crf_row = QHBoxLayout()
        crf_row.addSpacing(24)
        # CRF 0-28：0 为 x264 无损编码（体积极大），18 为视觉无损常用值，
        # 28 高压缩；默认 18（视觉无损）
        crf_row.addWidget(QLabel("CRF（0 无损 / 18 视觉无损 / 28 高压缩）"))
        self._crf_combo = QComboBox()
        for value in range(0, 29):
            self._crf_combo.addItem(str(value), value)
        self._crf_combo.setCurrentIndex(18)  # 默认 18
        crf_row.addWidget(self._crf_combo)
        crf_row.addStretch(1)
        layout.addLayout(crf_row)
        self._radio_snap = QRadioButton(
            "关键帧吸附（无损流拷贝；切口边界将对齐关键帧，可能偏移 ≤半个 GOP）"
        )
        layout.addWidget(self._radio_snap)
        layout.addWidget(
            QLabel("确认后将清空命令流（不可再退回）。")
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self) -> tuple[str, int | None]:
        """用户选择：("snap", None) 或 ("reencode", crf)。"""
        if self._radio_snap.isChecked():
            return "snap", None
        return "reencode", int(self._crf_combo.currentData())


class _ExportWorker(QThread):
    """「应用」导出后台线程：按片段序列导出 + 节流进度上报 + 可中断。"""

    progress_logged = pyqtSignal(str)  # 格式化进度行（日志区域展示）
    finished_ok = pyqtSignal(str)  # 输出路径
    failed = pyqtSignal(str)
    aborted = pyqtSignal()  # 用户中断（半成品已清理）

    #: 进度日志节流窗口（秒）：避免逐帧刷屏
    PROGRESS_INTERVAL_S = 0.5

    def __init__(
        self, clips, out_path: str, *,
        mode: str = "reencode", crf: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._clips = clips
        self._out_path = out_path
        self._mode = mode
        self._crf = crf
        self._last_emit = 0.0
        self._start_monotonic = 0.0
        self._stop = False

    def stop(self) -> None:
        """请求中断导出（异步；半成品由 run 内清理）。"""
        self._stop = True

    def run(self) -> None:  # noqa: D102
        import time

        self._start_monotonic = time.monotonic()
        self._last_emit = 0.0
        try:
            export_clips(
                self._clips,
                self._out_path,
                progress=self._on_progress,
                should_abort=lambda: self._stop,
                mode=self._mode,
                crf=self._crf,
            )
        except ExportAborted:
            self._cleanup_partial()
            self.aborted.emit()  # 中断提示由主窗口经 aborted 信号写入日志面板
            return
        except Exception as exc:  # 编码/IO 异常统一收敛
            self.failed.emit(str(exc))  # 失败原因由主窗口经 failed 信号写入面板
            return
        self.finished_ok.emit(self._out_path)

    def _cleanup_partial(self) -> None:
        """中断后删除半成品（避免残留不可播放文件）。"""
        try:
            Path(self._out_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("半成品清理失败：%s（%s）", self._out_path, exc)

    def _on_progress(self, done: int, total: int, note: str) -> None:
        """帧进度 → 节流后的日志行（百分比/已处理帧/总帧/速度/ETA）。

        本方法自身抛异常也不应中断导出（需求 4）：exporter 侧已隔离回
        调异常，此处再兜一层保证日志格式化错误不冒泡。
        """
        try:
            import time

            now = time.monotonic()
            if now - self._last_emit < self.PROGRESS_INTERVAL_S and done < total:
                return
            self._last_emit = now
            elapsed = max(1e-6, now - self._start_monotonic)
            pct = min(100.0, done / max(1, total) * 100.0)
            speed = done / elapsed  # 帧/秒
            eta_s = (total - done) / speed if done > 0 else None
            eta_part = f"，预计剩余 {eta_s:.0f}s" if eta_s is not None else ""
            note_part = f" {note}" if note else ""
            self.progress_logged.emit(
                f"导出进度 {pct:.1f}%（{done}/{total} 帧），"
                f"速度 {speed:.0f} 帧/秒{eta_part}{note_part}"
            )
        except Exception:
            logger.debug("导出进度格式化异常（忽略）", exc_info=True)


class EditorWindow(QMainWindow):
    """剪辑主窗口（宿主单例嵌入；不强制置顶）。

    宿主集成：``config`` 注入宿主 Config（config.json 单一来源）；
    ``api_acquire``/``api_release`` 注入「同时仅一个 API 请求」互斥槽
    （制作字幕期间占用，与实时语音识别同模式）；``video_dir`` 为资源
    面板文件对话框的默认目录；WA_DeleteOnClose——关闭即销毁（渲染
    线程已在 closeEvent 停止，不可复用旧实例），宿主经 ``destroyed``
    回调清空单例引用。
    """

    _sdl_event = pyqtSignal(str)  # SDL 全屏键盘事件（渲染线程 → 主线程队列化）
    _rebind_done = pyqtSignal(str, bool)  # rebind 完成回调（渲染线程 → 主线程）

    def __init__(
        self,
        *,
        config,
        api_acquire=None,
        api_release=None,
        video_dir: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("视频剪辑")
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        # 关闭即销毁：closeEvent 已停渲染线程/引擎，旧实例不可复用
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._config = config
        self._api_acquire = api_acquire
        self._api_release = api_release
        # 文件对话框 directory 参数要求 str（default_video_dir 返回 Path）
        self._video_dir = str(video_dir) if video_dir else ""

        # ---- 模型与命令流 ------------------------------------------------------
        self._model = TimelineModel(self)
        self._markers = ClipMarkerModel(self)
        self._undo_stack = UndoStack()
        self._view_state = ViewState(self)

        # ---- 渲染管理（单引擎双视图） ---------------------------------------------
        self._player = None  # pane.player()（构造 pane 后引用）
        self._renderer: SdlVideoRenderer | None = None
        self._fullscreen: FullscreenPlayer | None = None
        #: 统一全屏态：None / "sdl-pending" / "sdl" / "sdl-exit-pending" /
        #: "attach" / "attach-exit-pending"（pending 防重入，完成回调落定）
        self._fullscreen_kind: str | None = None
        #: 异步 rebind 的挂起参数（完成回调消费）
        self._pending_was_playing = False
        self._pending_fullscreen: FullscreenPlayer | None = None
        #: SDL 全屏路径的悬浮控制面板（随全屏创建/销毁）
        self._overlay: FullscreenOverlay | None = None
        #: 全屏渲染路径（"sdl"=SDL 独立窗口默认；"qt"=attach 保底回退；
        #: 非法值已由宿主配置层回退为 "qt"）
        self._fullscreen_mode = getattr(config, "fullscreen_mode", "qt")
        self._fps = 30.0

        #: 切片预览播放：到达终点自动暂停（蒙版点击「播放至终点」）
        self._preview_end_ms: int | None = None
        #: 最近一次导出大小预估（完成对比用；取消导出后残留无害）
        self._last_estimate: SizeEstimate | None = None

        #: 长按连发状态：当前按住的键与节拍定时器
        self._held_key: int | None = None
        self._repeat_timer = QTimer(self)
        self._repeat_timer.setSingleShot(False)
        self._repeat_timer.setInterval(REPEAT_INTERVAL_MS)
        self._repeat_timer.timeout.connect(self._fire_held_key)

        self._build_ui()
        self._connect_signals()

    # ---- UI 构建 ---------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top_split = QSplitter(Qt.Orientation.Horizontal)
        self._resource_panel = ResourcePanel(video_dir=self._video_dir)
        self._pane = PlayerPane()
        self._player = self._pane.player()
        self._log_panel = LogPanel()
        top_split.addWidget(self._resource_panel)
        top_split.addWidget(self._pane)
        top_split.addWidget(self._log_panel)
        top_split.setStretchFactor(0, 0)
        top_split.setStretchFactor(1, 1)
        top_split.setStretchFactor(2, 0)
        top_split.setSizes([240, 800, 320])
        # 防挤占：禁折叠 + 资源/日志窗最小宽锁定——打开大分辨率视频后
        # 播放窗不再扩张蚕食两侧面板（需求 5）
        top_split.setChildrenCollapsible(False)
        self._resource_panel.setMinimumWidth(200)
        self._log_panel.setMinimumWidth(240)
        root.addWidget(top_split, stretch=5)

        # 快捷按钮行（按钮文字附 Emacs 风格快捷键提示，与 keyPressEvent 路由一致）
        buttons = QHBoxLayout()
        self._buttons: dict[str, QPushButton] = {}
        for name, text, handler in (
            ("split", "分割 x", self._split_at_progress),
            ("delete", "删除 Del", self._delete_selected),
            ("join", "合并 c", self._join_all),
            ("undo", "退回 C-z", self._undo_once),
            ("mark", "标记切片 f", self._mark_clip),
            ("prev_frame", "上一帧 C-←", lambda: self._pane._seek_by(-frame_duration_ms(self._fps))),
            ("next_frame", "下一帧 C-→", lambda: self._pane._seek_by(frame_duration_ms(self._fps))),
            ("make_subtitles", "制作字幕", self._make_subtitles),
            ("load_subtitles", "加载字幕", self._load_subtitles),
            ("apply", "应用", self._apply_export),
        ):
            button = QPushButton(text)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 不夺焦点（空格可达窗口）
            button.clicked.connect(handler)
            self._buttons[name] = button
            buttons.addWidget(button)
        root.addLayout(buttons)

        # 时间轴 + 声波图
        self._timeline = TimelineView(self._view_state, self._model)
        self._timeline.setMinimumHeight(48)
        root.addWidget(self._timeline)
        self._waveform = WaveformView(self._view_state, self._model, self._markers)
        self._waveform.setMinimumHeight(130)
        root.addWidget(self._waveform)

        # 快捷键可靠性：全部子控件统一 NoFocus——任何控件不夺焦点，按键
        # 统一冒泡到主窗口 keyPressEvent 路由（实测：QListWidget/日志区
        # 获得焦点后会吞掉 c/x/f/space 等字符键与空格）
        for child in self.findChildren(QWidget):
            child.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 日志区例外：ClickFocus——点击可获得焦点（Ctrl+C 复制日志文本），
        # 初始焦点仍在主窗口（快捷键默认可用）；策略落于文本区（按钮独立一行）
        self._log_panel.set_text_focus_policy(Qt.FocusPolicy.ClickFocus)

        self.statusBar().showMessage("就绪")

    def _connect_signals(self) -> None:
        # 资源 → 播放
        self._resource_panel.load_requested.connect(self._load_video)
        # 渲染器生命周期
        self._pane.canvas_shown.connect(lambda: self._attach_renderer(self._pane.canvas().native_hwnd()))
        # 时间轴/声波图 → seek（复用 pane 节流链路；timeline 传序列时间）
        self._timeline.seek_requested.connect(self._seek_timeline)
        self._waveform.seek_requested.connect(self._seek_source)
        self._waveform.clip_play_requested.connect(self._play_clip_range)
        self._waveform.clip_move_committed.connect(self._on_clip_moved)
        self._waveform.selection_made.connect(lambda _: None)  # 门控状态即模型选中
        # 引擎 → 视图
        self._player.position_changed.connect(self._on_position_changed)
        self._player.media_opened.connect(self._on_media_opened)
        self._model.clips_changed.connect(self._on_clips_changed)
        # 全屏
        self._pane.fullscreen_requested.connect(self._enter_fullscreen)
        # SDL 全屏窗口键盘事件桥接（渲染线程 emit → 本队列投递）
        self._sdl_event.connect(self._dispatch_sdl_event)
        # rebind 完成回调桥接（渲染线程 emit → 主线程落全屏态/恢复播放）
        self._rebind_done.connect(self._dispatch_rebind_done)
        logger.info(
            "全屏模式：%s（config.fullscreen_mode；qt=attach 保底 / sdl=SDL 独立窗口）",
            self._fullscreen_mode,
        )

    # ---- 载入与同步 ---------------------------------------------------------------

    def _load_video(self, path: str) -> None:
        """载入视频：暂停引擎 → 时间轴模型重置 → 打开 → 波形后台提取。"""
        if self._fullscreen_kind is not None:
            # 悬挂全屏会让新视频渲染进不可见/全屏窗口，嵌入面板无渲染器
            # （实测：全屏态开新视频后双视图全黑）
            logger.info("载入新视频前先退出全屏态")
            self._exit_fullscreen()
        self._pane.pause_engine()
        self._markers.clear()
        self._undo_stack.clear()
        from .probe import probe_media_info

        info = probe_media_info(path)
        duration_ms = info.duration_ms if info else 0
        self._model.load_source(path, duration_ms)
        self._fps = info.fps if info else 30.0
        self._timeline.set_fps(self._fps)
        self._pane.open_path(path)
        self._worker = _WaveformWorker(path, self)
        self._worker.waveform_ready.connect(self._on_waveform_ready)
        self._worker.start()
        self._log_panel.append_info(
            f"已载入剪辑会话：{path}（时长 {duration_ms}ms，fps={self._fps:.3f}）",
            category="播放",
        )

    def _on_waveform_ready(self, wave) -> None:
        if wave is None:
            self._log_panel.append_warning(
                "声波图不可用（无音频轨或解码失败）", category="播放"
            )
            return
        self._waveform.set_waveform(wave)
        self._log_panel.append_info(
            f"声波图就绪（{wave.duration_ms}ms，块 {wave.block_ms}ms）",
            category="播放",
        )

    def _on_media_opened(self, info) -> None:
        self._fps = info.fps
        self._timeline.set_fps(info.fps)
        self._view_state.set_bounds(max(self._model.total_duration_ms(), info.duration_ms))

    def _on_clips_changed(self) -> None:
        self._view_state.set_bounds(self._model.total_duration_ms())

    def _on_position_changed(self, source_ms: int) -> None:
        """引擎源位置 → 双视图序列位置（落在片段外指针隐藏）。"""
        timeline_ms = self._model.source_to_timeline(source_ms)
        self._timeline.set_position(timeline_ms)
        self._waveform.set_position(timeline_ms)
        # 切片预览：到达终点自动暂停（蒙版点击「播放至终点」）
        end = self._preview_end_ms
        if end is not None and source_ms >= end:
            self._preview_end_ms = None
            self._player.pause()
            logger.info("切片预览播放结束（%dms）自动暂停", end)

    # ---- seek（节流统一走 pane 链路） -----------------------------------------------

    def _seek_timeline(self, timeline_ms: int) -> None:
        src = self._model.timeline_to_source(timeline_ms)
        if src is not None:
            self._pane._seek_to(src, "timeline")

    def _seek_source(self, source_ms: int) -> None:
        self._pane._seek_to(source_ms, "waveform")

    # ---- 渲染器管理（单引擎双视图） ---------------------------------------------------

    def _attach_renderer(self, hwnd: int) -> None:
        """画布句柄就绪后绑定（pane 显示/全屏进出共用出口）。

        渲染线程常驻：首次调用懒创建并启动，此后仅投 rebind 命令——
        SDL 窗口/渲染器/纹理的创建销毁全部在渲染线程内串行执行。
        """
        if self._renderer is None:
            self._player.ensure_sdl()
            renderer = SdlVideoRenderer(
                **self._player.renderer_wiring(),
                on_event=self._on_sdl_event,
                on_rebind_done=self._on_rebind_done_cb,
            )
            self._renderer = renderer
            self._player.set_renderer(renderer)
            renderer.start()
            logger.info("SDL 渲染线程已启动（常驻，等待画布绑定）")
        if not self._renderer.rebind_embed(hwnd):
            logger.error("SDL 渲染器绑定画布句柄 %d 失败", hwnd)

    def _on_rebind_done_cb(self, mode: str, ok: bool) -> None:
        """rebind 完成回调（渲染线程；emit 经队列投递主线程）。"""
        self._rebind_done.emit(mode, ok)

    def _dispatch_rebind_done(self, mode: str, ok: bool) -> None:
        """rebind 完成处理（主线程）：异步落全屏态/恢复播放/失败回滚。

        主线程零阻塞——rebind 投递即返回，全部后续动作（落 pending、
        恢复播放、失败回滚）都收敛在本回调，彻底消除同步死等窗口创建
        的互等（Win32 窗口操作要求各线程消息泵活跃）。
        """
        if mode == "fullscreen":
            if ok:
                self._fullscreen_kind = "sdl"
                if self._pending_was_playing:
                    self._player.play()
                self._launch_overlay()
                logger.info(
                    "进入全屏播放（SDL 独立窗口；Esc 退出 / 空格播放暂停 / ←→ 步进）"
                )
            else:
                self._fullscreen_kind = None
                logger.error("SDL 全屏窗口创建失败，投递嵌入画布回滚")
                self._renderer.rebind_embed(self._pane.canvas().native_hwnd())
            return
        # embed 完成：attach 全屏窗口延迟关闭 / sdl 退出落定
        pending = self._pending_fullscreen
        if pending is not None:
            self._pending_fullscreen = None
            pending.close()
            if self._fullscreen_kind == "attach-exit-pending":
                self._fullscreen_kind = None
            logger.info("attach 全屏窗口已关闭（嵌入画布已恢复，ok=%s）", ok)
        if self._fullscreen_kind == "sdl-exit-pending":
            self._fullscreen_kind = None
            if ok and self._pending_was_playing:
                self._player.play()
            logger.info("退出全屏：嵌入画布已恢复（ok=%s）", ok)

    def _launch_overlay(self) -> None:
        """SDL 全屏就绪后创建悬浮控制面板（定位用渲染线程回传的窗口几何）。"""
        if self._overlay is not None or self._renderer is None:
            return
        geo = self._renderer.fullscreen_geometry
        if not geo or geo[2] <= 0:
            logger.error("悬浮面板缺少 SDL 全屏窗口几何，跳过创建")
            return
        overlay = FullscreenOverlay(self._player)
        overlay.exit_requested.connect(self._exit_fullscreen)
        self._overlay = overlay
        overlay.launch_at(geo)

    def _on_sdl_event(self, kind: str) -> None:
        """SDL 全屏窗口键盘事件（渲染线程回调；emit 经队列投递主线程）。"""
        self._sdl_event.emit(kind)

    def _dispatch_sdl_event(self, kind: str) -> None:
        """桥接事件分发（主线程）：仅 sdl 全屏态生效。"""
        if kind in ("mouse_move", "mouse_down", "mouse_wheel"):
            # SDL 全屏窗口上的鼠标活动：面板重现 + 重置 3s 计时
            if self._overlay is not None:
                self._overlay.notify_activity()
            return
        if self._fullscreen_kind != "sdl":
            return  # 防御：非全屏态的残余事件
        if kind == "escape":
            self._exit_fullscreen()
        elif kind == "space":
            self._pane.toggle_play()
        elif kind == "left":
            self._pane._seek_by(-STEP_MS)
        elif kind == "right":
            self._pane._seek_by(STEP_MS)

    # ---- 全屏（双路径：sdl 独立窗口 / attach 保底；config.fullscreen_mode 切换） -------

    def _enter_fullscreen(self) -> None:
        if self._fullscreen_kind is not None:
            # 防重入必须带日志：静默 return 曾致"第二次全屏无任何痕迹"难排查
            logger.warning("已在全屏中（%s），忽略重复全屏请求", self._fullscreen_kind)
            return
        if not self._player.hasMedia():
            logger.info("未打开视频，忽略全屏请求")
            return
        if self._fullscreen_mode == "sdl":
            self._enter_fullscreen_sdl()
        else:
            self._enter_fullscreen_attach()

    def _enter_fullscreen_sdl(self) -> None:
        """SDL 独立顶层全屏窗口（异步两段式：投递即返回，完成回调落态）。

        投递时即置 pending 防重入；主线程零阻塞——同步死等窗口创建会
        与渲染线程互等（实测死等 3s 放大为 22s 互锁且状态撕裂）。
        """
        was_playing = self._player.playbackState() == PlaybackState.Playing
        if not was_playing:
            # 暂停态保持暂停；播放态不 pause→play 往返——往返会冻结/重启
            # 音频时钟（断供与时钟重跳诱因）且中断音频；rebind 期间
            # vqueue 有界自然限流解码，音频环水位足够覆盖切换耗时
            self._pane.pause_engine()
        if self._renderer is None:
            # 渲染线程未启动（pane 未显示过）：先建线程再切全屏
            self._attach_renderer(self._pane.canvas().native_hwnd())
        if not self._renderer.rebind_fullscreen():
            return  # 并发拒绝（日志已打；维持当前态）
        self._fullscreen_kind = "sdl-pending"
        self._pending_was_playing = was_playing
        logger.info("进入全屏：rebind 命令已投递，等待渲染线程完成")

    def _enter_fullscreen_attach(self) -> None:
        """attach 保底路径：Qt 顶层窗口 + 画布重绑（本机存在窗口不可见风险）。"""
        was_playing = self._player.playbackState() == PlaybackState.Playing
        if not was_playing:
            self._pane.pause_engine()  # 暂停态保持暂停；播放态不中断音频
        fullscreen = FullscreenPlayer(self._player)
        fullscreen.exit_requested.connect(self._exit_fullscreen)
        fullscreen.seek_requested.connect(
            lambda delta: self._pane._seek_by(delta)
        )
        self._fullscreen = fullscreen
        fullscreen.showFullScreen()  # Windows 下 Frameless+FullScreen 组合
        # 用 show() 可能不置前（实测未见全屏窗口）——显式全屏 + 置前激活
        fullscreen.raise_()
        fullscreen.activateWindow()
        self._attach_renderer(fullscreen.canvas_hwnd())
        if was_playing:
            self._player.play()
        fullscreen.sync_from_player()  # 状态继承显示
        fullscreen.start_ui_timer()
        self._fullscreen_kind = "attach"
        # 窗口层状态校验打点：500ms 后实锤"show 了但不可见/未激活"还是"从未 show"
        QTimer.singleShot(
            500,
            lambda: logger.info(
                "全屏窗口状态校验：visible=%s active=%s minimized=%s geometry=%s",
                fullscreen.isVisible(),
                fullscreen.isActiveWindow(),
                fullscreen.isMinimized(),
                fullscreen.geometry(),
            ),
        )
        logger.info("进入全屏播放（attach 模式；空格等快捷键仅作用于全屏窗口）")

    def _exit_fullscreen(self) -> None:
        if self._fullscreen_kind is None:
            return
        kind = self._fullscreen_kind
        was_playing = self._player.playbackState() == PlaybackState.Playing
        if not was_playing:
            self._pane.pause_engine()  # 暂停态保持暂停；播放态不中断音频
        # 全屏面板音量改动回同步嵌入音量条（引擎音量已实时更新，仅补 UI）
        self._pane.sync_volume_ui(self._player.volume())
        if kind == "sdl":
            if not self._renderer.rebind_embed(self._pane.canvas().native_hwnd()):
                return  # 并发拒绝（日志已打）
            self._fullscreen_kind = "sdl-exit-pending"
            self._pending_was_playing = was_playing
            if self._overlay is not None:
                self._overlay.shutdown()  # SDL 窗口销毁前面板先退，无残留
                self._overlay = None
            logger.info("退出全屏：嵌入画布重绑已投递")
            return
        if kind == "attach":
            fullscreen, self._fullscreen = self._fullscreen, None
            fullscreen.stop_ui_timer()
            # 延迟关闭：渲染器离开旧画布（embed 完成回调）后再 close，
            # 避免 present 到已销毁 HWND
            self._pending_fullscreen = fullscreen
            self._fullscreen_kind = "attach-exit-pending"
            self._attach_renderer(self._pane.canvas().native_hwnd())
            return
        logger.warning("全屏切换进行中（%s），忽略退出请求", kind)

    # ---- 编辑命令（快捷键与按钮共用出口） -----------------------------------------------

    def _split_at_progress(self) -> None:
        """分割（x）：进度标记为切割点；选中声波图段才生效；边界保护在模型层。"""
        clip = self._model.selected_clip()
        if clip is None:
            logger.info("分割无效：未选中声波图片段")
            return
        at = self._player.position()
        before = len(self._model.clips())
        cmd = SplitCommand(self._model, clip.clip_id, at)
        cmd.apply()
        if len(self._model.clips()) == before:
            logger.info("分割无效：进度标记为片段起始帧或末尾帧")
            return
        self._undo_stack.push(cmd)
        logger.info("已分割（%dms）→ %d 段", at, len(self._model.clips()))

    def _delete_selected(self) -> None:
        """删除（del）：删除选中段（音视频一体联动）并收紧。

        至少保留一个片段：全删后时间轴/声波图进入空状态，渲染与导出
        链路多处假设非空（实测窗口卡死），故入口拦截。
        """
        clip = self._model.selected_clip()
        if clip is None:
            logger.info("删除无效：未选中声波图片段")
            return
        if len(self._model.clips()) <= 1:
            logger.info("删除无效：至少保留一个片段")
            return
        cmd = DeleteCommand(self._model, clip.clip_id)
        cmd.apply()
        self._undo_stack.push(cmd)
        logger.info("已删除片段并收紧（剩余 %d 段）", len(self._model.clips()))

    def _join_all(self) -> None:
        """合并（c）：按当前顺序紧凑重排（消除拖拽间隙）。"""
        cmd = JoinCommand(self._model)  # 构造时快照 join 前状态（回退用）
        changed = self._model.join_all()
        if changed:
            self._undo_stack.push(cmd)
            logger.info("已合并：紧凑重排全部片段")
        else:
            logger.info("合并无效：当前无间隙")

    def _undo_once(self) -> None:
        cmd = self._undo_stack.undo()
        if cmd is None:
            logger.info("退回无效：命令流为空")
        else:
            logger.info("已退回：%s", cmd.name)

    def _on_clip_moved(self, clip_id: int, snapshot) -> None:
        """声波图拖拽完成：以拖拽前快照入撤销栈（拖拽已实时生效）。"""
        self._undo_stack.push(RestoreCommand(self._model, snapshot))
        logger.info("片段 %d 已移动（可退回）", clip_id)

    def _mark_clip(self) -> None:
        """标记切片（f）：进度标记为状态机输入。"""
        self._markers.mark(self._player.position())
        pending = self._markers.pending_start()
        if pending is not None:
            logger.info("切片起点已标记（%dms），再次按 f 标记终点", pending)
        else:
            logger.info("当前切片数：%d", len(self._markers.segments()))

    # ---- 切片蒙版播放 ---------------------------------------------------------------

    def _play_clip_range(self, src_start_ms: int, src_end_ms: int) -> None:
        """点击蒙版：跳到切片起点播放至终点自动暂停（多切片互不干扰）。"""
        self._preview_end_ms = src_end_ms
        self._player.setPosition(src_start_ms)
        self._player.play()
        logger.info("切片预览：%d → %dms（终点自动暂停）", src_start_ms, src_end_ms)

    # ---- 快捷键路由（长按连发） ---------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._fullscreen_kind in ("sdl", "sdl-pending"):
            # SDL 全屏窗口应持键盘焦点（事件经桥接分发）；若焦点仍留主
            # 窗口，此处拦截防"桥接 + 主窗口"双路由互相抵消
            event.accept()
            return
        key = event.key()
        if event.isAutoRepeat():  # 自管节拍：键盘自动重复一律过滤
            event.accept()
            return
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        handled = True
        if key == Qt.Key.Key_Space:
            self._pane.toggle_play()
        elif key == Qt.Key.Key_Left and not ctrl:
            self._begin_hold(key, lambda: self._pane._seek_by(-1_000))
        elif key == Qt.Key.Key_Right and not ctrl:
            self._begin_hold(key, lambda: self._pane._seek_by(+1_000))
        elif key == Qt.Key.Key_Left and ctrl:
            self._begin_hold(key, lambda: self._pane._seek_by(-frame_duration_ms(self._fps)))
        elif key == Qt.Key.Key_Right and ctrl:
            self._begin_hold(key, lambda: self._pane._seek_by(+frame_duration_ms(self._fps)))
        elif key == Qt.Key.Key_F and not ctrl:
            self._mark_clip()
        elif key == Qt.Key.Key_X and not ctrl:
            self._split_at_progress()
        elif key == Qt.Key.Key_Delete:
            self._delete_selected()
        elif key == Qt.Key.Key_C and not ctrl:
            self._join_all()  # Ctrl+C 放行给日志区复制（不拦截）
        elif key == Qt.Key.Key_Z and ctrl:
            self._begin_hold(key, self._undo_once)
        else:
            handled = False
        if handled:
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() == self._held_key and not event.isAutoRepeat():
            self._end_hold()
        super().keyReleaseEvent(event)

    def _begin_hold(self, key: int, action) -> None:
        """长按连发：按下立即触发一次；持续 2 秒后以 0.5 秒间隔连发。"""
        self._end_hold()
        self._held_key = key
        self._held_action = action
        action()
        QTimer.singleShot(REPEAT_DELAY_MS, self._start_repeat)

    def _start_repeat(self) -> None:
        if self._held_key is not None:
            self._repeat_timer.start()

    def _fire_held_key(self) -> None:
        if self._held_key is not None:
            self._held_action()

    def _end_hold(self) -> None:
        self._held_key = None
        self._repeat_timer.stop()

    # ---- 字幕制作/加载 ---------------------------------------------------------------

    def _subtitle_path_for(self, video_path: str) -> str:
        return str(Path(video_path).with_suffix(SUBTITLE_SUFFIX))

    def _make_subtitles(self) -> None:
        """制作字幕：二次确认 → 后台逐切片 ASR+翻译 → 双语 ASS 追加落盘。"""
        if getattr(self, "_subtitle_worker", None) is not None and (
            self._subtitle_worker.isRunning()
        ):
            self._subtitle_worker.stop()  # 制作中再次点击：停止制作
            self._log_panel.append_info(
                "正在停止字幕制作（保留已落盘内容）…", category="字幕"
            )
            return
        segments = self._markers.segments()
        if not segments:
            self._log_panel.append_info(
                "制作字幕无效：尚无切片标记（按 f 标记）", category="字幕"
            )
            return
        video_path = self._pane.current_video_path()
        if video_path is None:
            self._log_panel.append_info(
                "制作字幕无效：未打开视频", category="字幕"
            )
            return
        cfg = self._config
        # ASR 三键经回退链解析（空值跟随主链路）后与翻译主链路一并校验
        asr_base_url, asr_model, _asr_key = resolve_asr_options(cfg)
        if not (asr_base_url and asr_model and cfg.api_base_url and cfg.model):
            self._log_panel.append_warning(
                "ASR/翻译配置缺失（config.json 三键），无法制作字幕",
                category="字幕",
            )
            return
        subtitle_path = self._subtitle_path_for(video_path)
        sub_path = Path(subtitle_path)
        if sub_path.exists():  # 制作前删除同名字幕，无法删除则中止
            try:
                sub_path.unlink()
                self._log_panel.append_info(
                    f"已删除旧字幕文件：{subtitle_path}", category="字幕"
                )
            except OSError as exc:
                self._log_panel.append_error(
                    f"旧字幕删除失败，中止制作（{subtitle_path}）：{exc}",
                    category="字幕",
                )
                return
        answer = QMessageBox.question(
            self,
            "制作字幕",
            f"将逐切片执行 语音识别 + 翻译，共 {len(segments)} 个切片。\n"
            f"输出：{subtitle_path}\n制作中再次点击「制作字幕」可停止。是否继续？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log_panel.append_info("制作字幕已取消", category="字幕")
            return
        worker = _SubtitleWorker(
            segments,
            video_path,
            subtitle_path,
            self._config,
            api_acquire=self._api_acquire,
            api_release=self._api_release,
            parent=self,
        )
        worker.entry_progress.connect(
            lambda done, total, note: self._log_panel.append_progress(
                f"字幕进度：{done} / {total} {note}", category="字幕"
            )
        )
        worker.finished_ok.connect(
            lambda n: self._log_panel.append_success(
                f"字幕制作完成：共 {n} 条 → {subtitle_path}", category="字幕"
            )
        )
        worker.failed.connect(
            lambda msg: self._log_panel.append_error(
                f"字幕制作失败：{msg}", category="字幕"
            )
        )
        self._subtitle_worker = worker
        worker.start()
        self._log_panel.append_info(
            f"字幕制作已启动：{len(segments)} 个切片 → {subtitle_path}",
            category="字幕",
        )

    def _load_subtitles(self) -> None:
        """加载字幕：同名 ASS → 解析 Dialogue → 切片标记映射（先清空现有切片）。"""
        video_path = self._pane.current_video_path()
        if video_path is None:
            logger.info("加载字幕无效：未打开视频")
            return
        subtitle_path = self._subtitle_path_for(video_path)
        sub_path = Path(subtitle_path)
        if not sub_path.is_file():
            logger.info("未找到匹配字幕：%s", subtitle_path)
            return
        with open(subtitle_path, encoding="utf-8") as f:  # 映射结束即关句柄（可删旧文件）
            entries = parse_ass(f.read())
        if not entries:
            logger.warning("字幕解析为空：%s", subtitle_path)
            return
        self._markers.clear()  # 决策 5：加载即替换
        self._markers.replace_all(entries_to_segments(entries))
        logger.info("已加载字幕 %d 条 → 切片标记（%s）", len(entries), subtitle_path)

    # ---- 应用导出 ---------------------------------------------------------------

    def _apply_export(self) -> None:
        """应用：预估大小 → 策略弹窗（Radio 二选一）→ 后台导出 → 对比。"""
        clips = self._model.clips()
        if not clips:
            self._log_panel.append_info("应用无效：未载入视频", category="导出")
            return
        worker = getattr(self, "_export_worker", None)
        if worker is not None and worker.isRunning():
            worker.stop()  # 导出中再次点击：中断导出（半成品自动清理）
            self._log_panel.append_info(
                "正在中断导出（半成品文件将清理）…", category="导出"
            )
            return
        out_path = default_export_path(clips[0].source_path)
        # 导出前预估（需求 1/4）：失败仅告警不阻塞
        estimate, reason = estimate_export_size(clips)
        self._last_estimate = estimate
        total_duration_s = sum(c.duration_ms for c in clips) / 1000.0
        if estimate is not None:
            est_mb = estimate.bytes_estimate / 1_048_576
            self._log_panel.append_info(
                f"预计导出文件大小：{est_mb:.2f} MB"
                f"（{estimate.method}；片段总时长 {total_duration_s:.1f}s）",
                category="导出",
            )
            estimate_line = f"预计导出文件大小：{est_mb:.2f} MB"
        else:
            self._log_panel.append_warning(
                f"导出大小预估不可用：{reason}", category="导出"
            )
            estimate_line = "大小预估不可用"
        dialog = _ExportDialog(
            len(clips), total_duration_s, str(out_path), estimate_line, self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._log_panel.append_info("应用已取消", category="导出")
            return
        mode, crf = dialog.choice()
        if mode == "snap":
            self._log_panel.append_info(
                "导出策略：关键帧吸附（边界吸附 ≤半个 GOP，零重编码损耗）",
                category="导出",
            )
        else:
            self._log_panel.append_info(
                f"导出策略：重编码（帧精确，CRF={crf}）", category="导出"
            )
        worker = _ExportWorker(
            clips, str(out_path), mode=mode, crf=crf, parent=self
        )
        worker.progress_logged.connect(
            lambda line: self._log_panel.append_progress(line, category="导出")
        )
        worker.finished_ok.connect(self._on_export_done)
        worker.failed.connect(
            lambda msg: self._log_panel.append_error(
                f"导出失败：{msg}", category="导出"
            )
        )
        worker.aborted.connect(
            lambda: self._log_panel.append_info(
                "导出已中断（半成品已清理）", category="导出"
            )
        )
        worker.finished.connect(lambda: setattr(self, "_export_worker", None))
        self._export_worker = worker
        worker.start()
        self._log_panel.append_info(
            f"导出已启动：{len(clips)} 个片段 → {out_path}", category="导出"
        )

    def _on_export_done(self, out_path: str) -> None:
        """导出完成：实际大小 + 与预估对比误差（需求 3）；清空命令流。"""
        try:
            actual_bytes = Path(out_path).stat().st_size
            actual_mb = actual_bytes / 1_048_576
        except OSError as exc:
            logger.error("导出完成但读取文件大小失败（%s）：%s", out_path, exc)
            actual_bytes = None
            actual_mb = None
        estimate = self._last_estimate
        self._last_estimate = None
        if actual_mb is not None and estimate is not None and estimate.bytes_estimate > 0:
            est_mb = estimate.bytes_estimate / 1_048_576
            error_pct = (actual_bytes - estimate.bytes_estimate) / estimate.bytes_estimate * 100
            # 码率法按源平均码率外推：片段实际内容码率与源均值差异大时误差
            # 可能超半（低运动片段重编码码率远低于源平均），提示仅供参考
            hint = (
                "（源码率与片段实际内容差异较大，估算仅供参考）"
                if abs(error_pct) > 50
                else ""
            )
            self._log_panel.append_success(
                f"导出完成：实际大小 {actual_mb:.2f} MB，预计大小 {est_mb:.2f} MB，"
                f"误差 {error_pct:.1f}%（{estimate.method}）{hint} → {out_path}",
                category="导出",
            )
        elif actual_mb is not None:
            self._log_panel.append_success(
                f"导出完成：实际大小 {actual_mb:.2f} MB → {out_path}",
                category="导出",
            )
        else:
            self._log_panel.append_success(
                f"导出完成 → {out_path}", category="导出"
            )
        self._undo_stack.clear()  # 二次确认后一律清空命令流（不检查是否剪辑过）

    # ---- 生命周期 ---------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._pane.pause_engine()
        if self._overlay is not None:
            self._overlay.shutdown()
            self._overlay = None
        if self._pending_fullscreen is not None:
            self._pending_fullscreen.close()
            self._pending_fullscreen = None
        if self._fullscreen is not None:
            self._fullscreen.stop_ui_timer()
            self._fullscreen.close()
        if self._renderer is not None:
            self._renderer.stop()  # 渲染线程尾部自清理 SDL 对象后退出
        super().closeEvent(event)
