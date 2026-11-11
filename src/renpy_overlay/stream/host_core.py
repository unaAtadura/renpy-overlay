"""流式悬浮窗的核心 Mixin：装配、生命周期、队列消费与显示出口。

宿主依赖：本模块定义唯一的 ``__init__``（全部共享状态的初始化点），
其余 Mixin 经 ``self`` 访问这些状态；``_drain`` 是唯一跨线程汇聚点
（``queue.Queue`` + 80ms QTimer 主线程消费），其 23 个 ``elif`` 分支
原样保留，各 ``_handle_xxx`` 方法分散在各链路 Mixin 中。
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from .. import config, translation_store
from ..quick_menu import QuickMenu
from ..screenshot import (
    AIChatWindow,
    ChatHistoryWindow,
    HotkeyMode,
    MouseLockController,
    RemoteAutomationController,
    RemoteScreenshotController,
    ScreenshotHistoryWindow,
    open_chat_store,
    open_store,
)
from ..song_recognition import SongHistoryWindow
from ..song_recognition.store import open_store as open_song_store
from ..translation_cache import TranslationCache, choice_translation_text
from ..translation_history_window import TranslationHistoryWindow
from ..tray_icon import ManagedItem, TrayIconController
from ..video import (
    AsrRtController,
    VideoRecorder,
    open_asr_store,
    resolve_asr_rt_options,
)
from .choice_log import ChoiceEffectLog
from .constants import (
    CLICK_DELAY_MS,
    DRAIN_INTERVAL_MS,
    FOLLOW_INTERVAL_MS,
    RECORD_TICK_INTERVAL_MS,
)
from .geometry import DOCK_CHOICES
from .visual import TITLE_GLOW_FACTOR
from .windows_style2 import resolve_window_classes

logger = logging.getLogger("renpy_overlay.stream_window")


class StreamOverlayCore:
    """流式悬浮窗门面核心：管理标题 + 正文双窗口，暴露队列化的对外接口。"""

    def __init__(
        self,
        target_pid: int,
        dock: str = "top-center",
        width: int = config.DEFAULT_STREAM_WINDOW_WIDTH,  # 正文窗尺寸来自 config.json
        height: int = config.DEFAULT_STREAM_WINDOW_HEIGHT,
        app_config: config.AppConfig | None = None,
        game_dir: str = "",
        skip_injection: bool = False,
        on_quit=None,
    ):
        self.target_pid = target_pid
        # 跳过注入模式（需求 future_跳过注入.txt）：无游戏进程可定位/无文本捕获，
        # 以主屏为虚拟游戏窗口置顶摆放；单击/自动翻译不可用（入口禁用）
        self.skip_injection = bool(skip_injection)
        self.dock = dock if dock in DOCK_CHOICES else "top-center"
        self.width = width
        self.height = height
        self.on_quit = on_quit
        self._config = app_config or config.AppConfig()
        self._title_gap = max(0, self._config.stream_window_title_gap)

        # QApplication 必须先于任何 QWidget；进程此前可能已创建（复用之）
        self._app = QApplication.instance() or QApplication([])
        # 程序退出只由宿主 on_quit（快捷菜单/控制台 q/游戏退出）驱动：悬浮窗
        # 各窗均为 Tool（不计入 lastWindowClosed），而历史/弹窗等常规窗口是
        # 最后一个可见窗口时，默认 quitOnLastWindowClosed 会连带退出整个工具
        # （实测：预构建弹窗点 X 后程序整体退出）
        self._app.setQuitOnLastWindowClosed(False)
        # 窗口样式经配置切换（translate_window_style：1=既有透明双窗；
        # 2=半透明黑色新样式）；两类构造签名与属性面完全兼容，后续
        # 装配/跟随/交互/显示出口均不感知样式差异
        title_cls, body_cls = resolve_window_classes(self._config.translate_window_style)
        self._body_window = body_cls(
            self._config.stream_window_font_size,
            self._config.stream_window_line_spacing,
            host=self,
            fill_color=self._config.body_fill_color,
        )
        self._title_window = title_cls(
            self._config.stream_window_title_font_size,
            host=self,
            fill_color=self._config.body_fill_color,
        )
        self._body_window.resize(width, height)
        self._title_window.resize(width, 10)  # 真实高度随字体计算后由跟随循环设定
        self._body_window.move(100, 100)
        self._title_window.move(100, 100)

        # 标题窗物理高度：字体盒 + 上下余量（TITLE_GLOW_FACTOR，保持既有几何
        # 外观），按窗口 DPR 从逻辑像素换算
        title_h_logical = self._title_window.fm.height() + TITLE_GLOW_FACTOR * (
            self._config.stream_window_title_font_size
        )
        self._title_h = max(12, int(math.ceil(title_h_logical * self._body_window.dpr())))
        self._title_window.resize(width, self._title_h)

        self._body_hwnd = self._body_window.hwnd
        self._title_hwnd = self._title_window.hwnd
        # 注入相关入口（预构建翻译缓存）内嵌在标题窗文本右侧，注入确认后经
        # _handle_injected 注入；预构建主窗口单例（惰性创建，关闭即清引用）
        self._game_dir = game_dir or ""
        self._injected = False
        self._prebuild_window = None
        self._settings_window = None
        logger.debug(
            "流式悬浮窗 HWND：正文=0x%X 标题=0x%X（标题高=%dpx 间距=%dpx）",
            self._body_hwnd,
            self._title_hwnd,
            self._title_h,
            self._title_gap,
        )

        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._target_hwnd = 0
        self._visible = False  # 首次定位成功后才显示，避免旧位置闪烁
        self._locked = False
        self._dragging = False
        self._drag_grab = (0, 0)
        self._drag_size = (width, height)
        self._drag_origin = (0, 0)
        self._user_offset: tuple[int, int] | None = None
        self._last_topmost_log = 0.0
        self._last_minimize_log = 0.0  # 最小化隐藏节流日志（follow.py）
        self._press_pos: tuple[int, int] | None = None
        self._click_suppress_until = 0.0
        self._title_text = ""
        self._last_say: dict | None = None
        self._translating = False
        # 听歌识曲：与翻译互斥的进行中标志；stop 让录制/识别尽快结束
        self._recognizing = False
        self._song_stop: threading.Event | None = None
        self._record_deadline = 0.0
        self._translation_seq = 0
        self._stream_seq = 0  # 正文窗正在流式呈现的翻译世代号（0 = 非翻译内容）
        self._current_stop: threading.Event | None = None
        self._translation_cache = TranslationCache(
            max_bytes=self._config.translation_cache_size_kb * 1024
        )
        self._translation_store = translation_store.open_store(game_dir or None)
        # 截图翻译：独立数据库 + 快捷菜单（窗口池），历史窗口单例惰性创建
        self._screenshot_store = open_store(game_dir or None)
        # 听歌识曲：成功识别结果独立落库（game_song.db，与 screenshot.db 同目录）
        self._song_store = open_song_store(game_dir or None)
        # AI 对话：用户消息与回复成对落库（chat.db，与 screenshot.db 同目录）
        self._chat_store = open_chat_store(game_dir or None)
        # 语音识别：成功识别的音频片段与文本落库（asr.db，与其它库同目录）
        self._asr_store = open_asr_store(game_dir or None)
        # 选项副作用日志：纯逻辑累积（choice 事件旁路，独立于正文窗与翻译
        # 链路）；弹窗视图由标题窗入口图标按需开合，关闭即清空累积
        self._choice_log = ChoiceEffectLog()
        self._choice_log_window = None
        # AI 对话（OCR/发送）与翻译/识曲共用「同时只允许一个 API 调用」互斥：
        # 对话侧经 _acquire/_release_chat_slot 占用，三个既有入口同样检查它
        self._chat_busy = False
        self._chat_window: AIChatWindow | None = None
        self._quick_menu = QuickMenu(
            on_capture_click=self.request_screenshot_translation,
            on_open_history=self._open_history,
            on_recognize_song=self.request_song_recognition,
            on_open_song_history=self._open_song_history,
            on_open_chat=self._open_chat,
            on_open_chat_history=self._open_chat_history,
            on_open_translation_history=self._open_translation_history,
            on_quit=self.request_close,
            on_record_video=self._toggle_video_record,
            on_open_video_clip=self._open_video_clip,
            on_open_asr_history=self._open_asr_history,
            on_open_prebuild=self.request_prebuild,
            on_open_settings=self._open_settings,
        )
        # 快捷键模式：全局热键主开关 + 8 窗口键，联动控制模块的布局锁定；
        # 提示走标题窗唯一出口，触发翻译直接复用截图翻译入口（含既有提示规则）
        self._hotkey_mode = HotkeyMode(
            controller=self._quick_menu,
            notify=self._update_title,
            trigger_translation=self.request_screenshot_translation,
            main_hotkey=self._config.hotkey_main,
            window_hotkeys=self._config.hotkey_windows,
        )
        self._quick_menu.attach_hotkey_mode(self._hotkey_mode)
        # 遥控截图：快捷键模式期间存在的 A/B 触发/瞄准小圆窗（与主开关相互
        # 独立）；A 单击经 B 的装填状态复用截图翻译入口（含在途互斥与提示）
        self._remote_screenshot = RemoteScreenshotController(
            enable=self._config.remote_screenshot_enable,
            diameter_a=self._config.remote_screenshot_diameter_a,
            diameter_b=self._config.remote_screenshot_diameter_b,
            locked_windows_provider=self._quick_menu.locked_windows,
            trigger=self.request_screenshot_translation,
        )
        self._hotkey_mode.attach_remote_screenshot(self._remote_screenshot)
        # 遥控器自动化：周期截图 + 感知哈希变化检测 + 缓存命中（装填状态复用
        # 遥控截图控制器）；提交走视觉链路但不隐藏窗口、不落 screenshot.db
        self._remote_automation = RemoteAutomationController(
            interval=self._config.remote_screenshot_interval,
            cache_size=self._config.remote_cache_size_count,
            has_windows=lambda: self._remote_screenshot.windows_created,
            is_busy=self._is_translation_busy,
            display=self._display_remote_auto_text,
            submit=self._submit_remote_auto_translation,
            notify=self._update_title,
            suppress_frame=self._quick_menu.set_frame_suppressed,
            get_windows_hidden=lambda: self._remote_screenshot.user_hidden,
            set_windows_hidden=self._remote_screenshot.set_user_hidden,
        )
        self._remote_screenshot.attach_automation(self._remote_automation)
        self._quick_menu.attach_remote_automation(self._remote_automation)
        # 锁鼠标区域：ClipCursor 限制 + 逃脱全局热键（保底退出）；窗口由
        # 控制器自持，不进截图窗口池，与布局锁定三组接口互不影响
        self._mouse_lock = MouseLockController(
            notify=self._update_title,
            escape_hotkey=self._config.hotkey_mouse_escape,
        )
        self._quick_menu.attach_mouse_lock(self._mouse_lock)
        # 视频录制：ffmpeg gdigrab + 环回录音双流（自备 ffmpeg，缺失时降级提示）；
        # 菜单「录制视频」开关式入口，提示走标题窗唯一出口
        self._video_recorder = VideoRecorder(
            notify=self._update_title,
            game_dir=game_dir,
        )
        self._quick_menu.attach_recorder(self._video_recorder)
        # 实时语音识别：开关式后台功能（AsrRtController 自持状态）；开启占用
        # 对话互斥槽阻塞其余 API 请求（is_busy 合并预构建在途，P5 决议），
        # 识别事件经队列回主线程上屏，实时结果不入 asr.db（需求）
        self._asr_rt = AsrRtController(
            submit=lambda kind, payload: self._queue.put((kind, payload)),
            notify=self._update_title,
            is_busy=lambda: self._is_translation_busy()
            or (self._prebuild_window is not None and self._prebuild_window.is_running),
            acquire=self._acquire_chat_slot,
            release=self._release_chat_slot,
            options=lambda: resolve_asr_rt_options(self._config),
            on_state=self._on_asr_rt_state,
        )
        self._quick_menu.attach_asr_rt(self._asr_rt)
        # 实时识别显示状态：已定稿句列表 + 当前句全文（同参考实现，
        # 每次事件全量重算显示文本后差分上屏）
        self._asr_rt_finals: list[str] = []
        self._asr_rt_current = ""
        self._history_window: ScreenshotHistoryWindow | None = None
        self._song_history_window: SongHistoryWindow | None = None
        self._asr_history_window = None  # 语音识别历史窗口单例（惰性创建）
        self._video_clip_window = None  # 视频剪辑窗口单例（关闭即销毁，destroyed 清引用）
        self._chat_history_window: ChatHistoryWindow | None = None
        self._translation_history_window: TranslationHistoryWindow | None = None
        self._screenshot_suppress = False  # 截图瞬间抑制跟随循环重新显示
        self._context_menu_open = False  # 右键菜单打开期间暂停周期性置顶重申
        self._last_translation_input: str | None = None
        self._last_cache_hit_original: str | None = None  # 最近缓存命中原文（定位入口用）
        self._lock_started_at = 0.0
        self._auto_skip_reason: str | None = None
        # Menu 合并译文呈现（show_original_text=false）：提问译文与选项译文
        # 整合为一段整替上屏。提问经独立通道解析（两级缓存优先，未命中独立
        # 线程，独立世代号作废迟到结果）；选项译文目标（编号拼接，兼作缓存
        # 键）与既有翻译链路完全不变（见 translate_flow._present_menu_translation）
        self._menu_prompt_pending_input: str | None = None  # choice 登记的提问原文
        self._menu_prompt_input: str | None = None          # 已进入合并流程（""=无提问菜单）
        self._menu_prompt_state: str = "idle"               # idle / pending / ready / failed
        self._menu_prompt_translated: str | None = None     # 提问译文
        self._menu_prompt_seq: int = 0                      # 世代号：作废在途线程的迟到结果
        self._menu_prompt_stop: threading.Event | None = None
        self._menu_choice_translated: str | None = None     # 已就绪待合并的选项译文
        # 运行时翻译模式（仅内存状态）：初值取自 config.json 的 auto_translate，
        # 标题窗「切换翻译模式」入口可随时切换，不回写配置文件
        self._auto_translate_on = self._config.auto_translate

        self._drain_timer = QTimer()
        self._drain_timer.setInterval(DRAIN_INTERVAL_MS)
        self._drain_timer.timeout.connect(self._drain)
        self._follow_timer = QTimer()
        self._follow_timer.setInterval(FOLLOW_INTERVAL_MS)
        self._follow_timer.timeout.connect(self._follow)
        self._click_timer = QTimer()
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(CLICK_DELAY_MS)
        self._click_timer.timeout.connect(self._on_delayed_click)
        self._auto_timer = QTimer()
        self._auto_timer.timeout.connect(self._auto_translate_tick)
        self._record_timer = QTimer()
        self._record_timer.setInterval(RECORD_TICK_INTERVAL_MS)
        self._record_timer.timeout.connect(self._record_tick)
        # 系统托盘（需求 future_托盘图标.txt）：全部悬浮窗一键隐藏/恢复的第二
        # 入口；退出复用 request_close（与快捷菜单同一函数引用，单一退出路径）；
        # 托盘不可用/构造失败时降级为无（hidden 恒 False），不影响主程序
        self._tray = TrayIconController(
            items_provider=self._tray_managed_items, on_quit=self.request_close
        )
        self._update_title("正在启动…")

    # ------------------------------------------------------------ 对外接口

    def set_status(self, text: str) -> None:
        self._queue.put(("status", text))

    def push_say(self, who: str, what: str, source: str = "", ts: float | None = None) -> None:
        self._queue.put(("say", {"who": who, "what": what, "src": source, "ts": ts or time.time()}))

    def push_choice(
        self,
        items: list[str],
        who: str = "",
        what: str = "",
        effects: list | None = None,
    ) -> None:
        """分支选项出现（跨线程安全，经队列回主线程展示）。"""
        self._queue.put(
            ("choice", {"items": list(items), "who": who, "what": what, "effects": effects})
        )

    def push_choice_pick(self, index: int, caption: str) -> None:
        """玩家做出选择（跨线程安全，经队列回主线程展示）。"""
        self._queue.put(("choice_pick", {"index": index, "caption": caption}))

    def hint(self, text: str) -> None:
        self._queue.put(("hint", text))

    def toggle_visible(self) -> None:
        self._queue.put(("cmd", "toggle"))

    def reset_position(self) -> None:
        """恢复"自动停靠"（清除手动拖动锁定的位置）。"""
        self._queue.put(("cmd", "dock"))

    def request_close(self) -> None:
        self._queue.put(("cmd", "quit"))

    def _open_settings(self) -> None:
        """快捷菜单「设置」：打开/唤回设置窗口（单例，关闭即清引用）。"""
        if self._closed:
            return
        from ..settings_window import SettingsWindow  # 局部导入：仅入口时加载

        window = self._settings_window
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
            return
        self._settings_window = SettingsWindow(
            on_reload=self.reload_config,
            on_closed=lambda: setattr(self, "_settings_window", None),
        )
        self._settings_window.show()
        logger.info("设置窗口已打开")

    def reload_config(self) -> str | None:
        """重新加载 config.json 并热应用到运行时（设置窗口「确定」触发）。

        返回 None=成功；错误字符串=失败原因（设置窗口提示；已写盘的
        新配置不受影响，运行时整体回退旧配置）。热应用范围：

        - API 参数 / 截图压缩比 / 识曲时长等现取键：引用替换即生效；
        - 流式文字填充色（body_fill_color）：纯绘制参数，重绘即生效；
        - 自动翻译轮询间隔：重设定时器（开关是运行时状态，保留用户
          当前选择不覆盖，下次启动读新值）；
        - 全局热键组 / 锁鼠标逃脱键：重建控制器并重注册；

        需重启键（双窗样式与尺寸、对话窗尺寸、fullscreen_mode、内存缓存
        容量、遥控截图参数）由设置窗口提示，不做热切换（风险点：运行中
        单例持有字体/几何，双层控制器重建风险高）。
        """
        try:
            new_cfg = config.load_config()
        except Exception as exc:  # 读取异常：运行时保持现状
            logger.warning("重载配置失败（读取）：%s", exc)
            return f"读取配置失败：{exc}"
        old_cfg = self._config
        self._config = new_cfg
        errors = self._apply_runtime_config(new_cfg)
        if errors:
            # 任一组件重建失败：整体回退旧配置（旧键已注销，用旧值重建即恢复）
            self._config = old_cfg
            fallback_errors = self._apply_runtime_config(old_cfg)
            if fallback_errors:
                logger.error("配置回退未完全成功：%s", "；".join(fallback_errors))
            message = "；".join(errors)
            logger.warning("配置热重载失败，已回退原配置：%s", message)
            return f"{message}（已回退原配置）"
        logger.info("配置已热重载并生效（需重启键由设置窗口提示）")
        return None

    def _apply_runtime_config(self, cfg: config.AppConfig) -> list[str]:
        """把 ``cfg`` 热应用到运行时组件（热键/鼠标锁重建 + 定时器间隔）。"""
        errors: list[str] = []
        try:
            self._body_window.set_fill_color(cfg.body_fill_color)
            self._title_window.set_fill_color(cfg.body_fill_color)
        except Exception as exc:  # pragma: no cover - 纯绘制参数异常极罕见
            errors.append(f"文字填充色应用失败：{exc}")
        try:
            self._auto_timer.setInterval(self._auto_interval_ms())
            if self._auto_translate_on:
                self._auto_timer.start()
        except Exception as exc:  # pragma: no cover - 定时器异常极罕见
            errors.append(f"自动翻译间隔应用失败：{exc}")
        try:
            self._hotkey_mode.shutdown()
            self._hotkey_mode = HotkeyMode(
                controller=self._quick_menu,
                notify=self._update_title,
                trigger_translation=self.request_screenshot_translation,
                main_hotkey=cfg.hotkey_main,
                window_hotkeys=cfg.hotkey_windows,
            )
            self._quick_menu.attach_hotkey_mode(self._hotkey_mode)
            self._hotkey_mode.attach_remote_screenshot(self._remote_screenshot)
        except Exception as exc:
            logger.exception("热键组重注册失败")
            errors.append(f"热键重注册失败：{exc}")
        try:
            self._mouse_lock.shutdown()
            self._mouse_lock = MouseLockController(
                notify=self._update_title,
                escape_hotkey=cfg.hotkey_mouse_escape,
            )
            self._quick_menu.attach_mouse_lock(self._mouse_lock)
        except Exception as exc:
            logger.exception("锁鼠标逃脱键重注册失败")
            errors.append(f"锁鼠标热键重注册失败：{exc}")
        return errors

    @property
    def hwnd(self) -> int:
        return self._body_hwnd

    @property
    def visible(self) -> bool:
        return self._visible

    # ------------------------------------------------------------ 主循环

    def run(self) -> None:
        """启动 Qt 事件循环（阻塞）。返回即代表窗口已销毁。"""
        self._drain_timer.start()
        self._follow_timer.start()
        self._start_auto_translate()
        try:
            self._app.exec()
        finally:
            self._closed = True
            logger.debug("流式悬浮窗已销毁")

    def close(self) -> None:
        """从任意线程请求关闭（清理兜底路径）。"""
        try:
            self._app.quit()
        except Exception:  # pragma: no cover - 已销毁
            pass

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tray.shutdown()  # 先移除托盘图标，避免退出过程中用户再点托盘
        self._translation_seq += 1  # 在途翻译的结果作废
        if self._translating:
            self._abort_translation("窗口关闭")
        stop = self._menu_prompt_stop
        if stop is not None:
            stop.set()  # Menu 提问独立翻译线程尽快结束（daemon，结果已作废）
        self._abort_song_recognition("窗口关闭")
        self._cancel_pending_click()
        for timer in (
            self._drain_timer,
            self._follow_timer,
            self._click_timer,
            self._auto_timer,
            self._record_timer,
        ):
            try:
                timer.stop()
            except Exception:  # pragma: no cover
                pass
        if self._translation_store is not None:
            self._translation_store.close()
            self._translation_store = None
        if self._screenshot_store is not None:
            self._screenshot_store.close()
            self._screenshot_store = None
        if self._song_store is not None:
            self._song_store.close()
            self._song_store = None
        if self._chat_store is not None:
            self._chat_store.close()
            self._chat_store = None
        if self._asr_store is not None:
            self._asr_store.close()
            self._asr_store = None
        self._hotkey_mode.shutdown()
        self._remote_screenshot.shutdown()  # 遥控截图 A/B 随退出销毁（幂等）
        self._mouse_lock.shutdown()  # 必须先于退出解除 ClipCursor，否则鼠标被永久限制
        self._video_recorder.shutdown()  # 录制中则同步停止并完成合并收尾
        self._asr_rt.shutdown()  # 实时识别开启则静默停止并释放互斥槽
        # 视频剪辑窗口：close()（非 deleteLater）触发 closeEvent——停播放
        # 引擎/SDL 渲染线程/卸载日志面板；渲染线程未停会阻塞进程退出
        if self._video_clip_window is not None:
            try:
                self._video_clip_window.close()
            except Exception:  # pragma: no cover
                pass
            self._video_clip_window = None
        if self._prebuild_window is not None:
            try:
                self._prebuild_window.close()  # closeEvent 内限时收束在途任务
            except Exception:  # pragma: no cover
                pass
            self._prebuild_window = None
        if self._settings_window is not None:
            try:
                self._settings_window.close()
            except Exception:  # pragma: no cover
                pass
            self._settings_window = None
        if self._choice_log_window is not None:
            try:
                self._choice_log_window.close()
            except Exception:  # pragma: no cover
                pass
            self._choice_log_window = None
        self._quick_menu.close_all()
        if self._history_window is not None:
            try:
                self._history_window.deleteLater()
            except Exception:  # pragma: no cover
                pass
            self._history_window = None
        if self._song_history_window is not None:
            try:
                self._song_history_window.deleteLater()
            except Exception:  # pragma: no cover
                pass
            self._song_history_window = None
        if self._chat_history_window is not None:
            try:
                self._chat_history_window.deleteLater()
            except Exception:  # pragma: no cover
                pass
            self._chat_history_window = None
        if self._translation_history_window is not None:
            try:
                self._translation_history_window.deleteLater()
            except Exception:  # pragma: no cover
                pass
            self._translation_history_window = None
        if self._chat_window is not None:
            try:
                self._chat_window.deleteLater()
            except Exception:  # pragma: no cover
                pass
            self._chat_window = None
        for window in (self._title_window, self._body_window):
            try:
                window.close()
            except Exception:  # pragma: no cover
                pass
        try:
            self._app.quit()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 托盘显隐

    def _tray_managed_items(self) -> list[ManagedItem]:
        """托盘受管窗口清单（每次现取；凡隐藏前可见的窗口全部参与快照）。

        条目三态：流式双窗合一条目（必须走 ``_set_visible`` 维护
        ``_visible`` 标志，否则截图翻译的 was_visible 判定与跟随循环状态
        失真）、框选提示窗组（恢复序列含自动化压制帧重放）、其余真实窗口
        逐个直连 QWidget 原语。单例窗口未创建（None）时跳过。
        """
        item = ManagedItem
        items = [
            item(
                lambda: self._visible,
                lambda: self._set_visible(False),
                lambda: self._set_visible(True),
            )
        ]
        items.extend(
            item(w.isVisible, w.hide, w.show)
            for w in self._quick_menu.screenshot_windows()
        )
        items.append(
            item(
                self._quick_menu.frame_overlay_shown,
                self._quick_menu.hide_frame_overlay,
                self._quick_menu.show_frame_overlay,
            )
        )
        items.extend(
            item(w.isVisible, w.hide, w.show)
            for w in self._remote_screenshot.managed_windows()
        )
        items.extend(
            item(w.isVisible, w.hide, w.show)
            for w in self._mouse_lock.managed_windows()
        )
        for window in (
            self._chat_window,
            self._history_window,
            self._song_history_window,
            self._chat_history_window,
            self._translation_history_window,
            self._asr_history_window,
            self._video_clip_window,
            self._prebuild_window,
            self._settings_window,
            self._choice_log_window,
        ):
            if window is not None:
                items.append(item(window.isVisible, window.hide, window.show))
        return items

    # ------------------------------------------------------------ 队列消费

    def _drain(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "say":
                    self._handle_say(payload)
                elif kind == "choice":
                    self._handle_choice(payload)
                elif kind == "choice_pick":
                    self._handle_choice_pick(payload)
                elif kind == "hint":
                    # 正文只保留最新一条内容：提示语同样整段替换显示
                    self._display_text(payload)
                elif kind == "status":
                    self._update_title(payload)
                elif kind == "chunk":
                    self._handle_chunk(payload)
                elif kind == "translation":
                    self._handle_translation(payload)
                elif kind == "menu_prompt_result":
                    self._handle_menu_prompt_result(payload)
                elif kind == "screenshot_done":
                    self._handle_screenshot_done(payload)
                elif kind == "screenshot_fail":
                    self._handle_screenshot_fail(payload)
                elif kind == "remote_auto_done":
                    self._handle_remote_auto_done(payload)
                elif kind == "song_recognizing":
                    self._handle_song_recognizing(payload)
                elif kind == "song_done":
                    self._handle_song_done(payload)
                elif kind == "song_fail":
                    self._handle_song_fail(payload)
                elif kind == "asr_rt_delta":
                    self._handle_asr_rt_delta(payload)
                elif kind == "asr_rt_completed":
                    self._handle_asr_rt_completed(payload)
                elif kind == "asr_rt_error":
                    self._handle_asr_rt_error(payload)
                elif kind == "cmd":
                    self._handle_command(payload)
                elif kind == "injected":
                    self._handle_injected()
        except queue.Empty:
            pass
        except Exception:  # pragma: no cover - UI 异常不应终止循环
            logger.exception("处理界面消息时出错")

    def _handle_command(self, command: str) -> None:
        if command == "toggle":
            self._set_visible(not self._visible)
        elif command == "show":
            self._set_visible(True)
        elif command == "hide":
            self._set_visible(False)
        elif command == "dock":
            self._reset_to_dock()
        elif command == "quit":
            if callable(self.on_quit):
                try:
                    self.on_quit()
                except Exception:  # pragma: no cover
                    logger.exception("退出回调失败")
            self._shutdown()

    def _handle_say(self, payload: dict) -> None:
        # 新对话到达：作废未完成的 Menu 合并（迟到的提问翻译结果不再整替
        # 正文，逐条译文的既有呈现随即恢复常态）
        if (
            self._menu_prompt_input is not None
            or self._menu_prompt_pending_input is not None
        ):
            self._menu_prompt_pending_input = None
            self._menu_prompt_input = None
            self._menu_prompt_state = "idle"
            self._menu_prompt_translated = None
            self._menu_choice_translated = None
            self._menu_prompt_seq += 1
        who = str(payload.get("who") or "")
        what = str(payload.get("what") or "")
        self._last_say = {"who": who, "what": what}
        preview = what.strip()[:20]
        if preview and not self._asr_rt.enabled:
            # 标题提醒：这条对话就是翻译时将发送的文本（不受显示开关影响）；
            # 实时识别开启期间抑制预览（P3）：标题保持「实时语音识别中…」提示
            self._update_title(preview)
        if self._config.show_original_text:
            self._display_text((who + "\n" if who.strip() else "") + what + "\n")
            logger.debug("显示对话：[%s] %s", who or "-", what[:60])
        else:
            # 正文区不刷新，但原文已记录，翻译/自动翻译链路不受影响
            # 诊断：上报延迟 = 工具端处理时刻 - 代理发布时刻（ts 同一时钟源），
            # 用于定量区分「捕获晚 / 传输晚 / 触发晚」；异常或越界时降级为未知
            try:
                delay_ms = (time.time() - float(payload.get("ts"))) * 1000
                delay_text = (
                    f"；上报延迟={delay_ms:.1f}ms"
                    if 0 <= delay_ms < 10 * 60 * 1000
                    else "；上报延迟=未知"
                )
            except (TypeError, ValueError):
                delay_text = "；上报延迟=未知"
            logger.debug(
                "已捕获对话（show_original_text=false，正文区不更新）：[%s] %s%s",
                who or "-",
                what[:60],
                delay_text,
            )
        # 事件驱动触发：新捕获的对话到达即评估自动翻译（不再等下一个轮询 tick，
        # 缩短「台词显示→译文上屏」的感知延迟；条件判断与轮询版共用）
        self._auto_translate_check(immediate=True)

    def _display_text(self, text: str) -> None:
        """用打字机方式整段替换正文内容（窗口只保留最新一条，不累积历史）。

        实时语音识别开启期间抑制改写（P3 决议）：识别文本是正文唯一写者，
        对话原文/提示/选项/自动化缓存命中等非 API 内容不得覆盖实时识别画面。
        """
        if self._asr_rt.enabled:
            logger.debug("实时语音识别开启期间抑制正文改写")
            return
        self._stream_seq = 0
        self._body_window.begin_stream()
        self._body_window.feed_text(text)

    def _handle_choice(self, payload: dict) -> None:
        """分支选项出现：标题显示选项预览，翻译目标切换为选项文本本身。

        正文呈现（两种模式译文侧一致）：提问由注入侧解析紧邻 Say/caption
        得到（choice 事件的 what），先无条件登记进合并链路——与选项译文
        整合为一段一次性整替（打字机呈现，见 translate_flow）；``show_original_text=true``
        时额外先显示「提问原文 + 编号选项列表」的原文合并段，译文就绪后
        被合并译文整段替换；false 时正文在译文就绪前保持上一条内容。
        无前置提问时 what 为空、仅显示选项列表；选项翻译目标（编号拼接，
        兼作缓存键）与去重重置照常；选择后的「→ 已选择：…」提示不受影响。
        """
        items = [
            str(item).strip()
            for item in (payload.get("items") or [])
            if str(item).strip()
        ]
        who = str(payload.get("who") or "").strip()
        what = str(payload.get("what") or "").strip()
        choice_text = choice_translation_text(items)
        if choice_text:
            self._last_say = {"who": "", "what": choice_text}
            # 允许自动翻译重新评估该选项文本：同一菜单重放/载入时缓存命中
            # 直接上屏，首次出现则发起流式翻译（下个轮询间隔内生效）
            self._last_translation_input = None
        # 两种模式统一登记该菜单提问（原文，空串=无提问菜单）：译文合并
        # 共用同一套链路（解析/抑制/整合/呈现），show_original_text 仅决定
        # 是否先显示一段原文合并
        self._menu_prompt_pending_input = what
        self._menu_prompt_seq += 1  # 新菜单：作废在途提问线程的迟到结果
        self._menu_prompt_input = None
        self._menu_prompt_state = "idle"
        self._menu_prompt_translated = None
        self._menu_choice_translated = None
        if self._config.show_original_text:
            lines = ([what] if what else []) + [
                f"{number}. {item}" for number, item in enumerate(items, 1)
            ]
            if any(line.strip() for line in lines):
                self._display_text("\n".join(lines) + "\n")
        if not self._asr_rt.enabled:  # 实时识别开启期间抑制标题预览（P3）
            if items:
                self._update_title("选项：" + " / ".join(items))
            else:
                self._update_title("出现选项")
        if not self._config.show_original_text:
            logger.debug(
                "已捕获分支选项（show_original_text=false，正文区不更新）："
                "%s",
                " / ".join(items) or "<空>",
            )
        effects = payload.get("effects")
        if not isinstance(effects, list):
            effects_desc = "None（agent 端采集降级）"
        else:
            counts = [len(block) if isinstance(block, list) else -1 for block in effects]
            effects_desc = f"{len(effects)} 项，语句数 {counts}"
        logger.info(
            "出现分支选项（%d 项）：%s；提问/上下文：[%s] %s；翻译目标=%d 字；effects=%s",
            len(items),
            " / ".join(items) or "<空>",
            who or "-",
            what[:60],
            len(choice_text),
            effects_desc,
        )
        # 选项副作用日志（纯旁路）：独立累积，不进正文窗、不进翻译缓存与
        # 去重；effects=None（agent 端采集降级）时条目仅记录选项文本
        appended = self._choice_log.append_menu(
            items,
            effects=effects,
            who=who,
            what=what,
            ts=payload.get("ts"),
        )
        if not appended:
            logger.debug(
                "副作用日志：未新增条目（同指纹时间窗合并或选项为空）"
            )
        if self._choice_log_window is not None:
            self._choice_log_window.refresh()

    def _handle_choice_pick(self, payload: dict) -> None:
        """玩家做出选择：正文追加结果提示行，标题同步更新（不清屏）。"""
        try:
            index = int(payload.get("index", -1))
        except (TypeError, ValueError):
            index = -1
        caption = str(payload.get("caption") or "").strip()
        label = f"{index}. {caption}" if index >= 0 and caption else (caption or "未知选项")
        if not self._asr_rt.enabled:  # 实时识别开启期间抑制正文追加与标题预览（P3）
            self._body_window.feed_text(f"→ 已选择：{label}\n")
            self._update_title(f"已选择：{label}")
        logger.info(
            "玩家做出选择：%s；预测翻转=%s",
            label,
            "成功" if self._choice_log.mark_picked(index, caption) else "未命中（停留预测态）",
        )
        if self._choice_log_window is not None:
            self._choice_log_window.refresh()

    def _handle_chunk(self, payload: dict) -> None:
        seq = payload.get("seq")
        if seq != self._translation_seq:
            logger.debug("翻译增量已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        if self._menu_prompt_input is not None:
            # Menu 合并期间增量不入正文：选项译文随 _handle_translation 与
            # 提问译文整合后一次性整替上屏
            return
        if self._config.translate_window_style == 2:
            # style=2 整段汇总上屏（已确认决议）：增量不入正文，整条译文
            # 收完后由 _handle_translation 整段替换显示（翻译期间正文
            # 保持上一条内容不变）
            return
        if self._stream_seq != seq:  # 该轮译文的第一个增量：清屏开新流
            self._stream_seq = seq
            self._body_window.begin_stream()
        self._body_window.feed_text(str(payload.get("text") or ""))

    def _update_title(self, text: str) -> None:
        """标题窗状态文案的唯一出口：随交互状态动态更新，变化时写入日志。"""
        if self._title_text == text:
            return
        self._title_text = text
        self._title_window.set_text(text)
        logger.debug("标题窗状态更新：%s", text)

    def _set_visible(self, visible: bool) -> None:
        try:
            if visible:
                self._title_window.show()
                self._body_window.show()
            else:
                self._title_window.hide()
                self._body_window.hide()
        except Exception:  # pragma: no cover
            return
        self._visible = visible
        logger.debug("流式悬浮窗可见性：%s", visible)
