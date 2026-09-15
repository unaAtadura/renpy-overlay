"""悬浮窗：置顶、无边框、半透明的对话显示窗（完全可鼠标交互）。

四个关键点：

1. **完全可交互**。窗口不设 ``WS_EX_TRANSPARENT`` 等穿透样式，鼠标事件由 Tk 正常
   接收：按住窗口（对话文本 / 状态栏 / 空白处）拖动即可移动，文本区支持滚轮与右侧
   滚动条查看长对话全文。

2. **鼠标拖动 + 双击锁定位置**。``<ButtonPress-1>`` / ``<B1-Motion>`` 事件直接
   驱动 ``SetWindowPos`` 跟手移动；拖动结束后记录“相对游戏窗口的偏移”，跟随逻辑
   此后按偏移定位，不会被拉回停靠位（控制台 ``d`` 可恢复自动停靠）。**双击**切换
   位置锁定：锁定后不接受拖动、跟随也让位，位置固定；再双击解锁。

3. **跟随游戏窗口**。定时读取目标窗口的 ``GetWindowRect`` 并用 ``SetWindowPos``
   把悬浮窗贴到游戏窗口内部（默认顶部居中），游戏最小化时自动隐藏；拖动/锁定期间
   跟随让位，避免与用户操作“抢方向盘”。进程开启 Per-Monitor DPI 感知后，两边
   坐标都是物理像素，可直接对齐。

4. **锁定态翻译（手动 + 自动）**。锁定状态下单击左键，把“最近捕获的游戏原文”
   （而非窗口当前显示的文本）发给本地 LM Studio（OpenAI 兼容协议）翻译成中文并
   替换显示；网络请求在守护线程里执行，结果经队列回到主线程；同时只允许一条
   在途请求，双击（解锁）会强制中止在途翻译（结果作废，不再覆盖界面）。
   当 config.json 的 ``auto_translate`` 开启时，锁定状态下还会按轮询间隔（默认
   3 秒，主线程 after 循环）自动翻译最新的游戏原文 —— 同一原文只自动翻一次，
   进入锁定后需经过一个完整轮询间隔才会首次触发，解锁即停用并中止在途。

5. **线程模型**。Tk 只能在创建它的线程里操作，因此所有跨线程输入（IPC 回调、
   控制台命令、翻译结果）都先进队列，再由主线程的 ``after`` 定时器统一消费 ——
   不使用 "从其它线程调用 Tk" 这种未定义行为。

交互细节：单击/双击用“延迟判定”区分 —— 松开左键后等一个系统双击间隔再当作单击
处理，双击事件会取消挂起的单击，避免 Tk“双击先产生一次单击”造成误触发；在滚动条
上的按下 / 双击不会触发拖动、锁定或翻译。

显示策略：窗口只展示当前（最新一条）对话 —— 新对话到来时直接替换上一轮内容，
不累积历史、也不做行数裁剪；内容较长时可用滚轮 / 滚动条查看全文。
``config.json`` 的 ``show_original_text=false`` 时正文区不随对话刷新原文（仅影响
显示，不影响采集、标题栏提示与翻译链路）。

标题栏策略：启动时的初始文本不变；之后随交互状态动态更新 —— 新对话显示该条文本
的前 20 个字符（提醒将被发送的文本）、“翻译中... / 翻译完成 / 翻译失败”反映
翻译进程、锁定状态显示“已锁定，单击启动翻译”（自动翻译开启时为“已锁定，开始
自动翻译”）、“已解锁，中止翻译”；控制台的连接状态也写入同一位置。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk

from . import config, translator, win32api

logger = logging.getLogger("renpy_overlay.overlay")

MARGIN = 12
DOCK_CHOICES = (
    "top-center",
    "top-left",
    "top-right",
    "bottom-center",
    "bottom-left",
    "bottom-right",
)
FOLLOW_INTERVAL_MS = 200
DRAIN_INTERVAL_MS = 80
#: 双击与单击的区分：松开后等一个系统双击间隔（+ 余量）再当作单击处理，
#: 双击事件会取消挂起的单击，避免 Tk“双击先产生一次单击”的误触发。
CLICK_DELAY_MS = win32api.double_click_time_ms() + 60
#: 按下与松开之间的位移超过该值就视为拖动，不再算单击
CLICK_MOVE_TOLERANCE = 4
#: 双击后短暂抑制单击调度，防止双击 / 三击的余波被当成单击
CLICK_SUPPRESS_SECONDS = 0.5

BG = "#0e0e14"
FG_WHO = "#8ad7ff"
FG_WHAT = "#f2f2f7"
FG_DIM = "#8b8b9a"


def compute_geometry(
    game_rect: tuple[int, int, int, int],
    size: tuple[int, int],
    dock: str,
    user_offset: tuple[int, int] | None = None,
    margin: int = MARGIN,
) -> tuple[int, int, int, int]:
    """计算悬浮窗的目标几何 ``(x, y, 宽, 高)``（屏幕物理像素）。

    - ``user_offset`` 为 None：按 ``dock`` 贴靠游戏窗口；
    - ``user_offset`` 存在：用户手动拖动过，位置 = 游戏窗口左上角 + 偏移，
      尺寸仍按游戏窗口约束缩放 —— 游戏窗口移动/改变大小时悬浮窗跟随，
      但保持用户设定的相对位置。

    抽成纯函数便于离线验证坐标计算（见 ``tests/test_overlay.py``）。
    """
    left, top, right, bottom = game_rect
    game_w, game_h = max(1, right - left), max(1, bottom - top)
    width = max(240, min(size[0], game_w - 2 * margin))
    height = max(80, min(size[1], game_h // 2))

    if user_offset is not None:
        return left + user_offset[0], top + user_offset[1], width, height

    if dock.startswith("top"):
        y = top + margin
    else:
        y = bottom - height - margin
    if dock.endswith("left"):
        x = left + margin
    elif dock.endswith("right"):
        x = right - width - margin
    else:
        x = left + (game_w - width) // 2
    return x, y, width, height


class OverlayWindow:
    """对话悬浮窗。``run()`` 会阻塞在当前线程（必须是主线程）。"""

    def __init__(
        self,
        target_pid: int,
        dock: str = "top-center",
        width: int = 880,
        height: int = 200,
        alpha: float = 0.85,
        font_family: str = "Microsoft YaHei UI",
        font_size: int = 11,
        app_config: config.AppConfig | None = None,
        on_quit=None,
    ):
        self.target_pid = target_pid
        self.dock = dock if dock in DOCK_CHOICES else "top-center"
        self.width = width
        self.height = height
        self.alpha = alpha
        self.font_family = font_family
        self.font_size = font_size
        self.on_quit = on_quit
        self._config = app_config or config.AppConfig()

        self._queue: queue.Queue = queue.Queue()
        self._root = tk.Tk()
        self._status_var = tk.StringVar(master=self._root, value="正在启动…")
        self._hwnd = 0
        self._target_hwnd = 0
        self._visible = True
        self._closed = False
        self._dragging = False
        self._drag_grab = (0, 0)  # 按下瞬间鼠标相对窗口左上角的位置
        self._drag_size = (width, height)
        self._drag_origin = (0, 0)  # 拖动开始时的窗口位置（用于判断是否真的移动过）
        self._user_offset: tuple[int, int] | None = None  # 手动拖动后的相对偏移
        self._locked = False  # 双击锁定位置：锁定后不接受拖动、跟随让位
        self._press_pos: tuple[int, int] | None = None  # 左键按下的屏幕坐标（单击判定用）
        self._click_after_id: str | None = None  # 挂起的延迟单击任务
        self._click_suppress_until = 0.0  # 双击后的短窗口内不再调度单击
        self._last_say: dict | None = None  # 最近一次捕获的游戏原文（翻译请求的输入）
        self._translating = False  # 是否有在途翻译请求（仅主线程读写）
        self._translation_seq = 0  # 翻译世代号：双击中止 / 新请求时递增，作废旧结果
        self._last_translation_input: str | None = None  # 最近已触发翻译的原文（自动去重）
        self._lock_started_at = 0.0  # 本次锁定开始时间（自动翻译需等一个完整间隔）
        self._auto_skip_reason: str | None = None  # 上次自动翻译跳过原因（日志去重）
        self._build()

    # ------------------------------------------------------------ 界面构建

    def _build(self) -> None:
        root = self._root
        root.title(f"renpy-overlay · pid={self.target_pid}")
        root.overrideredirect(True)
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        if self.alpha < 1.0:
            root.attributes("-alpha", max(0.1, min(1.0, self.alpha)))
        root.geometry(f"{self.width}x{self.height}+0+0")

        header = tk.Label(
            root,
            textvariable=self._status_var,
            bg=BG,
            fg=FG_DIM,
            anchor="w",
            justify="left",
            padx=10,
            pady=4,
            font=(self.font_family, max(8, self.font_size - 2)),
        )
        header.pack(side="top", fill="x")

        body = tk.Frame(root, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))
        self._text = tk.Text(
            body,
            wrap="word",
            bg="#16161f",
            fg=FG_WHAT,
            insertbackground=FG_WHAT,
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            highlightthickness=0,
            font=(self.font_family, self.font_size),
            spacing1=1,
            spacing3=3,
        )
        scrollbar = tk.Scrollbar(body, command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)
        self._text.tag_configure(
            "who", foreground=FG_WHO, spacing1=6, font=(self.font_family, self.font_size, "bold")
        )
        self._text.tag_configure("what", foreground=FG_WHAT)
        self._text.tag_configure(
            "dim", foreground=FG_DIM, font=(self.font_family, max(8, self.font_size - 2))
        )
        self._text.configure(state="disabled")

        # 鼠标交互：左键拖动窗口（滚动条上的按下留给滚动条自己处理），滚轮查看长对话
        root.bind("<ButtonPress-1>", self._on_drag_press)
        root.bind("<B1-Motion>", self._on_drag_motion)
        root.bind("<ButtonRelease-1>", self._on_drag_release)
        root.bind("<Double-Button-1>", self._on_double_click)
        for widget in (root, self._text, scrollbar):
            widget.bind("<MouseWheel>", self._on_mousewheel)

        self.hint(
            "悬浮窗已启动：拖动可调整位置；双击锁定/解锁位置；锁定后单击可翻译；"
            "长对话可用滚轮或滚动条查看全文；控制台可输入 h / d / q"
        )

        root.update_idletasks()
        self._hwnd = win32api.toplevel_hwnd(root.winfo_id())
        logger.debug("悬浮窗 HWND=0x%X（Tk id=0x%X）", self._hwnd, root.winfo_id())

    # ------------------------------------------------------------ 对外接口

    def set_status(self, text: str) -> None:
        self._queue.put(("status", text))

    def push_say(self, who: str, what: str, source: str = "", ts: float | None = None) -> None:
        self._queue.put(("say", {"who": who, "what": what, "src": source, "ts": ts or time.time()}))

    def hint(self, text: str) -> None:
        self._queue.put(("hint", text))

    def toggle_visible(self) -> None:
        self._queue.put(("cmd", "toggle"))

    def reset_position(self) -> None:
        """恢复“自动停靠”（清除手动拖动锁定的位置）。"""
        self._queue.put(("cmd", "dock"))

    def request_close(self) -> None:
        self._queue.put(("cmd", "quit"))

    @property
    def hwnd(self) -> int:
        return self._hwnd

    @property
    def visible(self) -> bool:
        return self._visible

    # ------------------------------------------------------------ 主线程循环

    def run(self) -> None:
        """启动 Tk 主循环（阻塞）。返回即代表窗口已销毁。"""
        self._root.after(DRAIN_INTERVAL_MS, self._drain)
        self._root.after(FOLLOW_INTERVAL_MS, self._follow)
        self._start_auto_translate()
        try:
            self._root.mainloop()
        finally:
            self._closed = True
            logger.debug("悬浮窗已销毁")

    def close(self) -> None:
        """从任意线程请求关闭。"""
        try:
            self._root.after(0, self._shutdown)
        except Exception:  # pragma: no cover - 窗口已销毁
            pass

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._translation_seq += 1  # 在途翻译的结果作废，不再覆盖界面
        self._cancel_pending_click()
        try:
            self._root.quit()
            self._root.destroy()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 队列消费

    def _drain(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "say":
                    who = str(payload.get("who") or "")
                    what = str(payload.get("what") or "")
                    self._last_say = {"who": who, "what": what}
                    preview = what.strip()[:20]
                    if preview:
                        # 标题栏提醒：这条对话就是翻译时将发送的文本（不受显示开关影响）
                        self._update_header(preview)
                    if self._config.show_original_text:
                        self._append_say(payload)
                    else:
                        # 正文区不刷新，但原文已记录，翻译/自动翻译链路不受影响
                        logger.debug(
                            "已捕获对话（show_original_text=false，正文区不更新）：[%s] %s",
                            who or "-",
                            what[:60],
                        )
                elif kind == "hint":
                    self._append_dim(payload)
                elif kind == "status":
                    self._update_header(payload)
                elif kind == "translation":
                    self._handle_translation(payload)
                elif kind == "cmd":
                    self._handle_command(payload)
        except queue.Empty:
            pass
        except Exception:  # pragma: no cover - UI 异常不应终止循环
            logger.exception("处理界面消息时出错")
        if not self._closed:
            self._root.after(DRAIN_INTERVAL_MS, self._drain)

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

    def _update_header(self, text: str) -> None:
        """标题栏状态文案的唯一出口：反映当前交互状态，变化时写入日志。"""
        if self._status_var.get() == text:
            return
        self._status_var.set(text)
        logger.debug("标题栏状态更新：%s", text)

    def _set_visible(self, visible: bool) -> None:
        try:
            if visible:
                self._root.deiconify()
                self._root.attributes("-topmost", True)
            else:
                self._root.withdraw()
        except Exception:  # pragma: no cover
            return
        self._visible = visible
        logger.debug("悬浮窗可见性：%s", visible)

    def _append_dim(self, text: str) -> None:
        self._write([("\n" + text + "\n", "dim")])

    def _append_say(self, item: dict) -> None:
        who = str(item.get("who") or "").strip()
        what = str(item.get("what") or "").strip()
        if not what:
            return
        self._display_say(who, what)
        logger.debug("显示对话：[%s] %s", who or "-", what[:60])

    def _display_say(self, who: str, what: str) -> None:
        """按现有对话样式替换显示（窗口只保留最新一条，不累积历史）。"""
        blocks: list[tuple[str, str]] = []
        if who:
            blocks.append((who, "who"))
            blocks.append(("\n", "dim"))
        blocks.append((what + "\n", "what"))
        self._write(blocks)

    def _write(self, blocks: list[tuple[str, str]]) -> None:
        """用本次内容整体替换对话区（窗口只保留最新一条，不累积历史）。"""
        text = self._text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for content, tag in blocks:
            text.insert("end", content, tag)
        text.configure(state="disabled")
        text.yview_moveto(0.0)  # 长对话从开头看起，其余部分自行滚动查看

    # ------------------------------------------------------------ 滚动

    def _on_mousewheel(self, event) -> str:
        """滚轮查看长对话的其余部分；返回 “break” 阻止 Tk 默认绑定重复滚动。"""
        steps = -int(event.delta / 120) * 3 if event.delta else -1
        if steps:
            self._text.yview_scroll(steps, "units")
        return "break"

    # ------------------------------------------------------------ 跟随与定位

    def _resolve_target(self) -> int:
        hwnd = self._target_hwnd
        if hwnd and win32api.is_window_valid(hwnd):
            return hwnd
        hwnd = win32api.find_main_window(self.target_pid) or 0
        if hwnd != self._target_hwnd:
            logger.info("已定位游戏窗口：HWND=0x%X", hwnd)
        self._target_hwnd = hwnd
        return hwnd

    def _follow(self) -> None:
        if self._closed:
            return
        try:
            self._follow_once()
        except Exception:  # pragma: no cover - 窗口在跟随过程中关闭
            logger.exception("跟随游戏窗口时出错")
        if not self._closed:
            self._root.after(FOLLOW_INTERVAL_MS, self._follow)

    def _follow_once(self) -> None:
        hwnd = self._resolve_target()
        if not hwnd:
            self._status_var.set(f"等待 pid={self.target_pid} 的游戏窗口…")
            if self._visible:
                self._set_visible(False)
            return
        if win32api.is_minimized(hwnd):
            if self._visible:
                self._set_visible(False)
            return
        if not self._visible:
            self._set_visible(True)
        if self._locked or self._dragging:
            # 位置已锁定 / 拖动中：由用户操作主导，跟随逻辑让位
            return

        rect = win32api.window_rect(hwnd)
        x, y, width, height = compute_geometry(
            rect, (self.width, self.height), self.dock, self._user_offset
        )
        win32api.move_window(self._hwnd, x, y, width, height, topmost=True)

    # ------------------------------------------------------------ 鼠标拖动

    def _on_drag_press(self, event) -> None:
        """左键按下：记录点击候选；未锁定时开始拖动。滚动条上的按下留给滚动条。"""
        if self._closed or isinstance(event.widget, tk.Scrollbar):
            return
        self._press_pos = (event.x_root, event.y_root)
        if self._locked:
            logger.debug("位置已锁定，忽略拖动按下")
            return
        left, top, right, bottom = win32api.window_rect(self._hwnd)
        self._dragging = True
        self._drag_grab = (event.x_root - left, event.y_root - top)
        self._drag_size = (max(1, right - left), max(1, bottom - top))
        self._drag_origin = (left, top)
        logger.info(
            "开始拖动悬浮窗：窗口位于 (%d, %d)，鼠标抓取点 (%d, %d)",
            left,
            top,
            event.x_root - left,
            event.y_root - top,
        )

    def _on_drag_motion(self, event) -> None:
        """拖动中：窗口跟随鼠标（保持抓取点相对位置不变），跟手平滑。"""
        if not self._dragging:
            return
        try:
            win32api.move_window(
                self._hwnd,
                event.x_root - self._drag_grab[0],
                event.y_root - self._drag_grab[1],
                self._drag_size[0],
                self._drag_size[1],
                topmost=True,
                resize=False,
            )
        except Exception:  # pragma: no cover - 窗口在拖动中销毁
            self._dragging = False

    def _on_drag_release(self, event) -> None:
        if self._closed or isinstance(event.widget, tk.Scrollbar):
            return
        if self._dragging:
            self._finish_drag()
        press_pos = self._press_pos
        self._press_pos = None
        if press_pos is None:
            return
        if (
            abs(event.x_root - press_pos[0]) > CLICK_MOVE_TOLERANCE
            or abs(event.y_root - press_pos[1]) > CLICK_MOVE_TOLERANCE
        ):
            return  # 移动过 = 拖动，不算单击
        if time.time() < self._click_suppress_until:
            return  # 双击/三击的余波，不调度单击
        self._schedule_click()

    def _finish_drag(self) -> None:
        """结束拖动：把当前位置换算成相对游戏窗口的偏移并锁定（供跟随逻辑使用）。"""
        self._dragging = False
        try:
            rect = win32api.window_rect(self._hwnd)
        except Exception:  # pragma: no cover - 窗口已销毁
            return
        if (rect[0], rect[1]) == self._drag_origin:
            logger.debug("拖动结束：位置未变化，保持原有跟随方式")
            return
        hwnd = self._resolve_target()
        if not hwnd:
            logger.warning("拖动结束：未能定位游戏窗口，本次位置不参与跟随")
            return
        game = win32api.window_rect(hwnd)
        offset = (rect[0] - game[0], rect[1] - game[1])
        self._user_offset = offset
        logger.info(
            "拖动结束：位置已锁定为相对游戏窗口的偏移 (%+d, %+d)（控制台输入 d 可恢复自动停靠）",
            offset[0],
            offset[1],
        )

    def _reset_to_dock(self) -> None:
        """清除手动拖动的位置，回到自动停靠。"""
        if self._user_offset is None:
            logger.info("当前已是自动停靠位置，无需恢复。")
            return
        self._user_offset = None
        logger.info("已恢复自动停靠位置（dock=%s）", self.dock)
        try:
            self._follow_once()
        except Exception:  # pragma: no cover
            logger.debug("恢复停靠时立即定位失败", exc_info=True)

    # ------------------------------------------------------------ 双击锁定与单击翻译

    def _on_double_click(self, event) -> None:
        """双击：中止在途翻译并切换位置锁定（滚动条上的双击不参与）。"""
        if self._closed or isinstance(event.widget, tk.Scrollbar):
            return
        self._cancel_pending_click()
        self._click_suppress_until = time.time() + CLICK_SUPPRESS_SECONDS
        if self._dragging:
            # 双击的第二次按下可能顺带开了拖动，直接取消，避免它的 release 产生副作用
            self._dragging = False
        self._abort_translation("双击")
        self._toggle_lock()

    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        if self._locked:
            self._lock_started_at = time.time()
            if self._config.auto_translate:
                logger.info("已锁定悬浮窗位置（自动翻译开启：等一个轮询间隔后开始）")
                self._update_header("已锁定，开始自动翻译")
            else:
                logger.info("已锁定悬浮窗位置（双击解锁；锁定状态下单击触发翻译）")
                self._update_header("已锁定，单击启动翻译")
        else:
            logger.info("已解锁悬浮窗位置（恢复鼠标拖动与自动跟随；自动翻译停用）")
            self._update_header("已解锁，中止翻译")

    def _schedule_click(self) -> None:
        """延迟判定单击：等一个系统双击间隔，双击会取消该任务。"""
        self._cancel_pending_click()
        self._click_after_id = self._root.after(CLICK_DELAY_MS, self._on_delayed_click)

    def _cancel_pending_click(self) -> None:
        if self._click_after_id is None:
            return
        try:
            self._root.after_cancel(self._click_after_id)
        except Exception:  # pragma: no cover - 窗口已销毁
            pass
        self._click_after_id = None

    def _on_delayed_click(self) -> None:
        self._click_after_id = None
        if not self._locked:
            logger.debug("未锁定状态，单击不触发翻译")
            return
        logger.debug("锁定状态下的单击：请求翻译当前对话原文")
        self._start_translation()

    def _start_translation(self, origin: str = "manual") -> None:
        """发起翻译请求（仅主线程调用）。同一时刻只允许一条在途请求。"""
        label = "自动触发" if origin == "auto" else "单击触发"
        if self._translating:
            logger.info("已有翻译请求在途，忽略本次%s翻译", label)
            return
        say = self._last_say or {}
        what = str(say.get("what") or "").strip()
        if not what:
            logger.info("暂无可翻译的捕获文本，跳过翻译请求")
            return
        who = str(say.get("who") or "").strip()
        self._translating = True
        self._translation_seq += 1
        seq = self._translation_seq
        self._last_translation_input = what  # 记录已触发的原文，自动翻译据此去重
        logger.info("发起翻译请求（%s，seq=%d，原文 %d 字）：%s", label, seq, len(what), what[:40])
        self._update_header("翻译中...")
        thread = threading.Thread(
            target=self._translate_worker,
            args=(seq, who, what),
            name=f"overlay-translate-{seq}",
            daemon=True,
        )
        thread.start()

    def _translate_worker(self, seq: int, who: str, what: str) -> None:
        """后台线程：调用 LM Studio 翻译（输入为捕获的游戏原文），结果只经队列回主线程。"""
        try:
            text = translator.translate_text(what)
            message = {"seq": seq, "ok": True, "who": who, "what": text}
        except Exception as exc:  # 网络/协议错误统一收敛为失败结果
            logger.debug("翻译请求失败（seq=%d）：%s", seq, exc)
            message = {"seq": seq, "ok": False, "who": who, "error": str(exc)}
        self._queue.put(("translation", message))

    def _handle_translation(self, item: dict) -> None:
        """主线程：校验世代号后把译文按现有规则替换显示。"""
        seq = item.get("seq")
        if seq != self._translation_seq:
            logger.info("翻译结果已过期（seq=%s，当前=%s），丢弃", seq, self._translation_seq)
            return
        self._translating = False
        if not item.get("ok"):
            logger.warning("翻译失败：%s", item.get("error"))
            self._update_header("翻译失败")
            return
        who = str(item.get("who") or "").strip()
        text = str(item.get("what") or "").strip()
        if not text:
            logger.warning("翻译返回空文本，保持当前显示")
            self._update_header("翻译失败")
            return
        logger.info("翻译完成（seq=%s），已替换为译文（%d 字）", seq, len(text))
        self._display_say(who, text)
        self._update_header("翻译完成")

    def _abort_translation(self, reason: str) -> None:
        """作废在途翻译：其网络等待自然结束，但结果不再可能覆盖界面。"""
        if not self._translating:
            return
        self._translation_seq += 1
        self._translating = False
        logger.info("已中止在途翻译（%s）：结果作废，界面保持当前状态", reason)

    # -- 自动翻译（config.json: auto_translate / auto_translate_interval）

    def _start_auto_translate(self) -> None:
        """启动自动翻译轮询（仅在配置开启时）。"""
        if not self._config.auto_translate:
            logger.info("自动翻译未启用（config.json: auto_translate=false）")
            return
        interval_ms = self._auto_interval_ms()
        logger.info("自动翻译已启用：每 %.1f 秒轮询一次（仅锁定状态生效）", interval_ms / 1000.0)
        self._root.after(interval_ms, self._auto_translate_tick)

    def _auto_interval_ms(self) -> int:
        return max(200, int(self._config.auto_translate_interval * 1000))

    def _auto_translate_tick(self) -> None:
        """自动翻译轮询（主线程 after 循环，与 _drain/_follow 同模式）。"""
        if self._closed:
            return
        try:
            self._auto_translate_check()
        except Exception:  # pragma: no cover - 轮询异常不应终止循环
            logger.exception("自动翻译轮询出错")
        if not self._closed:
            self._root.after(self._auto_interval_ms(), self._auto_translate_tick)

    def _auto_translate_check(self) -> None:
        """条件全部满足时自动发起一次翻译；任一不满足则跳过（原因去重记日志）。"""
        if not self._config.auto_translate:
            return
        if not self._locked:
            self._auto_note_skip("未锁定")
            return
        if time.time() - self._lock_started_at < self._config.auto_translate_interval:
            self._auto_note_skip("刚进入锁定，等待一个完整轮询间隔")
            return
        say = self._last_say or {}
        what = str(say.get("what") or "").strip()
        if not what:
            self._auto_note_skip("暂无可翻译的捕获文本")
            return
        if what == self._last_translation_input:
            self._auto_note_skip("该条原文已翻译过")
            return
        if self._translating:
            self._auto_note_skip("已有翻译请求在途")
            return
        self._auto_skip_reason = None
        self._start_translation(origin="auto")

    def _auto_note_skip(self, reason: str) -> None:
        if reason == self._auto_skip_reason:
            return
        self._auto_skip_reason = reason
        logger.debug("自动翻译跳过：%s", reason)
