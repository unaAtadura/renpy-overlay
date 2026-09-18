"""Win32 底层封装，分两段职责：

1. **ctypes**：进程句柄、远程内存、远程线程、模块枚举 —— 注入所需的最小原语集合。
   所有函数失败时抛出 :class:`OSError`，错误信息附带 Win32 错误码，便于定位
   "访问被拒绝(5)" / "部分读取完成(299)" 这类注入期典型问题。
2. **pywin32**：窗口枚举、窗口状态、窗口定位 —— 目标筛选与悬浮窗跟随所需。
   pywin32 采用惰性导入，保证在没有安装它时（或做纯离线单测时）模块仍可 import。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger("renpy_overlay.win32")

_IS_WINDOWS = sys.platform == "win32"
#: 公开别名，供上层模块做平台判断
IS_WINDOWS = _IS_WINDOWS

# ---------------------------------------------------------------- 常量

PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020

INJECTION_ACCESS = (
    PROCESS_CREATE_THREAD
    | PROCESS_QUERY_INFORMATION
    | PROCESS_VM_OPERATION
    | PROCESS_VM_READ
    | PROCESS_VM_WRITE
)
QUERY_ACCESS = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40

LIST_MODULES_32BIT = 0x01
LIST_MODULES_64BIT = 0x02
LIST_MODULES_ALL = 0x03

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

ERROR_ACCESS_DENIED = 5
ERROR_PARTIAL_COPY = 299
ERROR_INVALID_PARAMETER = 87

_MAX_PATH = 1024

# ---------------------------------------------------------------- ctypes 声明

if _IS_WINDOWS:

    class MODULEINFO(ctypes.Structure):
        _fields_ = [
            ("lpBaseOfDll", ctypes.c_void_p),
            ("SizeOfImage", wintypes.DWORD),
            ("EntryPoint", ctypes.c_void_p),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    _kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    _kernel32.VirtualFreeEx.restype = wintypes.BOOL
    _kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.ReadProcessMemory.restype = wintypes.BOOL
    _kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.WriteProcessMemory.restype = wintypes.BOOL
    _kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeThread.restype = wintypes.BOOL
    _kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    _kernel32.IsWow64Process.restype = wintypes.BOOL

    _psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    ]
    _psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    _psapi.GetModuleBaseNameW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _psapi.GetModuleBaseNameW.restype = wintypes.DWORD
    _psapi.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    _psapi.GetModuleInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(MODULEINFO),
        wintypes.DWORD,
    ]
    _psapi.GetModuleInformation.restype = wintypes.BOOL
else:  # pragma: no cover - 便于在非 Windows 环境做静态检查
    _kernel32 = None
    _psapi = None


def _k32():
    if _kernel32 is None:
        raise RuntimeError("renpy-overlay 的注入功能仅支持 Windows")
    return _kernel32


def _psapi_dll():
    if _psapi is None:
        raise RuntimeError("renpy-overlay 的注入功能仅支持 Windows")
    return _psapi


def _win_error(what: str, pid: int | None = None) -> OSError:
    code = ctypes.get_last_error()
    detail = ctypes.FormatError(code).strip() if code else "no Win32 error reported"
    prefix = f"{what} (pid={pid})" if pid is not None else what
    return OSError(0, f"{prefix} 失败：Win32 错误 {code} - {detail}")


def _win32gui():
    try:
        import win32gui  # noqa: PLC0415 - 惰性导入
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 pywin32（uv sync 后重试）") from exc
    return win32gui


def _win32con():
    try:
        import win32con  # noqa: PLC0415 - 惰性导入
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 pywin32（uv sync 后重试）") from exc
    return win32con


def _win32process():
    """窗口 → 进程查询在 win32process 里（win32gui 没有 GetWindowThreadProcessId）。"""
    try:
        import win32process  # noqa: PLC0415 - 惰性导入
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 pywin32（uv sync 后重试）") from exc
    return win32process


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class ModuleEntry:
    """目标进程内的一个已加载模块。"""

    name: str
    path: str
    base: int
    size: int


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])


# ---------------------------------------------------------------- 远程进程


class RemoteProcess:
    """对目标进程句柄的薄封装，负责句柄与远程分配的生命周期。"""

    def __init__(self, handle: int, pid: int):
        self._handle = handle
        self.pid = pid
        self._allocations: list[int] = []

    # -- 生命周期 -------------------------------------------------

    @classmethod
    def open(cls, pid: int, access: int = INJECTION_ACCESS) -> RemoteProcess:
        ctypes.set_last_error(0)
        handle = _k32().OpenProcess(access, False, int(pid))
        if not handle:
            raise _win_error(f"OpenProcess(0x{access:04X})", pid)
        logger.debug("已打开进程句柄 pid=%d handle=0x%X access=0x%04X", pid, handle, access)
        return cls(handle, pid)

    @property
    def handle(self) -> int:
        return self._handle or 0

    @property
    def closed(self) -> bool:
        return not self._handle

    def close(self) -> None:
        if self._handle:
            _k32().CloseHandle(self._handle)
            logger.debug("已关闭进程句柄 pid=%d", self.pid)
            self._handle = 0

    def __enter__(self) -> RemoteProcess:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- 内存 -----------------------------------------------------

    def read(self, address: int, size: int, chunk: int = 0x1000) -> bytes:
        """分块读远程内存；任一块失败即抛错（避免静默拿到半截数据）。"""
        if size <= 0:
            return b""
        out = bytearray()
        remaining = size
        cursor = address
        while remaining > 0:
            take = min(remaining, chunk)
            out += self._read_once(cursor, take)
            cursor += take
            remaining -= take
        return bytes(out)

    def _read_once(self, address: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ctypes.set_last_error(0)
        ok = _k32().ReadProcessMemory(
            self._handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)
        )
        if not ok or read.value != size:
            raise _win_error(f"ReadProcessMemory(0x{address:X}, {size}B)", self.pid)
        return buffer.raw

    def read_cstring(self, address: int, max_len: int = 512) -> bytes:
        data = self.read(address, max_len)
        end = data.find(b"\x00")
        return data if end < 0 else data[:end]

    def write(self, address: int, data: bytes) -> int:
        if not data:
            return 0
        buffer = ctypes.create_string_buffer(bytes(data), len(data))
        written = ctypes.c_size_t(0)
        ctypes.set_last_error(0)
        ok = _k32().WriteProcessMemory(
            self._handle, ctypes.c_void_p(address), buffer, len(data), ctypes.byref(written)
        )
        if not ok or written.value != len(data):
            raise _win_error(f"WriteProcessMemory(0x{address:X}, {len(data)}B)", self.pid)
        return written.value

    def alloc(
        self,
        size: int,
        protect: int = PAGE_READWRITE,
        track: bool = True,
        address: int | None = None,
    ) -> int:
        ctypes.set_last_error(0)
        result = _k32().VirtualAllocEx(
            self._handle,
            ctypes.c_void_p(address) if address else None,
            size,
            MEM_COMMIT | MEM_RESERVE,
            protect,
        )
        if not result:
            raise _win_error(f"VirtualAllocEx({size}B, protect=0x{protect:X})", self.pid)
        if track:
            self._allocations.append(result)
        logger.debug("远程分配 0x%X (%d 字节, protect=0x%X)", result, size, protect)
        return result

    def free(self, address: int) -> bool:
        ctypes.set_last_error(0)
        ok = bool(_k32().VirtualFreeEx(self._handle, ctypes.c_void_p(address), 0, MEM_RELEASE))
        if ok:
            if address in self._allocations:
                self._allocations.remove(address)
            logger.debug("已释放远程分配 0x%X", address)
        else:
            logger.debug(
                "释放远程分配 0x%X 失败：%s", address, _win_error("VirtualFreeEx", self.pid)
            )
        return ok

    def free_all(self) -> int:
        """释放本对象登记的全部远程分配，返回成功数量。进程已退出时不抛异常。"""
        released = 0
        for address in list(self._allocations):
            try:
                if self.free(address):
                    released += 1
            except OSError:  # pragma: no cover - 进程已死
                self._allocations.remove(address)
        return released

    @property
    def allocations(self) -> tuple[int, ...]:
        return tuple(self._allocations)

    # -- 模块 -----------------------------------------------------

    def _enum_modules(self, filter_flag: int) -> list[int]:
        """枚举模块基址；缓冲区不足时按 lpcbNeeded 扩张后重试。"""
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        slot_count = 1024
        while slot_count <= 65536:
            array = (ctypes.c_void_p * slot_count)()
            capacity = ctypes.sizeof(array)
            needed = wintypes.DWORD(0)
            ctypes.set_last_error(0)
            ok = _psapi_dll().EnumProcessModulesEx(
                self._handle, array, capacity, ctypes.byref(needed), filter_flag
            )
            if not ok:
                code = ctypes.get_last_error()
                if (
                    code in (ERROR_PARTIAL_COPY, ERROR_INVALID_PARAMETER)
                    and filter_flag != LIST_MODULES_ALL
                ):
                    raise _win_error(f"EnumProcessModulesEx(filter=0x{filter_flag:X})", self.pid)
                if code in (ERROR_PARTIAL_COPY, ERROR_INVALID_PARAMETER):
                    # 64 位工具枚举 WOW64 目标时，LIST_MODULES_ALL 可能失败，改用 32 位筛选
                    logger.debug(
                        "pid=%d LIST_MODULES_ALL 枚举失败(%d)，回退到 LIST_MODULES_32BIT",
                        self.pid,
                        code,
                    )
                    return self._enum_modules(LIST_MODULES_32BIT)
                raise _win_error("EnumProcessModulesEx", self.pid)
            if needed.value <= capacity:
                break
            slot_count = needed.value // pointer_size + 64
        else:  # pragma: no cover - 模块数量异常多
            raise OSError(0, f"pid={self.pid} 模块数量超出预期上限")
        total = min(needed.value // pointer_size, slot_count)
        return [int(array[i]) for i in range(total) if array[i]]

    def modules(self) -> list[ModuleEntry]:
        bases = self._enum_modules(LIST_MODULES_ALL)
        name_buffer = ctypes.create_unicode_buffer(_MAX_PATH)
        path_buffer = ctypes.create_unicode_buffer(_MAX_PATH)
        entries: list[ModuleEntry] = []
        for base in bases:
            name_len = _psapi_dll().GetModuleBaseNameW(
                self._handle, ctypes.c_void_p(base), name_buffer, _MAX_PATH
            )
            _psapi_dll().GetModuleFileNameExW(
                self._handle, ctypes.c_void_p(base), path_buffer, _MAX_PATH
            )
            name = name_buffer.value if name_len else ""
            module_info = MODULEINFO()
            size = 0
            if _psapi_dll().GetModuleInformation(
                self._handle,
                ctypes.c_void_p(base),
                ctypes.byref(module_info),
                ctypes.sizeof(module_info),
            ):
                size = int(module_info.SizeOfImage)
            entries.append(
                ModuleEntry(
                    name=name,
                    path=path_buffer.value or "",
                    base=int(base),
                    size=size,
                )
            )
        logger.debug("pid=%d 共枚举到 %d 个模块", self.pid, len(entries))
        return entries

    # -- 线程 -----------------------------------------------------

    def is_wow64(self) -> bool:
        """目标进程是否为 WOW64（32 位进程运行在 64 位系统上）。"""
        result = wintypes.BOOL(False)
        ctypes.set_last_error(0)
        if not _k32().IsWow64Process(self._handle, ctypes.byref(result)):
            raise _win_error("IsWow64Process", self.pid)
        return bool(result.value)

    def create_remote_thread(self, start_address: int, parameter: int = 0) -> int:
        thread_id = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        handle = _k32().CreateRemoteThread(
            self._handle,
            None,
            0,
            ctypes.c_void_p(start_address),
            ctypes.c_void_p(parameter) if parameter else None,
            0,
            ctypes.byref(thread_id),
        )
        if not handle:
            raise _win_error(f"CreateRemoteThread(0x{start_address:X})", self.pid)
        logger.debug("已创建远程线程 tid=%d start=0x%X", thread_id.value, start_address)
        return int(handle)


def wait_for_thread(handle: int, timeout_ms: int = 15000) -> tuple[int, int]:
    """等待远程线程结束，返回 ``(wait_result, exit_code)``；超时返回 WAIT_TIMEOUT。"""
    ctypes.set_last_error(0)
    result = _k32().WaitForSingleObject(ctypes.c_void_p(handle), timeout_ms)
    exit_code = wintypes.DWORD(0xFFFFFFFF)
    if result == WAIT_OBJECT_0:
        if not _k32().GetExitCodeThread(ctypes.c_void_p(handle), ctypes.byref(exit_code)):
            raise _win_error("GetExitCodeThread")
    return result, exit_code.value


def close_handle(handle: int) -> None:
    if handle:
        _k32().CloseHandle(ctypes.c_void_p(handle))


# ---------------------------------------------------------------- 窗口（pywin32）


def list_windows() -> list[WindowInfo]:
    """枚举所有可见的顶层窗口（跳过 tool window 与尺寸过小的窗口）。"""
    gui = _win32gui()
    con = _win32con()
    results: list[WindowInfo] = []

    def callback(hwnd, _extra):
        if not gui.IsWindowVisible(hwnd):
            return True
        if gui.GetParent(hwnd):
            return True
        if gui.GetWindow(hwnd, con.GW_OWNER):
            return True
        try:
            left, top, right, bottom = gui.GetWindowRect(hwnd)
        except Exception:  # pragma: no cover - 窗口在枚举过程中销毁
            return True
        if (right - left) < 120 or (bottom - top) < 80:
            return True
        _thread_id, pid = _win32process().GetWindowThreadProcessId(hwnd)
        results.append(
            WindowInfo(
                hwnd=int(hwnd),
                pid=int(pid),
                title=gui.GetWindowText(hwnd),
                rect=(int(left), int(top), int(right), int(bottom)),
            )
        )
        return True

    gui.EnumWindows(callback, None)
    return results


def find_main_window(pid: int, min_width: int = 200, min_height: int = 120) -> int | None:
    """按 PID 找到游戏主窗口（面积最大的那个可见顶层窗口）。"""
    candidates = [
        window
        for window in list_windows()
        if window.pid == pid and window.width >= min_width and window.height >= min_height
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda item: item.width * item.height)
    return best.hwnd


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return _win32gui().GetWindowRect(hwnd)


def ltrb_to_xywh(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """``GetWindowRect`` 的 (left, top, right, bottom) 换算为 (x, y, 宽, 高)。

    ``move_window`` / ``SetWindowPos`` 接收的是位置 + 尺寸，直接展开 ltrb
    会把 right/bottom 当宽高传入（窗口尺寸错乱）。宽高下限钳到 1。
    """
    left, top, right, bottom = rect
    return (int(left), int(top), max(1, int(right) - int(left)), max(1, int(bottom) - int(top)))


def window_xywh(hwnd: int) -> tuple[int, int, int, int]:
    """窗口几何 ``(x, y, 宽, 高)``（物理像素，``move_window`` 的参数格式）。"""
    return ltrb_to_xywh(_win32gui().GetWindowRect(hwnd))


def is_window_valid(hwnd: int) -> bool:
    return bool(_win32gui().IsWindow(hwnd))


def double_click_time_ms() -> int:
    """系统的双击时间间隔（毫秒）。单击/双击区分要等这么久才确认单击。"""
    if not _IS_WINDOWS:  # pragma: no cover
        return 500
    try:
        value = int(ctypes.windll.user32.GetDoubleClickTime())
    except (AttributeError, OSError):  # pragma: no cover
        return 500
    return value if value > 0 else 500


def is_minimized(hwnd: int) -> bool:
    gui = _win32gui()
    return bool(gui.IsIconic(hwnd)) or not bool(gui.IsWindowVisible(hwnd))


def toplevel_hwnd(widget_id: int) -> int:
    """把 Tk 子窗口 id 归一化为真正的顶层 HWND。"""
    gui = _win32gui()
    parent = gui.GetParent(widget_id)
    return int(parent) if parent else int(widget_id)


def move_window(
    hwnd: int,
    x: int,
    y: int,
    width: int,
    height: int,
    topmost: bool = True,
    resize: bool = True,
) -> None:
    gui = _win32gui()
    con = _win32con()
    flags = con.SWP_NOACTIVATE
    if resize:
        flags |= con.SWP_SHOWWINDOW
    else:
        flags |= con.SWP_NOSIZE
    insert_after = con.HWND_TOPMOST if topmost else con.HWND_NOTOPMOST
    gui.SetWindowPos(hwnd, insert_after, int(x), int(y), int(width), int(height), flags)


def set_topmost(hwnd: int) -> None:
    """重申窗口的 TOPMOST 层级（不改位置/尺寸/焦点）。

    独占全屏的游戏窗口被激活/获得前台焦点后，可能盖住其它 topmost 窗口；
    必须在之后重新执行一次 ``SetWindowPos(HWND_TOPMOST)`` 才能恢复置顶。
    """
    gui = _win32gui()
    con = _win32con()
    flags = con.SWP_NOMOVE | con.SWP_NOSIZE | con.SWP_NOACTIVATE
    gui.SetWindowPos(hwnd, con.HWND_TOPMOST, 0, 0, 0, 0, flags)


def enable_dpi_awareness() -> str:
    """开启 Per-Monitor DPI 感知，保证 GetWindowRect 拿到物理像素、与游戏窗口对齐。"""
    if not _IS_WINDOWS:  # pragma: no cover
        return "unsupported"
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except (AttributeError, OSError):  # pragma: no cover - Windows 8.1 之前
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "system"
        except (AttributeError, OSError):
            return "none"
