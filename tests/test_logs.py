"""日志系统的离线验证：级别传递、通道分工与 CLI 默认值防回归。

背景缺陷排查（2026-09-22）：用户报告默认级别下"运行输出"出现 DEBUG——
实测证明控制台链路正确：``setup_logging(level="INFO")`` 时 StreamHandler
级别为 INFO 并过滤 DEBUG，DEBUG 只出现在按设计记录全量的文件日志中。
本文件把这一行为固化为防回归断言：默认级别控制台无 DEBUG、显式 DEBUG
放行、文件 handler 保持 DEBUG 全量、非法级别回退 INFO、CLI 默认值。

二次排查（同日）：renpy_overlay_20260922_235211.log 第 80-81 行出现
无日志头的裸行——Qt 的 DPI 警告本身带两行文本，Formatter 只给记录
首行加前缀，续行看似绕过统一日志系统直接写入。修复为消息内换行
转义（_SingleLineFormatter），本文件同时固化"一条记录严格占一行"。
"""

from __future__ import annotations

import logging
import sys

import pytest

from renpy_overlay import logs


@pytest.fixture()
def fresh_logging(monkeypatch):
    """隔离模块级幂等守卫与已挂 handler，测试后移除测试 handler。"""
    monkeypatch.setattr(logs, "_configured", False)
    monkeypatch.setattr(logs, "_log_file", None)
    root = logs.get_logger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    yield root
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _handler_levels(root: logging.Logger) -> tuple[int, int]:
    """返回（控制台 handler 级别, 文件 handler 级别），顺序无关按类型取。"""
    stream = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert len(stream) == 1 and len(file_handlers) == 1
    return stream[0].level, file_handlers[0].level


def test_default_console_level_is_info_and_filters_debug(
    fresh_logging, capsys, tmp_path
):
    """默认级别（INFO）下控制台不输出 DEBUG（缺陷排查结论的防回归）。"""
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    log = logs.get_logger("probe")
    log.debug("DEBUG-SHOULD-NOT-APPEAR")
    log.info("INFO-SHOULD-APPEAR")
    out = capsys.readouterr().out
    assert "DEBUG-SHOULD-NOT-APPEAR" not in out
    assert "INFO-SHOULD-APPEAR" in out


def test_explicit_debug_level_enables_console_debug(fresh_logging, capsys, tmp_path):
    """--log-level DEBUG 语义：控制台放行 DEBUG。"""
    logs.setup_logging(level="DEBUG", log_dir=tmp_path)
    logs.get_logger("probe").debug("DEBUG-SHOULD-APPEAR")
    assert "DEBUG-SHOULD-APPEAR" in capsys.readouterr().out


def test_file_handler_keeps_debug_full_log_regardless_of_level(fresh_logging, tmp_path):
    """文件日志按设计记录 DEBUG 全量（不受 --log-level 影响）；控制台仍 INFO。"""
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    console_level, file_level = _handler_levels(fresh_logging)
    assert console_level == logging.INFO
    assert file_level == logging.DEBUG


def test_unknown_level_falls_back_to_info_console(fresh_logging, capsys, tmp_path):
    """未知识别串回退 INFO：控制台不放行 DEBUG（与 _LEVEL_BY_NAME 缺省一致）。"""
    logs.setup_logging(level="not-a-level", log_dir=tmp_path)
    log = logs.get_logger("probe")
    log.debug("DEBUG-FALLBACK")
    log.info("INFO-FALLBACK")
    out = capsys.readouterr().out
    assert "DEBUG-FALLBACK" not in out
    assert "INFO-FALLBACK" in out


def test_cli_default_log_level_is_info():
    """CLI 默认 --log-level 必须是 INFO（根因链路第一环防回归）。"""
    from renpy_overlay.cli import build_parser

    assert build_parser().parse_args([]).log_level == "INFO"


# ---- 诊断输出纳管：Qt 消息 / 被忽略异常 / Python warnings -----------------------


