"""注入编排：把引导代码送进目标进程并回收资源。

一次注入的完整链路：

1. ``OpenProcess``（CREATE_THREAD + VM_OPERATION + VM_READ + VM_WRITE + QUERY_INFORMATION）
2. 枚举模块，挑出 Python 运行库候选（按"越像越靠前"排序，逐个验证导出）
3. 解析远程 PE 导出表拿到 ``PyGILState_Ensure`` / ``PyRun_SimpleString`` /
   ``PyGILState_Release`` —— 这一步同时确定了目标架构（PE Machine 字段）与 Python 版本
4. 组装引导源码（base64 内嵌 payload 源码 + 配置 JSON + 结果区地址）
5. 分配两块远程内存：桩区（RWX，代码+数据+源码）与结果区（RW）
6. 写入桩 → ``CreateRemoteThread`` → 等待线程结束
7. 读取结果区得到引导代码的 JSON 结果 → 释放桩区与结果区

结果区的意义：``PyRun_SimpleString`` 的返回值只能说明"成功/失败"，而结果区能带回
agent 的详细信息（Python 版本、实际装上了哪几层 Hook、异常类型）。写入由引导代码
在 ``start()`` 返回后**同步**完成，所以"线程结束 → 读取 → 释放"的顺序不存在
use-after-free 风险。反过来，一旦线程等待超时（说明它可能正卡在 PyRun_SimpleString
里），两块内存都不释放，留给进程退出时统一回收 —— 宁可少量泄漏也不去动一块
可能仍被执行的代码页。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from importlib.resources import files

from .pe import PeError, RemotePE, rank_python_modules
from .shellcode import REQUIRED_EXPORTS, X64, X86, build_stub
from .win32api import (
    PAGE_EXECUTE_READWRITE,
    PAGE_READWRITE,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    ModuleEntry,
    RemoteProcess,
    close_handle,
    wait_for_thread,
)

logger = logging.getLogger("renpy_overlay.injector")

RESULT_SIZE = 8192
DEFAULT_TIMEOUT = 15.0
AGENT_MODULE_NAME = "renpy_overlay_agent"

_DLL_VERSION_RE = re.compile(r"^python((?:\d+\.\d+)|(?:\d{2,3}))\.dll$", re.IGNORECASE)


class InjectionError(RuntimeError):
    """注入过程失败（进程不可访问、找不到 Python 运行时、桩执行异常等）。"""


def dll_version_hint(name: str) -> str:
    """从 dll 文件名推出 Python 版本，例如 python310.dll -> 3.10、python27.dll -> 2.7。"""
    match = _DLL_VERSION_RE.match(name or "")
    if not match:
        return ""
    digits = match.group(1)
    if "." in digits:
        return digits
    if len(digits) == 2:
        return f"{digits[0]}.{digits[1]}"
    return f"{digits[0]}.{digits[1:]}"


def agent_source() -> bytes:
    """读取要注入的 agent 源码（UTF-8 字节，供目标进程 compile）。"""
    return files("renpy_overlay.payload").joinpath("agent.py").read_bytes()


# ---------------------------------------------------------------- 引导源码模板
#
# 模板里的 `__RESULT_ADDR__` 会在分配到结果区后被替换成十进制地址。
# 注意 `\\x00` 是刻意的双重转义：工具端把它写成字面量，目标进程侧才会得到 `\x00`。
# 另外这里用 eval(compile(...)) 而不是 exec(...)：在 Python 2 里 `exec(x, g)` 会被
# 解析成 exec 语句 + 元组表达式，运行时报 TypeError，而 eval 对 exec 模式的 code
# 对象在 2.7 与 3.x 上行为一致。

_BOOTSTRAP_TEMPLATE = """# -*- coding: utf-8 -*-
import sys, types, base64, json

_RESULT_ADDR = __RESULT_ADDR__

def _write_result(_obj):
    try:
        import ctypes
        _data = json.dumps(_obj, ensure_ascii=True).encode("utf-8") + b"\\x00"
        ctypes.memmove(_RESULT_ADDR, _data, len(_data))
    except BaseException:
        pass

