"""日志基础设施。

设计要点：
- 控制台只输出 INFO 及以上，保持交互清爽；文件日志记录 DEBUG 全量信息，
  便于排查注入与 Hook 过程中的细节。
- 目标进程内的 agent 通过 IPC 把自身日志回灌到同一个 logger（前缀 ``[game]``），
  这样"注入端"和"游戏端"的日志出现在同一份时间线上，时序问题一目了然。
- 统一日志管理：接管三类绕过 logging 直接打印到 stderr 的诊断输出 ——
  Qt 诊断消息（qInstallMessageHandler）、被 Python 忽略的异常（
  sys.unraisablehook，如 ctypes 回调异常的 "Exception ignored"）与
  Python warnings（captureWarnings），全部转为标准 DEBUG 级日志，
  受 ``--log-level`` 与文件全量日志统一管理。
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

LOGGER_NAME = "renpy_overlay"

_CONSOLE_FORMAT = "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s"
_CONSOLE_DATEFMT = "%H:%M:%S"
_FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [pid:%(process)d tid:%(threadName)s] %(name)s:%(lineno)d - %(message)s"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 游戏端回灌日志的级别映射
_LEVEL_BY_NAME = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_configured = False
_log_file: Path | None = None


def force_utf8_stdio() -> None:
    """让控制台输出在 GBK 代码页下也不会因为中文/特殊字符抛 UnicodeEncodeError。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 取决于终端
                pass


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path = "logs",
    log_file: str | Path | None = None,
    console: bool = True,
) -> Path | None:
    """初始化根 logger。重复调用是幂等的，返回本次使用的日志文件路径。"""
    global _configured, _log_file
    if _configured:
        return _log_file

    force_utf8_stdio()
    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if console:
        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setLevel(_LEVEL_BY_NAME.get(str(level).lower(), logging.INFO))
        stream_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _CONSOLE_DATEFMT))
        root.addHandler(stream_handler)

    target = (
        Path(log_file)
        if log_file
        else Path(log_dir) / f"renpy_overlay_{time.strftime('%Y%m%d_%H%M%S')}.log"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8", delay=True)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _FILE_DATEFMT))
        root.addHandler(file_handler)
        _log_file = target
    except OSError:
        # 日志目录不可写不应导致程序无法启动
        _log_file = None

    _configured = True
    # 统一日志管理：接管三类绕过 logging 的直接输出（warnings / 被忽略
    # 异常 / Qt 诊断消息），全部转为本命名空间下的标准级别日志
    logging.captureWarnings(True)
    logging.getLogger("py.warnings").parent = root
    install_unraisable_hook()
    install_qt_message_handler()
    root.debug("日志系统已初始化：level=%s file=%s", level, _log_file)
    return _log_file


def get_logger(name: str = "") -> logging.Logger:
    """获取子 logger，例如 ``get_logger("injector")`` -> ``renpy_overlay.injector``。"""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def _qt_message_handler(msg_type, _context, message) -> None:
    """Qt 诊断消息 → logging 转发（按 QtMsgType 映射标准级别）。

    映射键用 QtMsgType 枚举对象本身：PyQt6 不同版本的枚举 ``str()``
    形态不一致（名称或纯数值），按字符串解析会间歇回退 INFO（实测）。
    """
    try:
        from PyQt6.QtCore import QtMsgType

        level = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }.get(msg_type, logging.INFO)
    except ImportError:  # pragma: no cover - PyQt6 为硬依赖，防御性降级
        level = logging.INFO
    get_logger("qt").log(level, "%s", message)


def install_qt_message_handler() -> None:
    """把 Qt 诊断消息（qDebug/qWarning 等）转发到本日志系统。

    PyQt6 默认把 Qt 的 debug/warning 直接打到 stderr，绕过 logging——
    它们正是"默认级别下运行输出仍出现 DEBUG 级诊断内容"的主要现实来源
    （如 QObject::startTimer 线程警告）。安装后按类型映射为 DEBUG/INFO/
    WARNING/ERROR/CRITICAL，统一受 ``--log-level`` 与文件全量日志管理；
    未安装 PyQt6 的环境静默跳过。QtFatalMsg 转发后 Qt 仍会自行 abort。
    """
    try:
        from PyQt6.QtCore import qInstallMessageHandler
    except ImportError:  # pragma: no cover - PyQt6 为硬依赖，防御性降级
        return
    qInstallMessageHandler(_qt_message_handler)


def _unraisable_to_log(unraisable) -> None:
    """sys.unraisablehook 替身：被忽略的异常转为 DEBUG 日志（含堆栈）。"""
    get_logger("unraisable").debug(
        "被忽略的异常：object=%r",
        unraisable.object,
        exc_info=(
            unraisable.exc_type,
            unraisable.exc_value,
            unraisable.exc_traceback,
        ),
    )


def install_unraisable_hook() -> None:
    """接管被 Python 忽略的异常，转为 DEBUG 日志（不再打印到 stderr）。

    ctypes 回调抛出的异常由 _ctypes 经 PyErr_WriteUnraisable 上报，默认
    只打印 "Exception ignored ..." 到 stderr，不进 logging 也无法留档
    （实测缺陷来源）；接管 sys.unraisablehook 后统一转为 DEBUG 日志，
    与"忽略"语义对齐，同时消除并行的 stderr 打印路径。
    """
    sys.unraisablehook = _unraisable_to_log


def log_file_path() -> Path | None:
    return _log_file


def log_from_game(level: str, message: str) -> None:
    """把游戏进程内 agent 上报的日志写入同一个 logger。"""
    get_logger("game").log(_LEVEL_BY_NAME.get(str(level).lower(), logging.INFO), "%s", message)