class _ListHandler(logging.Handler):
    """收集经过 root logger 的记录（caplog 无法穿透 propagate=False）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_setup_logging_installs_diagnostic_hooks(fresh_logging, tmp_path):
    """setup_logging 必须接管三类直接输出：warnings、unraisable、Qt 消息。"""
    original_hook = sys.unraisablehook
    try:
        logs.setup_logging(level="INFO", log_dir=tmp_path)
        assert sys.unraisablehook is logs._unraisable_to_log  # 被忽略异常已接管
        assert (
            logging.getLogger("py.warnings").parent is fresh_logging
        )  # warnings 已路由进本命名空间
    finally:
        sys.unraisablehook = original_hook


def test_unraisable_hook_routes_to_debug_log(fresh_logging, tmp_path):
    """被忽略异常（如 ctypes 回调异常）转 DEBUG 日志，不再打 stderr。"""
    import types

    original = sys.unraisablehook
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    collector = _ListHandler()
    fresh_logging.addHandler(collector)
    try:
        exc = ValueError("UNRAISABLE-PROBE")
        sys.unraisablehook(
            types.SimpleNamespace(
                exc_type=ValueError,
                exc_value=exc,
                exc_traceback=exc.__traceback__,
                object=None,
            )
        )
    finally:
        sys.unraisablehook = original
        fresh_logging.removeHandler(collector)
    hits = [r for r in collector.records if r.name == "renpy_overlay.unraisable"]
    assert hits and hits[0].levelno == logging.DEBUG
    assert hits[0].exc_info is not None and hits[0].exc_info[1] is not None
    assert "UNRAISABLE-PROBE" in str(hits[0].exc_info[1])


def test_qt_message_handler_maps_levels(fresh_logging, tmp_path):
    """Qt 诊断消息按 QtMsgType 映射标准级别（DEBUG 转发在默认级别下被控制台过滤）。"""
    from PyQt6.QtCore import QtMsgType

    logs.setup_logging(level="INFO", log_dir=tmp_path)
    collector = _ListHandler()
    fresh_logging.addHandler(collector)
    try:
        # PyQt6 的 QMessageLogContext 不可实例化；转发函数不使用 context，传 None
        logs._qt_message_handler(QtMsgType.QtDebugMsg, None, "QT-DEBUG-PROBE")
        logs._qt_message_handler(QtMsgType.QtWarningMsg, None, "QT-WARN-PROBE")
    finally:
        fresh_logging.removeHandler(collector)
    debug_hits = [r for r in collector.records if "QT-DEBUG-PROBE" in r.getMessage()]
    warn_hits = [r for r in collector.records if "QT-WARN-PROBE" in r.getMessage()]
    assert debug_hits and debug_hits[0].levelno == logging.DEBUG
    assert debug_hits[0].name == "renpy_overlay.qt"
    assert warn_hits and warn_hits[0].levelno == logging.WARNING


def test_python_warnings_captured_into_logging(fresh_logging, tmp_path):
    """setup 后 py.warnings 路由进本命名空间（不再直接打 stderr）。

    仅断言路由通路（手动 py.warnings.warning → collector）：
    warnings.warn → showwarning 的端到端在 pytest 下被其 warnings 插件
    接管，全局替换状态不可自动化断言（留手动验证）。
    """
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    collector = _ListHandler()
    fresh_logging.addHandler(collector)
    try:
        logging.getLogger("py.warnings").warning("WARNINGS-ROUTE-PROBE")
    finally:
        fresh_logging.removeHandler(collector)
    hits = [
        r
        for r in collector.records
        if r.name == "py.warnings" and "WARNINGS-ROUTE-PROBE" in r.getMessage()
    ]
    assert hits and hits[0].levelno == logging.WARNING


# ---- 多行消息单行化：杜绝文件中的无头裸行（2026-09-22 二次排查防回归） ---------


def test_multiline_message_writes_single_physical_line(fresh_logging, tmp_path):
    """多行消息（Qt 两行 DPI 警告类）在文件中不得产生无日志头的裸行。

    缺陷实录（renpy_overlay_20260922_235211.log 第 80-81 行）：Qt 的
    DPI 警告消息含内嵌换行，Formatter 只给记录首行加前缀，第二行
    裸奔落盘，看似绕过统一日志系统直接写入。
    """
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    logs.get_logger("qt").warning(
        "SetProcessDpiAwarenessContext() failed: 拒绝访问。\n"
        "Qt's default DPI awareness context is "
        "DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2."
    )
    lines = logs.log_file_path().read_text(encoding="utf-8").splitlines()
    hits = [ln for ln in lines if "SetProcessDpiAwarenessContext" in ln]
    assert len(hits) == 1  # 续行不再裸奔
    assert "PER_MONITOR_AWARE_V2" in hits[0]  # 续行内容并入同一物理行
    assert "\\n" in hits[0]  # 换行以转义形式保留，信息不丢失


def test_multiline_console_output_also_single_line(fresh_logging, capsys, tmp_path):
    """控制台同样单行：多行消息不产生无头续行。"""
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    logs.get_logger("probe").info("LINE-ONE\nLINE-TWO")
    out = capsys.readouterr().out
    assert "LINE-ONE\\nLINE-TWO" in out
    assert out.count("\n") == 1  # 一条记录只占一行（含行终止符）


def test_unraisable_stacktrace_stays_single_line_in_file(fresh_logging, tmp_path):
    """带 exc_info 的记录（多行堆栈）同样单行落盘，不产生裸行。"""
    import types

    original = sys.unraisablehook
    logs.setup_logging(level="INFO", log_dir=tmp_path)
    try:
        try:
            raise ValueError("STACK-PROBE")
        except ValueError:
            exc = sys.exc_info()
        sys.unraisablehook(
            types.SimpleNamespace(
                exc_type=exc[0], exc_value=exc[1], exc_traceback=exc[2], object=None
            )
        )
    finally:
        sys.unraisablehook = original
    lines = logs.log_file_path().read_text(encoding="utf-8").splitlines()
    hits = [ln for ln in lines if "STACK-PROBE" in ln]
    assert len(hits) == 1  # 堆栈不产生裸行
    assert "renpy_overlay.unraisable" in hits[0]
    assert "Traceback" in hits[0]  # 堆栈内容也在同一物理行