try:
    _old = sys.modules.get("__AGENT_NAME__")
    if _old is not None:
        try:
            _old.shutdown()
        except BaseException:
            pass
    _mod = types.ModuleType("__AGENT_NAME__")
    sys.modules["__AGENT_NAME__"] = _mod
    eval(compile(base64.b64decode(__AGENT_B64__), "<__AGENT_NAME__>", "exec"), _mod.__dict__)
    _info = _mod.start(base64.b64decode(__CONFIG_B64__).decode("utf-8"))
    _write_result({"ok": True, "stage": "started", "info": _info})
except BaseException as _exc:
    _write_result({"ok": False, "stage": "error", "error": repr(_exc), "type": type(_exc).__name__})
    raise
"""

_ACTION_TEMPLATE = """# -*- coding: utf-8 -*-
import sys, json

_RESULT_ADDR = __RESULT_ADDR__

def _write_result(_obj):
    try:
        import ctypes
        _data = json.dumps(_obj, ensure_ascii=True).encode("utf-8") + b"\\x00"
        ctypes.memmove(_RESULT_ADDR, _data, len(_data))
    except BaseException:
        pass

try:
    _mod = sys.modules.get("__AGENT_NAME__")
    if _mod is None:
        _write_result({"ok": True, "stage": "not_loaded"})
    elif __ACTION__ == "status":
        _write_result({"ok": True, "stage": "status", "info": _mod.status()})
    else:
        _write_result({"ok": True, "stage": "unloaded", "info": _mod.shutdown()})
except BaseException as _exc:
    _write_result({"ok": False, "stage": "error", "error": repr(_exc), "type": type(_exc).__name__})
    raise
