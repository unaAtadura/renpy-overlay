"""远程 PE 解析的离线验证。

思路：把"读取接口"换成读本进程内存，于是可以直接拿系统 dll 当样本，
用 ``GetProcAddress`` 的结果当基准答案来交叉验证解析器 —— 不需要真的去注入任何进程。
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from renpy_overlay.pe import (
    ARCH_BY_MACHINE,
    PYTHON_DLL_PATTERN,
    PeError,
    RemotePE,
    is_python_runtime_dll,
    load_self_module,
    rank_python_modules,
    self_reader,
)
from renpy_overlay.win32api import ModuleEntry

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="PE 解析依赖本进程内存读取")


@pytest.fixture(scope="module")
def kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    library.GetProcAddress.restype = ctypes.c_void_p
    base, _size = load_self_module("kernel32.dll")
    module = RemotePE(self_reader(base), base=base, name="kernel32.dll")
    return library, base, module


def test_header_parsed(kernel32):
    _library, base, module = kernel32
    assert base > 0
    assert module.header.machine in ARCH_BY_MACHINE
    assert module.header.arch in ("x64", "x86")
    assert module.header.size_of_image > 0
    assert module.header.export_dir_rva > 0
    assert module.header.export_dir_size > 0
    assert module.header.num_sections > 0


def test_export_name_count(kernel32):
    _library, _base, module = kernel32
    assert module.export_name_count > 100, "kernel32 的具名导出数量应该在千级别"


def test_exports_match_get_proc_address(kernel32):
    """对每个名字：要么我们给出与 GetProcAddress 完全一致的地址，要么我们判定它是转发导出。"""
    library, base, module = kernel32
    size = module.header.size_of_image
    checked = 0
    for name in (
        "GetProcAddress",
        "LoadLibraryW",
        "GetModuleHandleW",
        "VirtualAlloc",
        "GetTickCount",
        "CreateFileW",
        "GetLastError",
        "HeapAlloc",
    ):
        expected = library.GetProcAddress(base, name.encode("ascii"))
        ours = module.export_rva(name)
        if ours is None:
            # 只有两种情况允许返回 None：函数不存在，或它是一个指向别的 dll 的转发导出
            assert expected is None or not (base <= expected < base + size), f"{name} 被误判"
            continue
        assert base + ours == expected, f"{name} 地址不一致"
        checked += 1
    assert checked >= 3, "至少应有若干函数走通正向解析分支"


def test_forwarder_is_skipped(kernel32):
    """找到任意一个真正的转发导出，确认解析器不会把它的"转发字符串地址"当成函数地址。"""
    library, base, module = kernel32
    size = module.header.size_of_image
    for name in ("GetLastError", "HeapAlloc", "CreateFileW", "LoadLibraryW"):
        expected = library.GetProcAddress(base, name.encode("ascii"))
        if expected and not (base <= expected < base + size):
            assert module.export_rva(name) is None, f"{name} 是转发导出，应被跳过"
            return
    pytest.skip("本机 kernel32 中未观察到转发导出")


def test_unknown_export_returns_none(kernel32):
    _library, _base, module = kernel32
    assert module.export_rva("NoSuchFunction__renpy_overlay__") is None
    assert module.export_va("NoSuchFunction__renpy_overlay__") is None


def test_invalid_image_raises():
    with pytest.raises(PeError):
        RemotePE(lambda _rva, size: b"\x00" * size, name="junk")
    with pytest.raises(PeError, match="e_lfanew"):
        RemotePE(lambda _rva, size: b"MZ" + b"\x00" * (size - 2), name="junk")


def test_python_dll_recognition():
    assert is_python_runtime_dll("python27.dll")
    assert is_python_runtime_dll("python39.dll")
    assert is_python_runtime_dll("python310.dll")
    assert is_python_runtime_dll("python312.dll")
    assert is_python_runtime_dll("python3.9.dll")
    assert is_python_runtime_dll("Python310.DLL")
    # Ren'Py 8.x 较新的 Windows 构建把 CPython 改名为 libpython3.9.dll
    assert is_python_runtime_dll("libpython3.9.dll")
    assert is_python_runtime_dll("LIBPYTHON39.DLL")
    # python3.dll 只是稳定 ABI 转发层，python.exe 是可执行文件，都不该被选为注入目标
    assert not is_python_runtime_dll("python3.dll")
    assert not is_python_runtime_dll("python.exe")
    assert not is_python_runtime_dll("pythonw.exe")
    assert not is_python_runtime_dll("_python310.dll")
    # librenpython.dll 是 Ren'Py 的扩展集合 dll（实测不含 Python C API 导出），
    # 不按"运行库实现"识别，只由 rank 当作最后兜底候选
    assert not is_python_runtime_dll("librenpython.dll")
    assert PYTHON_DLL_PATTERN.match("python311.dll")


def test_rank_python_modules():
    modules = [
        ModuleEntry(name="python3.dll", path="", base=0x1000, size=0),
        ModuleEntry(name="kernel32.dll", path="", base=0x2000, size=0),
        ModuleEntry(name="python39.dll", path="", base=0x3000, size=0),
        ModuleEntry(name="pythonw.exe", path="", base=0x4000, size=0),
        ModuleEntry(name="python312.dll", path="", base=0x5000, size=0),
        ModuleEntry(name="libpython3.9.dll", path="", base=0x6000, size=0),
        ModuleEntry(name="librenpython.dll", path="", base=0x7000, size=0),
    ]
    ranked = rank_python_modules(modules)
    names = [item.name for item in ranked]
    assert "kernel32.dll" not in names and "pythonw.exe" not in names
    # 优先级：带版本的实现（含 lib 前缀改名版，按名字排序）> python3.dll 转发层
    # > librenpython.dll 兜底；librenpython 含 "python" 子串但不是实现，必须排在最后
    assert names == [
        "libpython3.9.dll",
        "python312.dll",
        "python39.dll",
        "python3.dll",
        "librenpython.dll",
    ]
