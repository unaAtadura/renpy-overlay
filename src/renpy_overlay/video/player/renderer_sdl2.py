"""SDL2 视频渲染器：NV12/IYUV 流式纹理直传 + 低延迟信箱上屏。

渲染管线核心：

- 嵌入 Qt：``SDL_CreateWindowFrom(控件 winId)`` 直接把 SDL 渲染器挂到
  Qt 画布的原生窗口句柄上，SDL 纹理经 d3d9 后端上屏（免软件缩放）。
- 零转换直传：解码 worker 把帧统一为 yuv420p（本场景素材原生即该格
  式，一次转换都不做），渲染线程以 IYUV 流式纹理按平面更新——跳过
  rgb24 转格式 + QImage 深拷贝的整条开销链。
- 低延迟信箱：暂停态帧（打开首帧/seek 目标帧）经信箱立即上屏；播放
  态按音频主时钟节拍消费预解码队列，到期即传即 present，无中间排队。
  陈帧（seek 前代数号）直接丢弃不占上屏路径。
- 上屏回报：``on_presented(frame)``（移植适配：传整帧供门面缓存
  payload，供暂停放大帧按需转 QImage）。

线程：SDL 渲染器在 attach 线程（主线程）创建，渲染循环跑独立线程；
SDL 对象非线程安全——纹理更新/present 全部限定在渲染线程，创建/销毁
限定在主线程（close 经标志位移交渲染线程外的 join 收口）。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

import sdl2

from .sync_clock import pace_wait_ms, should_drop, wall_clock_ms

logger = logging.getLogger("renpy_overlay.video.player.renderer_sdl2")

#: EOF 哨兵帧的 pts 约定（decoder 约定俗成）
EOF_PTS = -1
#: 渲染循环空转轮询间隔（秒）
POLL_INTERVAL_S = 0.05
#: FPS 平滑系数（EMA）
FPS_EMA_ALPHA = 0.15


class SdlVideoRenderer(threading.Thread):
    """SDL 渲染线程：信箱帧立即上屏 + 播放态按主时钟节拍消费队列。"""

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
    ) -> None:
        super().__init__(name="renpy-overlay-video-render", daemon=True)
        self._vqueue = vqueue
        self._clock_provider = clock_provider
        self._state_provider = state_provider
        self._gen_provider = gen_provider
        self._on_presented = on_presented
        self._on_ended = on_ended
        self._stats = stats
        self._clock_ms = clock_ms or (lambda: time.monotonic() * 1000.0)
        self._window = None
        self._renderer = None
        self._texture = None
        self._tex_w = 0
        self._tex_h = 0
        self._hwnd = 0  # 外部窗口句柄（尺寸同步用）
        self._mailbox = None  # 暂停态立即上屏帧（VideoFrame）
        self._stop_flag = False
        self._cond = threading.Condition()
        self._last_present_wall = 0.0
        self._wall_base_pts = 0
        self._wall_base_wall = 0.0

    # ---- 生命周期（attach/destroy 限定主线程） ---------------------------------

    def attach_window(self, hwnd: int) -> bool:
        """从 Qt 控件原生句柄创建 SDL 窗口与渲染器（主线程调用）。

        强制 direct3d9 后端：d3d9 的 windowed present 会把 backbuffer
        自动拉伸到当前客户区——Qt 布局/resize 变化客户区尺寸后无需重
        建交换链（d3d11 的 flip 模型要求尺寸严格一致，否则只能覆盖
        左上部分或逐帧重建）。
        """
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"linear")
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_DRIVER, b"direct3d")
        self._hwnd = int(hwnd)
        self._window = sdl2.SDL_CreateWindowFrom(ctypes.c_void_p(self._hwnd))
        if not self._window:
            logger.error("SDL_CreateWindowFrom 失败：%s", sdl2.SDL_GetError().decode())
            return False
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
        logger.info("SDL 渲染器已挂载到 Qt 画布（driver=%s）", driver)
        return True

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

    def destroy_sdl(self) -> None:
        """销毁纹理与渲染器（主线程；窗口句柄归 Qt，不销毁）。"""
        if self._texture:
            sdl2.SDL_DestroyTexture(self._texture)
            self._texture = None
        if self._renderer:
            sdl2.SDL_DestroyRenderer(self._renderer)
            self._renderer = None
        self._window = None

    # ---- 渲染线程主体 -----------------------------------------------------------

    def run(self) -> None:  # pragma: no cover - SDL 链路人工联测
        try:
            self._render_loop()
        except Exception:
            logger.warning("渲染线程异常退出", exc_info=True)

    def _render_loop(self) -> None:
        while not self._stop_flag:
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
        self._render_black()

    def _take_mailbox(self) -> bool:
        with self._cond:
            frame, self._mailbox = self._mailbox, None
        if frame is None:
            return False
        if frame.pts_ms == EOF_PTS:
            return True
        self._configure_texture(frame)
        self._upload_and_present(frame.payload)
        self._on_presented(frame)
        return True

    def _pace_and_present(self, frame) -> None:
        """主时钟节拍等待 → 到期直传上屏；落后超阈值丢帧追平。"""
        while not self._stop_flag:
            if frame.gen != self._gen_provider():
                self._count("stale")
                return
            wait = pace_wait_ms(frame.pts_ms, int(self._current_clock()))
            if wait <= 0:
                break
            time.sleep(min(wait, 50) / 1000.0)
        if self._stop_flag:
            return
        clock = int(self._current_clock())
        if should_drop(frame.pts_ms, clock):
            self._count("dropped")
            return
        self._configure_texture(frame)
        self._upload_and_present(frame.payload)
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

    def _upload_and_present(self, payload) -> None:
        """yuv420p 三平面直传纹理 → present（渲染线程内执行）。"""
        if self._texture is None or self._renderer is None:
            return
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

    def _render_black(self) -> None:
        if self._renderer is not None:
            sdl2.SDL_RenderClear(self._renderer)
            sdl2.SDL_RenderPresent(self._renderer)

    def _count(self, key: str) -> None:
        if self._stats is not None:
            setattr(self._stats, key, getattr(self._stats, key) + 1)


class PlaybackStateName:
    """渲染线程可比较的状态名常量（与 engine.PlaybackState 对齐）。"""

    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