"""


def build_agent_bootstrap(config: dict) -> bytes:
    """生成"加载并启动 agent"的引导源码。"""
    config_bytes = json.dumps(config, ensure_ascii=True).encode("utf-8")
    source = _BOOTSTRAP_TEMPLATE
    source = source.replace("__AGENT_NAME__", AGENT_MODULE_NAME)
    source = source.replace("__AGENT_B64__", repr(base64.b64encode(agent_source())))
    source = source.replace("__CONFIG_B64__", repr(base64.b64encode(config_bytes)))
    return source.encode("utf-8")


def build_action_source(action: str) -> bytes:
    """生成卸载 / 状态查询的引导源码。"""
    if action not in ("unload", "status"):
        raise ValueError(f"未知动作：{action}")
    source = _ACTION_TEMPLATE
    source = source.replace("__AGENT_NAME__", AGENT_MODULE_NAME)
    source = source.replace("__ACTION__", repr(action))
    return source.encode("utf-8")


@dataclass
class BootstrapOutcome:
    """一次引导注入的结果。"""

    action: str
    pid: int
    arch: str = "unknown"
    python_dll: str = ""
    python_path: str = ""
    python_version: str = ""
    exports: dict[str, int] = field(default_factory=dict)
    thread_exit_code: int = -1
    finished: bool = False
    timed_out: bool = False
    payload_result: dict | None = None
    raw_result: str = ""
    stub_address: int = 0
    stub_size: int = 0
    released: bool = False
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        """引导代码确实跑完、且 agent 明确报告成功。"""
        if not self.finished or self.timed_out or self.thread_exit_code != 0:
            return False
        return bool(self.payload_result and self.payload_result.get("ok"))

    def summary(self) -> str:
        parts = [
            f"动作={self.action}",
            f"pid={self.pid}",
            f"架构={self.arch}",
            f"Python={self.python_version or '?'}（{self.python_dll}）",
        ]
        if self.timed_out:
            parts.append(f"等待远程线程超时（>{self.elapsed:.1f}s），已放弃释放远程内存")
        elif not self.finished:
            parts.append("远程线程未结束")
        else:
            parts.append(f"线程退出码={self.thread_exit_code}")
        if self.payload_result is not None:
            parts.append("结果=" + json.dumps(self.payload_result, ensure_ascii=False))
        elif self.raw_result:
            parts.append("原始结果=" + self.raw_result[:200])
        parts.append(f"耗时={self.elapsed * 1000:.0f}ms")
        return "；".join(parts)


class Injector:
    """对某个目标进程执行注入，并持有其句柄与远程分配的生命周期。"""

    def __init__(self, pid: int, timeout: float = DEFAULT_TIMEOUT):
        self.pid = int(pid)
        self.timeout = float(timeout)
        self.process: RemoteProcess | None = None
        self.python_entry: ModuleEntry | None = None
        self.python_module: RemotePE | None = None
        self._closed = False

    # ------------------------------------------------------------ 生命周期

    def open(self) -> RemoteProcess:
        if self._closed:
            raise InjectionError("Injector 已关闭")
        if self.process is None or self.process.closed:
            self.process = RemoteProcess.open(self.pid)
        return self.process

    def close(self) -> None:
        """释放远程分配并关闭句柄。可重复调用，进程已退出时也不抛异常。"""
        if self._closed:
            return
        self._closed = True
        if self.process is not None and not self.process.closed:
            remaining = self.process.allocations
            if remaining:
                logger.warning(
                    "释放 %d 块残留的远程内存（通常来自超时的注入）：%s",
                    len(remaining),
                    ", ".join(f"0x{addr:X}" for addr in remaining),
                )
            self.process.free_all()
            self.process.close()
        self.process = None

    def __enter__(self) -> Injector:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------ 目标探测

    def _locate_python(self) -> tuple[RemotePE, ModuleEntry]:
        """找到可用的 Python 运行库模块（同时完成架构识别与导出验证）。"""
        if self.python_module is not None and self.python_entry is not None:
            return self.python_module, self.python_entry

        process = self.open()
        try:
            modules = process.modules()
        except OSError as exc:
            raise InjectionError(f"枚举目标模块失败（可能需要以管理员身份运行）：{exc}") from exc

        candidates = rank_python_modules(modules)
        if not candidates:
            self._dump_modules(modules)
            names = ", ".join(sorted(item.name for item in modules if item.name)[:12])
            raise InjectionError(
                f"目标进程未加载任何 Python 运行库 dll，无法注入。"
                f"已加载模块示例：{names}（完整清单已写入日志 DEBUG）"
            )

        problems: list[str] = []
        for entry in candidates:
            try:
                module = RemotePE.from_process(process, entry)
            except PeError as exc:
                problems.append(f"{entry.name}: {exc}")
                logger.debug("候选 %s 跳过：PE 解析失败：%s", entry.name, exc)
                continue
            if module.header.arch not in (X64, X86):
                problems.append(f"{entry.name}: 架构 {module.header.arch} 暂不支持")
                logger.debug("候选 %s 跳过：架构 %s 暂不支持", entry.name, module.header.arch)
                continue
            resolved = module.resolve(REQUIRED_EXPORTS)
            missing = [name for name, address in resolved.items() if not address]
            if missing:
                problems.append(f"{entry.name}: 缺少导出 {', '.join(missing)}")
                logger.debug("候选 %s 跳过：缺少导出 %s", entry.name, ", ".join(missing))
                continue
            logger.info(
                "选定 Python 运行库：%s（%s，%s）%s",
                entry.name,
                dll_version_hint(entry.name) or "版本未知",
                module.header.arch,
                f"路径={entry.path}" if entry.path else "",
            )
            for name, address in resolved.items():
                logger.debug("  导出 %s -> 0x%X", name, address)
            self.python_module = module
            self.python_entry = entry
            return module, entry

        self._dump_modules(modules)
        raise InjectionError("目标进程内的 Python 运行库不可用：" + "；".join(problems))

    @staticmethod
    def _dump_modules(modules: list[ModuleEntry]) -> None:
        """把完整模块清单写入日志（DEBUG）：命名差异导致找不到运行库时的第一手证据。"""
        logger.debug("目标进程模块清单（共 %d 个）：", len(modules))
        for item in sorted(modules, key=lambda entry: (entry.name or "").lower()):
            logger.debug(
                "  %-32s base=0x%-12X size=0x%-10X %s",
                item.name or "<null>",
                item.base,
                item.size,
                item.path,
            )

    # ------------------------------------------------------------ 执行引导

    def run_bootstrap(self, source: bytes, action: str = "bootstrap") -> BootstrapOutcome:
        started = time.time()
        process = self.open()
        module, entry = self._locate_python()
        exports = module.resolve(REQUIRED_EXPORTS)
        arch = module.header.arch
        outcome = BootstrapOutcome(
            action=action,
            pid=self.pid,
            arch=arch,
            python_dll=entry.name,
            python_path=entry.path,
            python_version=dll_version_hint(entry.name),
            exports={name: int(address or 0) for name, address in exports.items()},
        )

        pointers = {
            "ensure": int(exports["PyGILState_Ensure"] or 0),
            "run": int(exports["PyRun_SimpleString"] or 0),
            "release": int(exports["PyGILState_Release"] or 0),
        }

        result_address = process.alloc(RESULT_SIZE, PAGE_READWRITE)
        body = source.replace(b"__RESULT_ADDR__", str(result_address).encode("ascii"))
        logger.debug("引导源码 %d 字节，结果区 0x%X", len(body), result_address)

        probe = build_stub(arch, pointers, body, 0)
        stub_address = process.alloc(len(probe), PAGE_EXECUTE_READWRITE)
        blob = build_stub(arch, pointers, body, stub_address)
        if len(blob) != len(probe):  # pragma: no cover - 长度只与源码长度有关
            raise InjectionError(f"桩长度不一致：{len(blob)} != {len(probe)}")
        process.write(stub_address, blob)
        outcome.stub_address = stub_address
        outcome.stub_size = len(blob)
        logger.debug("桩已写入 0x%X（%d 字节），准备创建远程线程", stub_address, len(blob))

        thread_handle = process.create_remote_thread(stub_address)
        try:
            wait_result, exit_code = wait_for_thread(thread_handle, int(self.timeout * 1000))
        finally:
            close_handle(thread_handle)

        outcome.finished = wait_result == WAIT_OBJECT_0
        outcome.timed_out = wait_result == WAIT_TIMEOUT
        outcome.thread_exit_code = exit_code

        outcome.raw_result = self._read_result(process, result_address)
        outcome.payload_result = self._parse_result(outcome.raw_result)
        outcome.elapsed = time.time() - started

        if outcome.timed_out:
            logger.error(
                "远程线程在 %.1fs 内未结束，暂不释放远程内存（0x%X / 0x%X），"
                "以免动到仍可能在被执行的代码页。游戏若卡死请直接结束进程。",
                self.timeout,
                stub_address,
                result_address,
            )
        else:
            freed_stub = process.free(stub_address)
            freed_result = process.free(result_address)
            outcome.released = bool(freed_stub and freed_result)

        logger.log(
            logging.INFO if outcome.ok else logging.WARNING, "注入结果：%s", outcome.summary()
        )
        return outcome

    def inject_agent(self, config: dict) -> BootstrapOutcome:
        return self.run_bootstrap(build_agent_bootstrap(config), action="inject")

    def unload_agent(self) -> BootstrapOutcome:
        return self.run_bootstrap(build_action_source("unload"), action="unload")

    def query_status(self) -> BootstrapOutcome:
        return self.run_bootstrap(build_action_source("status"), action="status")

    # ------------------------------------------------------------ 结果读取

    @staticmethod
    def _read_result(process: RemoteProcess, address: int) -> str:
        try:
            raw = process.read(address, RESULT_SIZE)
        except OSError as exc:
            logger.debug("读取结果区失败：%s", exc)
            return ""
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()

    @staticmethod
    def _parse_result(raw: str) -> dict | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            logger.debug("结果区内容不是合法 JSON：%.200s", raw)
            return None
        return parsed if isinstance(parsed, dict) else None
