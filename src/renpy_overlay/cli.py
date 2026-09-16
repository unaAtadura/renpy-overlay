"""命令行入口与主流程编排。

职责边界：这个模块只做"串起来"和"收拾干净"两件事 ——
- 参数解析、日志初始化、DPI 感知、目标选择（``--pid`` / 图形选择窗 / 命令行菜单）；
- 组装 IPC 通道 → 注入 agent → 驱动悬浮窗 → 退出时逆序清理。

清理顺序是刻意的：先让游戏端卸载代理（停止产生新消息），再停接收端，最后销毁界面
并释放远程内存。任何一步失败都只记日志不中断后续步骤，保证"退出时不会把游戏搞崩、
也不会留下悬挂的线程与句柄"。
"""

from __future__ import annotations

import argparse
import atexit
import os
import secrets
import signal
import sys
import threading
import time

import psutil

from . import config, discovery, logs, picker, win32api
from .discovery import Candidate
from .injector import InjectionError, Injector
from .ipc import DialogueLink, PeerState
from .logs import get_logger, log_from_game
from .overlay import DOCK_CHOICES, OverlayWindow

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130

EPILOG = """\
示例：
  uv run renpy-overlay --list                      # 只列出候选进程
  uv run renpy-overlay                             # 弹出选择窗，选好后注入
  uv run renpy-overlay --pid 12345                 # 直接注入指定 PID
  uv run renpy-overlay --pid 12345 --no-overlay    # 只注入，对话打印到控制台
  uv run renpy-overlay --pid 12345 --status        # 查询游戏内代理状态
  uv run renpy-overlay --pid 12345 --unload        # 卸载游戏内代理

悬浮窗操作（鼠标可直接交互）：
  拖动窗口    按住悬浮窗任意区域（对话文本 / 状态栏）用左键拖动，松开后位置锁定
  双击锁定    双击锁定 / 解锁位置；锁定后不再响应拖动，位置固定不被跟随拉回
  单击翻译    锁定状态下单击，把对话原文发给本地 LM Studio 翻译成中文并替换显示
  自动翻译    config.json 开启（auto_translate: true）后，锁定状态按间隔自动翻译最新对话
  显示原文    config.json 的 show_original_text（默认 true）控制正文区是否随对话显示原文
  缓存大小    config.json 的 translation_cache_size_kb（默认 256KB）控制内存缓存上限
  查看全文    窗口只显示最新一条对话；内容较长时可用滚轮或滚动条查看全文
  恢复停靠    控制台输入 d；隐藏/显示输入 h；退出输入 q
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renpy-overlay",
        description="向运行中的 Ren'Py 游戏进程注入对话采集代理，并在悬浮窗中实时显示对话。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    target = parser.add_argument_group("目标选择")
    target.add_argument("--pid", type=int, help="直接指定目标进程 PID（跳过选择界面）")
    target.add_argument("--list", action="store_true", help="只列出候选进程后退出")
    target.add_argument("--all", action="store_true", help="列出全部进程（含低分候选）")
    target.add_argument("--no-gui", action="store_true", help="不使用图形选择窗，改走命令行菜单")
    target.add_argument("--unload", action="store_true", help="卸载目标进程内已注入的代理后退出")
    target.add_argument("--status", action="store_true", help="查询目标进程内代理的运行状态后退出")

    ui = parser.add_argument_group("悬浮窗")
    ui.add_argument("--no-overlay", action="store_true", help="不显示悬浮窗，只在控制台打印对话")
    ui.add_argument(
        "--dock", choices=DOCK_CHOICES, default="top-center", help="悬浮窗相对游戏窗口的停靠位置"
    )
    ui.add_argument("--width", type=int, default=880, help="悬浮窗宽度（像素）")
    ui.add_argument("--height", type=int, default=200, help="悬浮窗高度（像素）")
    ui.add_argument("--alpha", type=float, default=0.85, help="悬浮窗不透明度 0.1~1.0")
    ui.add_argument("--font-size", type=int, default=11, help="对话字号")

    runtime = parser.add_argument_group("运行参数")
    runtime.add_argument(
        "--port", type=int, default=0, help="IPC 监听端口（默认由系统分配空闲端口）"
    )
    runtime.add_argument("--timeout", type=float, default=15.0, help="等待远程线程结束的秒数")
    runtime.add_argument(
        "--idle-exit", type=float, default=20.0, help="游戏进程消失后自动退出的秒数"
    )
    runtime.add_argument("--poll-hz", type=float, default=7.0, help="游戏内兜底轮询频率")
    runtime.add_argument("--log-level", default="INFO", help="控制台日志级别")
    runtime.add_argument("--log-dir", default="logs", help="日志目录")
    runtime.add_argument("--log-file", default=None, help="指定日志文件路径")
    return parser


def _select_target(args: argparse.Namespace) -> Candidate | None:
    log = get_logger("cli")

    if args.pid:
        candidate = discovery.find_candidate(args.pid)
        if candidate is None:
            log.error("PID %s 不存在或无法访问。", args.pid)
            return None
        log.info("目标进程：%s", candidate.detail().replace("\n", " | "))
        return candidate

    candidates = discovery.enumerate_candidates(include_all=args.all)
    log.info(
        "共发现 %d 个候选进程（%d 个已确认加载 Python 运行时）",
        len(candidates),
        sum(1 for item in candidates if item.is_renpy),
    )
    chosen = picker.choose_target(
        candidates,
        refresh=lambda: discovery.enumerate_candidates(include_all=args.all),
        use_gui=not args.no_gui,
    )
    if chosen is None:
        log.info("已取消选择。")
        return None
    log.info("已选择 PID=%d（%s）", chosen.pid, chosen.name)
    if not chosen.python_dll:
        log.warning(
            "该进程内未检测到 Python 运行库，注入很可能失败。若确认是 Ren'Py 游戏，"
            "请尝试以管理员身份运行本工具，或用 --all 重新确认。"
        )
    return chosen


class Session:
    """一次完整的注入会话：IPC + 注入 + 悬浮窗 + 清理。"""

    def __init__(self, args: argparse.Namespace, target: Candidate):
        self.args = args
        self.target = target
        self.log = get_logger("session")
        self.injector = Injector(target.pid, timeout=args.timeout)
        self.link = DialogueLink(
            token=secrets.token_hex(16),
            port=args.port,
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )
        self.overlay: OverlayWindow | None = None
        self._stop = threading.Event()
        self._cleaned = False
        self._io_lock = threading.Lock()
        self._console: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._say_count = 0

    # ------------------------------------------------------------ 启动

    def run(self) -> int:
        args = self.args
        port = self.link.start()
        self.log.info("对话接收通道就绪：127.0.0.1:%d", port)

        config = {
            "host": "127.0.0.1",
            "port": port,
            "token": self.link.token,
            "poll_hz": args.poll_hz,
            "notify": True,
        }
        try:
            outcome = self.injector.inject_agent(config)
        except InjectionError as exc:
            self.log.error("注入失败：%s", exc)
            self._cleanup(unload=False)
            return EXIT_FAILED

        if not outcome.ok:
            self.log.error("注入未成功，游戏内代理没有启动：%s", outcome.summary())
            self._cleanup(unload=True)
            return EXIT_FAILED

        info = (outcome.payload_result or {}).get("info") or {}
        self.log.info(
            "注入成功：Python %s（%s，%s），Hook 层=%s",
            outcome.python_version or info.get("py", "?"),
            outcome.python_dll,
            outcome.arch,
            ", ".join(info.get("layers", [])) or "待主线程注册",
        )

        self._start_monitor()
        self._start_console()

        if args.no_overlay:
            return self._run_console_mode()
        return self._run_overlay_mode()

    def _run_overlay_mode(self) -> int:
        args = self.args
        self.overlay = OverlayWindow(
            target_pid=self.target.pid,
            dock=args.dock,
            width=args.width,
            height=args.height,
            alpha=args.alpha,
            font_size=args.font_size,
            app_config=config.load_config(),  # 本地 config.json（自动翻译开关 / 轮询间隔）
            game_dir=os.path.dirname(self.target.exe) if self.target.exe else "",
            on_quit=self._request_stop,
        )
        self.overlay.set_status(self._status_text("等待游戏端上报…"))
        self.overlay.hint(
            f"已注入 pid={self.target.pid}（{self.target.name}）。"
            "拖动可调整位置；双击锁定/解锁位置；锁定后单击可用本地 LM Studio 翻译；"
            "控制台可输入 h/hide、d/dock、u/unload、q/quit。"
        )
        self._install_signal_handler()
        try:
            self.overlay.run()
        finally:
            self._cleanup(unload=True)
        return EXIT_OK

    def _run_console_mode(self) -> int:
        self.log.info("未启用悬浮窗，对话将直接打印到控制台。按 Ctrl+C 结束。")
        self._install_signal_handler()
        try:
            while not self._stop.wait(0.4):
                self._check_game_alive()
        except KeyboardInterrupt:  # pragma: no cover - Ctrl+C
            self.log.info("收到中断信号。")
        finally:
            self._cleanup(unload=True)
        return EXIT_OK

    # ------------------------------------------------------------ 消息处理

    def _on_message(self, message: dict, peer: PeerState) -> None:
        kind = message.get("t")
        if kind == "say":
            self._say_count += 1
            who = str(message.get("who") or "")
            what = str(message.get("what") or "")
            if self.args.no_overlay:
                print(f"[{who or '-'}] {what}")
            if self.overlay is not None:
                self.overlay.push_say(who, what, source=str(message.get("src") or ""))
        elif kind == "log":
            log_from_game(str(message.get("level", "info")), str(message.get("msg", "")))
        elif kind == "hooks":
            layers = message.get("layers") or []
            self.log.info(
                "游戏端确认 Hook 层：%s", ", ".join(str(item) for item in layers) or "<空>"
            )
            if self.overlay is not None:
                self.overlay.set_status(self._status_text(f"Hook {len(layers)} 层"))
                self.overlay.hint("Hook 就绪：" + ("、".join(str(item) for item in layers) or "无"))
        elif kind == "bye":
            stats = message.get("stats") or {}
            self.log.info("游戏端代理已卸载：%s", stats)
        else:
            self.log.debug("未知消息类型：%s", kind)

    def _on_connect(self, peer: PeerState) -> None:
        self.log.info("已连接游戏端 pid=%s python=%s", peer.pid, peer.py_version)
        if self.overlay is not None:
            self.overlay.set_status(self._status_text("已连接"))

    def _on_disconnect(self, peer: PeerState) -> None:
        if self.overlay is not None:
            self.overlay.set_status(self._status_text("连接断开"))
        if not self._stop.is_set():
            self.log.warning("与游戏端的连接断开，代理可能已被卸载或游戏正在退出。")

    def _status_text(self, extra: str) -> str:
        return (
            f"renpy-overlay · pid={self.target.pid} · {self.target.name} · "
            f"已捕获 {self._say_count} 条 · {extra}"
        )

    # ------------------------------------------------------------ 后台线程

    def _start_monitor(self) -> None:
        self._monitor = threading.Thread(target=self._monitor_loop, name="game-monitor")
        self._monitor.daemon = True
        self._monitor.start()

    def _monitor_loop(self) -> None:
        gone_since: float | None = None
        while not self._stop.wait(2.0):
            stats = self.link.seconds_since_last_message()
            if psutil.pid_exists(self.target.pid):
                gone_since = None
                if stats > 20 and self.link.connected:
                    self.log.warning("%.0f 秒未收到游戏端消息（游戏可能被暂停或卡住）", stats)
                continue
            if gone_since is None:
                gone_since = time.time()
                self.log.warning(
                    "游戏进程 %d 已退出，%.0f 秒后自动关闭。", self.target.pid, self.args.idle_exit
                )
                continue
            if time.time() - gone_since >= self.args.idle_exit:
                self.log.info("目标进程已退出，正在清理并关闭。")
                self._request_stop()
                return

    def _check_game_alive(self) -> None:
        if not psutil.pid_exists(self.target.pid):
            self.log.warning("游戏进程 %d 已退出。", self.target.pid)
            self._request_stop()

    def _start_console(self) -> None:
        if not picker.is_interactive():
            self.log.debug("标准输入不是终端，跳过控制台命令读取。")
            return
        self._console = threading.Thread(target=self._console_loop, name="console-keys")
        self._console.daemon = True
        self._console.start()

    def _console_loop(self) -> None:  # pragma: no cover - 交互式输入
        self.log.info("控制台命令：h=隐藏/显示，d=恢复自动停靠，u=卸载代理，s=状态，q=退出")
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            command = line.strip().lower()
            if not command:
                continue
            if command in ("h", "hide", "show", "toggle"):
                if self.overlay is not None:
                    self.overlay.toggle_visible()
                else:
                    self.log.info("当前模式没有悬浮窗。")
            elif command in ("d", "dock"):
                if self.overlay is not None:
                    self.overlay.reset_position()
                else:
                    self.log.info("当前模式没有悬浮窗。")
            elif command in ("u", "unload"):
                self.log.info("执行卸载：%s", self._unload_agent().summary())
            elif command in ("s", "status"):
                self.log.info("代理状态：%s", self._query_status().summary())
            elif command in ("q", "quit", "exit"):
                self._request_stop()
                return
            else:
                self.log.info("未知命令：%s", command)

    # ------------------------------------------------------------ 退出与清理

    def _install_signal_handler(self) -> None:
        def handler(_signum, _frame):
            self.log.info("收到 Ctrl+C，正在退出。")
            self._request_stop()

        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):  # pragma: no cover - 非主线程
            pass

    def _request_stop(self) -> None:
        first = not self._stop.is_set()
        self._stop.set()
        if first and self.overlay is not None:
            # 仅在首次请求时通知界面，避免"界面发起退出 → 回调再请求退出"的自我循环
            self.overlay.request_close()

    def _unload_agent(self):
        with self._io_lock:
            try:
                return self.injector.unload_agent()
            except InjectionError as exc:
                self.log.warning("卸载代理失败：%s", exc)
            except OSError as exc:
                self.log.warning("卸载代理时与目标进程通信失败：%s", exc)
        return None

    def unload_agent(self):
        """卸载游戏内代理（供命令行 --unload 与控制台命令调用）。"""
        return self._unload_agent()

    def _query_status(self):
        with self._io_lock:
            try:
                return self.injector.query_status()
            except InjectionError as exc:
                self.log.warning("查询状态失败：%s", exc)
        return None

    def query_status(self):
        """查询游戏内代理状态（供命令行 --status 与控制台命令调用）。"""
        return self._query_status()

    def _cleanup(self, unload: bool = True) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._stop.set()
        self.log.info("开始清理（共捕获 %d 条对话）…", self._say_count)

        # 1) 先让游戏端卸载，避免清理过程中还在产生新消息
        if unload and psutil.pid_exists(self.target.pid):
            outcome = self._unload_agent()
            if outcome is not None:
                self.log.info("游戏端清理结果：%s", outcome.summary())

        # 2) 停止接收端
        self.link.stop()

        # 3) 销毁界面
        if self.overlay is not None:
            try:
                self.overlay.close()
            except Exception:  # pragma: no cover
                self.log.debug("关闭悬浮窗时出现异常", exc_info=True)
            self.overlay = None

        # 4) 释放远程内存 + 关闭句柄
        try:
            self.injector.close()
        except Exception:  # pragma: no cover
            self.log.exception("关闭注入器时出现异常")

        self.log.info(
            "清理完成：远程内存已释放、进程句柄已关闭、%s",
            "游戏端代理已卸载" if unload else "游戏端代理保持运行",
        )


def main(argv: list[str] | None = None) -> int:
    logs.force_utf8_stdio()  # 先于参数解析：保证 --help 里的中文在 GBK 控制台也能正常输出
    args = build_parser().parse_args(argv)
    log_path = logs.setup_logging(
        level=args.log_level, log_dir=args.log_dir, log_file=args.log_file
    )
    log = get_logger("cli")
    win32api.enable_dpi_awareness()
    log.info(
        "renpy-overlay 启动：本进程 pid=%d，日志文件=%s",
        os.getpid(),
        log_path if log_path else "<仅控制台>",
    )

    if not win32api.IS_WINDOWS:
        log.error("本项目仅支持 Windows。")
        return EXIT_FAILED

    if args.list:
        candidates = discovery.enumerate_candidates(include_all=args.all)
        discovery.print_candidates(candidates)
        return EXIT_OK

    target = _select_target(args)
    if target is None:
        return EXIT_OK

    session = Session(args, target)
    atexit.register(session._cleanup, False)  # 兜底：异常退出路径也不留下句柄与远程内存

    if args.unload:
        outcome = session.unload_agent()
        if outcome is None:
            log.error("卸载失败（无法与目标进程建立注入通道）。")
            return EXIT_FAILED
        log.info("卸载动作完成：%s", outcome.summary())
        return EXIT_OK if outcome.ok else EXIT_FAILED

    if args.status:
        outcome = session.query_status()
        if outcome is None:
            log.error("状态查询失败。")
            return EXIT_FAILED
        log.info("状态：%s", outcome.summary())
        return EXIT_OK if outcome.ok else EXIT_FAILED

    try:
        return session.run()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("收到中断信号，退出中。")
        session._cleanup(unload=True)
        return EXIT_INTERRUPTED
    except InjectionError as exc:
        log.error("注入失败：%s", exc)
        session._cleanup(unload=False)
        return EXIT_FAILED
