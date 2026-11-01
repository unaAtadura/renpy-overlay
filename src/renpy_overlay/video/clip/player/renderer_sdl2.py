"""SDL2 视频渲染器：NV12/IYUV 流式纹理直传 + 低延迟信箱上屏。

渲染管线核心：

- 双窗口模式（渲染线程常驻）：嵌入模式经 ``SDL_CreateWindowFrom(控件
  winId)`` 把渲染器挂到 Qt 画布；全屏模式由 SDL 自建独立顶层窗口
  （desktop 全屏，不改显示模式）。模式切换经 rebind 命令在渲染线程
  内串行完成（teardown → create → 最后一帧重上屏），画面无黑屏；
  命令异步投递，完成经回调（Qt 信号桥接）通知主线程落态——主线程
  零阻塞，消息泵恒活跃。
- 窗口/渲染器/纹理/视口的创建与销毁全部限定在渲染线程——该线程即
  SDL 窗口的创建线程，结构性杜绝跨线程调用 SDL 窗口 API；主线程仅
  投递 rebind 命令并同步等待结果。
- 零转换直传：解码 worker 把帧统一为 yuv420p（本场景素材原生即该格
  式，一次转换都不做），渲染线程以 IYUV 流式纹理按平面更新——跳过
  rgb24 转格式 + QImage 深拷贝的整条开销链。
- 低延迟信箱：暂停态帧（打开首帧/seek 目标帧）经信箱立即上屏；播放
  态按音频主时钟节拍消费预解码队列，到期即传即 present，无中间排队。
  陈帧（seek 前代数号）直接丢弃不占上屏路径。
- 事件桥接：渲染循环内 PumpEvents/PollEvent（窗口消息队列归创建线
  程），全屏态键盘事件（Esc/空格/←→）经 on_event 回调跨线程投递主
  线程分发；SDL 渲染器的视口重算不依赖事件监听——present 前主动比
  对窗口尺寸并重设逻辑尺寸。
- 上屏回报：``on_presented(frame)``（移植适配：传整帧供门面缓存
  payload，供暂停放大帧按需转 QImage）。

线程：SDL 渲染器在渲染线程创建并全程独占；纹理更新/present/窗口
操作全部限定在渲染线程；stop 后线程尾部自清理 SDL 对象后退出。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

import sdl2

from .sync_clock import pace_wait_ms, should_drop, wall_clock_ms

logger = logging.getLogger("renpy_overlay.video.clip.player.renderer_sdl2")

#: EOF 哨兵帧的 pts 约定（decoder 约定俗成）
EOF_PTS = -1
#: 渲染循环空转轮询间隔（秒）
POLL_INTERVAL_S = 0.05
#: FPS 平滑系数（EMA）
FPS_EMA_ALPHA = 0.15
#: 单帧节拍等待上限（秒）：时钟停滞时强制弃帧逃逸，防渲染线程挂起
PACE_MAX_WAIT_S = 5.0
#: 渲染循环存活心跳间隔（毫秒）：冻结定位的关键证据
RENDER_HEARTBEAT_MS = 5_000

#: 全屏键盘事件 → 桥接 kind（渲染线程泵出；on_event 回调消费）
_SDL_KEY_MAP = {
    sdl2.SDLK_ESCAPE: "escape",
    sdl2.SDLK_SPACE: "space",
    sdl2.SDLK_LEFT: "left",
    sdl2.SDLK_RIGHT: "right",
}

#: SDL 鼠标事件 → 桥接 kind（悬浮面板活动检测；低频，不节流点击/滚轮）
_SDL_MOUSE_KINDS = {
    sdl2.SDL_MOUSEMOTION: "mouse_move",
    sdl2.SDL_MOUSEBUTTONDOWN: "mouse_down",
    sdl2.SDL_MOUSEWHEEL: "mouse_wheel",
}
#: mouse_move 转发节流（毫秒）：移动事件高频，防跨线程信号风暴
MOUSE_FWD_THROTTLE_MS = 200


class SdlVideoRenderer(threading.Thread):
    """SDL 渲染线程：常驻 + rebind 双窗口 + 信箱帧/节拍队列消费。"""

    def __init__(
        self,
        *,
        vqueue,
        clock_provider,
        state_provider,
        gen_provider,
        on_presented,
        on_ended,
        stats=None,
        clock_ms=None,
        on_event=None,
        on_rebind_done=None,
    ) -> None:
        super().__init__(name="video-cut-render", daemon=True)
        self._vqueue = vqueue
        self._clock_provider = clock_provider
        self._state_provider = state_provider
        self._gen_provider = gen_provider
        self._on_presented = on_presented
        self._on_ended = on_ended
        self._stats = stats
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._on_event = on_event  # SDL 键盘事件桥接（渲染线程回调）
        self._on_rebind_done = on_rebind_done  # rebind 完成回调（渲染线程）
        self._window = None
        self._renderer = None
        self._texture = None
        self._tex_w = 0
        self._tex_h = 0
        self._hwnd = 0  # 嵌入模式的外部窗口句柄（诊断日志用）
        self._win_size = (0, 0)  # 窗口客户区缓存（视口同步比对）
        self._last_frame = None  # 线程私有：最近上屏帧（rebind 重上屏用）
        self._mailbox = None  # 暂停态立即上屏帧（VideoFrame）
        self._stop_flag = False
        self._cond = threading.Condition()
        self._cmd = None  # rebind 命令信箱 (mode, hwnd)
        self._cmd_cond = threading.Condition()
        self._is_fullscreen = False  # 当前窗口类型（心跳展示用）
        self._last_heartbeat_ms = 0.0
        self._last_mouse_fwd_ms = 0.0  # mouse_move 转发节流基准
        self._fullscreen_geo = (0, 0, 0, 0)  # 全屏窗口几何快照（面板定位）
        self._last_present_wall = 0.0
        self._wall_base_pts = 0
        self._wall_base_wall = 0.0

    # ---- rebind 命令（异步投递；完成经 on_rebind_done 回调通知主线程） --------

    def rebind_embed(self, hwnd: int) -> bool:
        """投递嵌入画布绑定命令（异步；完成回调 on_rebind_done("embed", ok)）。"""
        return self._submit_rebind("embed", int(hwnd))

    def rebind_fullscreen(self) -> bool:
        """投递 SDL 独立顶层 desktop 全屏窗口切换（异步，同上）。"""
        return self._submit_rebind("fullscreen", 0)

    def _submit_rebind(self, mode: str, hwnd: int) -> bool:
        """投递命令后立即返回（主线程绝不阻塞等待）。

        Win32 窗口操作期间各线程消息泵必须保持活跃：同步死等会与渲染
        线程的窗口创建互等（实测死等 3s 不泵消息放大为 22s 互锁，且超
        时后渲染/UI 状态撕裂）。完成结果经 on_rebind_done 回调异步落态。
        """
        with self._cmd_cond:
            if self._cmd is not None:
                logger.error("上一个 rebind 命令未完成，拒绝并发请求（%s）", mode)
                return False
            self._cmd = (mode, hwnd)
            self._cmd_cond.notify_all()
        logger.info("rebind 命令已投递（mode=%s hwnd=%d）", mode, hwnd)
        return True

    # ---- 渲染线程内：命令执行与窗口生命周期 ------------------------------------

    def _take_command(self) -> bool:
        """消费 rebind 命令（渲染线程内串行执行；返回 True 表示已处理）。"""
        with self._cmd_cond:
            cmd, self._cmd = self._cmd, None
        if cmd is None:
            return False
        mode, hwnd = cmd
        started = time.monotonic()
        ok = False
        try:
            self._teardown_window()
            logger.info(
                "rebind teardown 完成（耗时 %.0fms）",
                (time.monotonic() - started) * 1000.0,
            )
            if mode == "fullscreen":
                ok = self._create_fullscreen_window()
            else:
                ok = self._create_embed_window(hwnd)
            if ok and self._last_frame is not None:
                # 最后一帧立即重上屏：模式切换瞬间画面无缝（<1 帧空窗）
                self._configure_texture(self._last_frame)
                self._upload_and_present(self._last_frame.payload)
                logger.info(
                    "rebind 后重上屏最后一帧（pts=%dms）", self._last_frame.pts_ms
                )
        except Exception:
            logger.error("rebind 执行异常（mode=%s）", mode, exc_info=True)
            ok = False
        logger.info(
            "rebind 执行完成（mode=%s 总耗时=%.0fms ok=%s）",
            mode, (time.monotonic() - started) * 1000.0, ok,
        )
        if self._on_rebind_done is not None:
            # 完成回调（渲染线程执行；实现侧 emit Qt 信号队列投递主线程）
            self._on_rebind_done(mode, ok)
        return True

    def _set_render_hints(self) -> None:
        """direct3d9 后端：windowed present 自动拉伸 backbuffer 到客户
        区——窗口尺寸变化（嵌入布局/全屏切换）无需重建交换链（d3d11
        的 flip 模型要求尺寸严格一致，否则只能覆盖左上或逐帧重建）。"""
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"linear")
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_DRIVER, b"direct3d")

    def _create_embed_window(self, hwnd: int) -> bool:
        """从 Qt 控件原生句柄创建 SDL foreign 窗口（渲染线程内）。"""
        self._set_render_hints()
        self._hwnd = int(hwnd)
        self._window = sdl2.SDL_CreateWindowFrom(ctypes.c_void_p(self._hwnd))
        if not self._window:
            logger.error("SDL_CreateWindowFrom 失败：%s", sdl2.SDL_GetError().decode())
            return False
        if not self._create_renderer():
            return False
        self._is_fullscreen = False
        logger.info("SDL 渲染器已挂载到 Qt 画布（渲染线程内，句柄 %d）", self._hwnd)
        return True

    def _create_fullscreen_window(self) -> bool:
        """创建 SDL 独立顶层 desktop 全屏窗口（无边框，不改显示模式；
        绝不使用独占 SDL_WINDOW_FULLSCREEN——DWM 下与合成器冲突）。

        创建后校验 SHOWN|FULLSCREEN 标志：可见性异常立即失败回滚，
        防止"渲染正常但窗口从未映射到屏幕"的静默故障（attach 路径
        实测教训）。
        """
        self._set_render_hints()
        self._window = sdl2.SDL_CreateWindow(
            b"fullscreen-playback",
            sdl2.SDL_WINDOWPOS_CENTERED_DISPLAY(0),
            sdl2.SDL_WINDOWPOS_CENTERED_DISPLAY(0),
            1280, 720,  # 全屏模式下被 SDL 忽略（自动取显示器尺寸）
            sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP,
        )
        if not self._window:
            logger.error(
                "SDL 全屏窗口创建失败：%s", sdl2.SDL_GetError().decode(errors="replace")
            )
            return False
        flags = sdl2.SDL_GetWindowFlags(self._window)
        need = sdl2.SDL_WINDOW_SHOWN | sdl2.SDL_WINDOW_FULLSCREEN
        if (flags & need) != need:
            logger.error(
                "SDL 全屏窗口可见性校验失败：flags=0x%x（缺 SHOWN/FULLSCREEN）", flags
            )
            self._teardown_window()
            return False
        if not self._create_renderer():
            return False
        self._is_fullscreen = True
        # 置前 + 输入焦点：窗口在渲染线程创建时，用户点击的前台授权时
        # 间窗已过，CreateWindow 的 FULLSCREEN 标志不保证激活——不显式
        # 激活则需手动点击窗口才能操作（实测）
        sdl2.SDL_RaiseWindow(self._window)
        focus_ok = sdl2.SDL_SetWindowInputFocus(self._window) == 0
        # 窗口几何快照（渲染线程内查询；主线程经属性读 int 元组供面板定位）
        gx = ctypes.c_int()
        gy = ctypes.c_int()
        gw = ctypes.c_int()
        gh = ctypes.c_int()
        sdl2.SDL_GetWindowPosition(self._window, ctypes.byref(gx), ctypes.byref(gy))
        sdl2.SDL_GetWindowSize(self._window, ctypes.byref(gw), ctypes.byref(gh))
        self._fullscreen_geo = (gx.value, gy.value, gw.value, gh.value)
        flags = sdl2.SDL_GetWindowFlags(self._window)
        logger.info(
            "SDL 全屏窗口已创建（desktop 全屏）：geo=%s flags=0x%x SHOWN=%s "
            "FULLSCREEN=%s INPUT_FOCUS=%s（SetInputFocus=%s）",
            self._fullscreen_geo,
            flags,
            bool(flags & sdl2.SDL_WINDOW_SHOWN),
            bool(flags & sdl2.SDL_WINDOW_FULLSCREEN),
            bool(flags & sdl2.SDL_WINDOW_INPUT_FOCUS),
            "ok" if focus_ok else "failed",
        )
        return True

    @property
    def fullscreen_geometry(self) -> tuple[int, int, int, int]:
        """SDL 全屏窗口几何快照（渲染线程写入；主线程读 int 元组原子）。"""
        return self._fullscreen_geo

    def _create_renderer(self) -> bool:
        self._renderer = sdl2.SDL_CreateRenderer(
            self._window, -1, sdl2.SDL_RENDERER_ACCELERATED
        )
        if not self._renderer:
            logger.warning("加速渲染器创建失败，回退 software：%s",
                           sdl2.SDL_GetError().decode())
            self._renderer = sdl2.SDL_CreateRenderer(self._window, -1, 0)
        if not self._renderer:
            logger.error("SDL_CreateRenderer 失败：%s", sdl2.SDL_GetError().decode())
            return False
        sdl2.SDL_SetRenderDrawColor(self._renderer, 0, 0, 0, 255)
        info = sdl2.SDL_RendererInfo()
        driver = "unknown"
        if sdl2.SDL_GetRendererInfo(self._renderer, info) == 0:
            driver = info.name.decode(errors="replace")
        logger.info("SDL 渲染器已创建（driver=%s）", driver)
        return True

    def _teardown_window(self) -> None:
        """销毁纹理/渲染器/SDL 窗口包装（渲染线程内）。

        foreign 窗口（CreateWindowFrom）标记 SDL_WINDOW_FOREIGN：
        SDL_DestroyWindow 仅移除 windowdata prop、恢复原 WndProc、释放
        DC——不销毁 Qt 拥有的 HWND 本体；自建全屏窗口则正常销毁。
        """
        if self._texture:
            sdl2.SDL_DestroyTexture(self._texture)
            self._texture = None
        if self._renderer:
            sdl2.SDL_DestroyRenderer(self._renderer)
            self._renderer = None
        if self._window:
            sdl2.SDL_DestroyWindow(self._window)
            self._window = None
        self._win_size = (0, 0)

    # ---- 信箱/控制 -------------------------------------------------------------

    def present_now(self, frame) -> None:
        """信箱投递：暂停态帧立即上屏（worker 线程调用，线程安全）。"""
        with self._cond:
            self._mailbox = frame
            self._cond.notify_all()

    def set_wall_base(self, pts_ms: int) -> None:
        """墙钟回退基准（play 时由门面设置）。"""
        self._wall_base_pts = pts_ms
        self._wall_base_wall = self._clock_ms()

    def stop(self) -> None:
        self._stop_flag = True
        with self._cond:
            self._cond.notify_all()

    # ---- 渲染线程主体 -----------------------------------------------------------

    def run(self) -> None:  # pragma: no cover - SDL 链路人工联测
        try:
            self._render_loop()
        except Exception:
            logger.warning("渲染线程异常退出", exc_info=True)

    def _render_loop(self) -> None:
        self._ensure_video_init()
        while not self._stop_flag:
            self._heartbeat_maybe()
            self._pump_events()
            if self._take_command():
                continue
            if self._renderer is None:
                # 窗口未绑定：信箱/队列帧原地保留（防丢打开首帧），仅轮询
                time.sleep(POLL_INTERVAL_S)
                continue
            if self._take_mailbox():
                continue
            # 暂停/停止态不消费队列（预填充帧保留到 play；信箱帧除外）——
            # 若在此处 get 后因 state 跳过，会与 worker 形成双高速空转
            if self._state_provider() != PlaybackStateName.PLAYING:
                time.sleep(POLL_INTERVAL_S)
                continue
            frame = self._vqueue.get(timeout_s=POLL_INTERVAL_S)
            if frame is None:
                continue
            if frame.pts_ms == EOF_PTS:
                self._on_ended()
                continue
            if frame.gen != self._gen_provider():
                self._count("stale")
                continue  # seek 前的陈帧：直接丢弃
            self._pace_and_present(frame)
        self._teardown_window()

    def _ensure_video_init(self) -> None:
        """VIDEO 子系统初始化（渲染线程内；SDL_Init 幂等可重复调用）。

        窗口/事件泵/渲染全部在本线程，video 子系统按线程归属初始化，
        消除"主线程 Init + 渲染线程使用"的内部状态跨线程风险（实测：
        音频设备停摆与渲染冻结并存的疑似内部锁问题）。AUDIO 由引擎在
        主线程独立初始化（SDL_Init 按子系统互不影响）。
        """
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            logger.error(
                "SDL_Init(VIDEO) 失败：%s", sdl2.SDL_GetError().decode(errors="replace")
            )

    def _heartbeat_maybe(self) -> None:
        """渲染循环存活心跳（5s INFO）：冻结时区分"循环挂起"与"循环慢"。

        ``heartbeat`` 标记（logging extra，不进格式化文本）供日志面板
        界面过滤：仅完整调试模式显示；级别与文件日志内容不变。
        """
        now = self._clock_ms()
        if now - self._last_heartbeat_ms < RENDER_HEARTBEAT_MS:
            return
        self._last_heartbeat_ms = now
        mode = "none" if self._window is None else (
            "fullscreen" if self._is_fullscreen else "embed"
        )
        presented = self._stats.presented if self._stats is not None else -1
        logger.info(
            "渲染循环心跳：window=%s presented=%d cmd=%s",
            mode, presented, "busy" if self._cmd is not None else "idle",
            extra={"heartbeat": True},
        )

    def _pump_events(self) -> None:
        """SDL 事件泵（窗口创建线程内调用合法）：全屏键盘事件桥接。

        窗口消息队列按 CreateWindow 的调用线程归属，本线程即创建线程；
        嵌入态 foreign 窗口无 SDL 键盘焦点，泵空转无副作用。
        """
        sdl2.SDL_PumpEvents()
        if self._on_event is None:
            return
        ev = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ctypes.byref(ev)):
            if ev.type == sdl2.SDL_KEYDOWN:
                if ev.key.repeat:
                    continue
                kind = _SDL_KEY_MAP.get(int(ev.key.keysym.sym))
                if kind is not None:
                    logger.info("SDL 全屏事件：%s", kind)
                    self._on_event(kind)
                continue
            mouse_kind = _SDL_MOUSE_KINDS.get(ev.type)
            if mouse_kind is None:
                continue
            now = self._clock_ms()
            if (
                mouse_kind == "mouse_move"
                and now - self._last_mouse_fwd_ms < MOUSE_FWD_THROTTLE_MS
            ):
                continue  # 移动事件高频：节流转发，点击/滚轮低频直转
            self._last_mouse_fwd_ms = now
            self._on_event(mouse_kind)

    def _take_mailbox(self) -> bool:
        with self._cond:
            frame, self._mailbox = self._mailbox, None
        if frame is None:
            return False
        if frame.pts_ms == EOF_PTS:
            return True
        self._configure_texture(frame)
        self._upload_and_present(frame.payload)
        self._last_frame = frame
        self._on_presented(frame)
        return True

    def _pace_and_present(self, frame) -> None:
        """主时钟节拍等待 → 到期直传上屏；落后超阈值丢帧追平。

        内层等待三重防护（实测教训）：
        - 每轮泵事件：暂停会冻结音频时钟，使本帧 wait 恒为小正数——
          不泵事件则 space/rebind 全部积压到超时逃逸（实测空格恢复与
          全屏切换各延迟 5s）；
        - 暂停打断：state 离开 PLAYING 立即上屏本帧后返回（时钟冻结
          后等待永不到期，继续等无意义）；
        - 最大等待上限：时钟异常停滞时强制弃帧逃逸。
        """
        entered = self._clock_ms()
        while not self._stop_flag:
            self._pump_events()  # 等待期也泵事件：键盘/鼠标不被积压
            if frame.gen != self._gen_provider():
                self._count("stale")
                return
            if self._state_provider() != PlaybackStateName.PLAYING:
                # 暂停打断节拍等待：本帧恰为应显示的下一帧，直接上屏
                self._configure_texture(frame)
                self._upload_and_present(frame.payload)
                self._last_frame = frame
                self._on_presented(frame)
                return
            wait = pace_wait_ms(frame.pts_ms, int(self._current_clock()))
            if wait <= 0:
                break
            if self._clock_ms() - entered > PACE_MAX_WAIT_S * 1000.0:
                self._count("dropped")
                logger.warning(
                    "节拍等待超时弃帧（pts=%dms 等待>%.0fs；疑似音频时钟停滞）",
                    frame.pts_ms, PACE_MAX_WAIT_S,
                )
                return
            time.sleep(min(wait, 50) / 1000.0)
        if self._stop_flag:
            return
        clock = int(self._current_clock())
        if should_drop(frame.pts_ms, clock):
            self._count("dropped")
            return
        self._configure_texture(frame)
        self._upload_and_present(frame.payload)
        self._last_frame = frame
        self._on_presented(frame)

    def _current_clock(self) -> float:
        """主时钟：音频优先，None（无音轨/基准失效）回退墙钟推算。"""
        ms = self._clock_provider()
        if ms is not None:
            return ms
        return wall_clock_ms(self._wall_base_pts, self._wall_base_wall, self._clock_ms())

    # ---- SDL 纹理操作（渲染线程内） ---------------------------------------------

    def _configure_texture(self, frame) -> None:
        """按帧尺寸惰性创建纹理（尺寸变化才重建；逻辑尺寸保比例缩放）。"""
        payload = frame.payload
        w = int(getattr(payload, "width", 0))
        h = int(getattr(payload, "height", 0))
        if w <= 0 or h <= 0 or self._renderer is None:
            return
        if self._texture is not None and (w, h) == (self._tex_w, self._tex_h):
            return
        if self._texture is not None:
            sdl2.SDL_DestroyTexture(self._texture)
        self._texture = sdl2.SDL_CreateTexture(
            self._renderer,
            sdl2.SDL_PIXELFORMAT_IYUV,
            sdl2.SDL_TEXTUREACCESS_STREAMING,
            w, h,
        )
        self._tex_w, self._tex_h = w, h
        sdl2.SDL_RenderSetLogicalSize(self._renderer, w, h)
        logger.info("SDL 纹理已创建：%dx%d（IYUV 流式）", w, h)

    def _sync_viewport_if_resized(self) -> None:
        """窗口尺寸变化时重设逻辑尺寸。

        本项目无主线程 SDL 事件泵，渲染器的窗口事件监听（自动重算
        logical viewport）不会触发——present 前主动比对窗口尺寸，覆盖
        嵌入布局 resize 与嵌入/全屏切换两种尺寸变化。
        """
        if self._window is None or self._renderer is None or self._tex_w <= 0:
            return
        w = ctypes.c_int()
        h = ctypes.c_int()
        sdl2.SDL_GetWindowSize(self._window, ctypes.byref(w), ctypes.byref(h))
        if (w.value, h.value) != self._win_size:
            self._win_size = (w.value, h.value)
            sdl2.SDL_RenderSetLogicalSize(self._renderer, self._tex_w, self._tex_h)
            logger.info(
                "viewport 同步：窗口 %dx%d（纹理 %dx%d）",
                w.value, h.value, self._tex_w, self._tex_h,
            )

    def _upload_and_present(self, payload) -> None:
        """yuv420p 三平面直传纹理 → present（渲染线程内执行）。"""
        if self._texture is None or self._renderer is None:
            return
        self._sync_viewport_if_resized()
        planes = payload.planes
        rect = sdl2.SDL_Rect(0, 0, self._tex_w, self._tex_h)
        # pysdl2 绑定的平面参数类型为 POINTER(c_ubyte)（非 c_void_p）：
        # av Plane 的 buffer_ptr 经 cast 转换后传入
        def plane_ptr(plane):
            return ctypes.cast(
                ctypes.c_void_p(plane.buffer_ptr), ctypes.POINTER(ctypes.c_ubyte)
            )

        sdl2.SDL_UpdateYUVTexture(
            self._texture,
            rect,
            plane_ptr(planes[0]), planes[0].line_size,
            plane_ptr(planes[1]), planes[1].line_size,
            plane_ptr(planes[2]), planes[2].line_size,
        )
        sdl2.SDL_RenderClear(self._renderer)
        sdl2.SDL_RenderCopy(self._renderer, self._texture, None, None)
        sdl2.SDL_RenderPresent(self._renderer)
        now = self._clock_ms()
        if self._last_present_wall > 0:
            dt = now - self._last_present_wall
            if dt > 0:
                fps = 1000.0 / dt
                if self._stats is not None:
                    self._stats.render_fps = (
                        self._stats.render_fps * (1 - FPS_EMA_ALPHA) + fps * FPS_EMA_ALPHA
                        if self._stats.render_fps > 0
                        else fps
                    )
        self._last_present_wall = now
        self._count("presented")

    def _count(self, key: str) -> None:
        if self._stats is not None:
            setattr(self._stats, key, getattr(self._stats, key) + 1)


class PlaybackStateName:
    """渲染线程可比较的状态名常量（与 engine.PlaybackState 对齐）。"""

    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
