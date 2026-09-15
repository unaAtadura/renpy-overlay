"""生成"引导桩"机器码：在目标进程的新线程里调用它内置的 Python C API。

桩的语义等价于下面这段 C 代码：

    PyGILState_STATE gil = PyGILState_Ensure();
    int rc = PyRun_SimpleString(source);
    PyGILState_Release(gil);
    return rc;                    // 线程退出码 = PyRun_SimpleString 的返回值

退出码是刻意设计的诊断信号：``0`` 表示引导代码执行完毕，``0xFFFFFFFF`` 表示引导
代码抛出了异常（回溯会写进游戏自身的 log.txt）。因此不需要任何额外通道，就能从
外部判断"注入的代码到底有没有跑起来"。

为什么需要 PyGILState_Ensure：
Ren'Py 主循环是 Python 字节码在执行，GIL 大概率被主线程持有。我们创建的是一个
全新的原生线程，它没有任何 Python 线程状态，直接调用 PyRun_SimpleString 会崩。
PyGILState_Ensure 会为当前线程挂上线程状态并拿到 GIL，Release 再还回去。

内存布局（一次性分配的同一块可执行内存）：

    +-------------------------+ 0x00
    | 机器码（下方注释逐字节）  |
    +-------------------------+ DT_OFFSET
    | ensure / run / release   | 三个函数指针数据槽
    +-------------------------+ SRC_OFFSET
    | 引导源码（UTF-8 + \\0）   |
    +-------------------------+

x64 用 RIP 相对寻址 ``call qword ptr [rip+disp32]`` 访问数据槽，因此 disp 只取决于
桩内偏移、与内存实际落点无关；x86 的 ``call dword ptr [abs32]`` 是绝对地址，必须
在拿到远程分配地址后回填（``build_stub(remote_base=...)``）。
"""

from __future__ import annotations

import struct
from collections.abc import Mapping

X64 = "x64"
X86 = "x86"
SUPPORTED_ARCHES = (X64, X86)

# --------------------------------------------------------------- x64 布局

# 机器码：
#   sub  rsp, 0x28              # 4B  预留影子空间，同时把栈对齐到 16 字节
#   call qword ptr [rip+d0]     # 6B  PyGILState_Ensure()
#   mov  [rsp+0x20], rax        # 5B  暂存 gil（复用影子空间，不额外动栈指针）
#   mov  rcx, <imm64 源码地址>   # 10B 第一个参数走 rcx
#   call qword ptr [rip+d1]     # 6B  PyRun_SimpleString(code)
#   mov  [rsp+0x18], eax        # 4B  暂存返回值
#   mov  rcx, [rsp+0x20]        # 5B  第一个参数 = gil
#   call qword ptr [rip+d2]     # 6B  PyGILState_Release(gil)
#   mov  eax, [rsp+0x18]        # 4B  返回值作为线程退出码
#   add  rsp, 0x28              # 4B
#   ret                         # 1B
X64_CODE_LEN = 0x37
X64_DATA_OFFSET = 0x40  # 数据槽起点（16 字节对齐，便于阅读与断言）
X64_SLOT_ENSURE = 0x40
X64_SLOT_RUN = 0x48
X64_SLOT_RELEASE = 0x50
X64_SRC_OFFSET = 0x60

# --------------------------------------------------------------- x86 布局

# 机器码（cdecl，参数在栈上，调用方清理）：
#   sub  esp, 8                 # 3B  预留 8 字节：gil 与返回值
#   call dword ptr [ensure]     # 6B
#   mov  [esp], eax             # 3B  暂存 gil
#   push <imm32 源码地址>        # 5B  PyRun_SimpleString 的参数
#   call dword ptr [run]        # 6B
#   add  esp, 4                 # 3B  清理 cdecl 参数
#   mov  [esp+4], eax           # 4B  暂存返回值
#   push dword [esp]            # 3B  PyGILState_Release 的参数 = gil
#   call dword ptr [release]    # 6B
#   add  esp, 4                 # 3B
#   mov  eax, [esp+4]           # 4B  返回值作为线程退出码
#   add  esp, 8                 # 3B
#   ret                         # 1B
X86_CODE_LEN = 0x32
X86_DATA_OFFSET = 0x34
X86_SLOT_ENSURE = 0x34
X86_SLOT_RUN = 0x38
X86_SLOT_RELEASE = 0x3C
X86_SRC_OFFSET = 0x40

#: 每次调用都要的三个导出函数
REQUIRED_EXPORTS = ("PyGILState_Ensure", "PyRun_SimpleString", "PyGILState_Release")


def src_offset(arch: str) -> int:
    return X64_SRC_OFFSET if arch == X64 else X86_SRC_OFFSET


def data_offset(arch: str) -> int:
    return X64_DATA_OFFSET if arch == X64 else X86_DATA_OFFSET


def stub_size(arch: str, source_length: int) -> int:
    """桩的总长度（机器码 + 数据槽 + 源码 + 结尾 \\0）。"""
    return src_offset(arch) + source_length + 1


def source_address(remote_base: int, arch: str) -> int:
    return remote_base + src_offset(arch)


def _rel32(target_offset: int, rip_after_instruction: int) -> bytes:
    return struct.pack("<i", target_offset - rip_after_instruction)


def _build_x64(pointers: Mapping[str, int], source: bytes, remote_base: int) -> bytearray:
    code = bytearray()
    code += b"\x48\x83\xec\x28"  # sub rsp, 0x28
    code += b"\xff\x15" + _rel32(X64_SLOT_ENSURE, len(code) + 6)  # call [rip+d0]
    code += b"\x48\x89\x44\x24\x20"  # mov [rsp+0x20], rax
    code += b"\x48\xb9" + struct.pack("<Q", remote_base + X64_SRC_OFFSET)  # mov rcx, imm64
    code += b"\xff\x15" + _rel32(X64_SLOT_RUN, len(code) + 6)  # call [rip+d1]
    code += b"\x89\x44\x24\x18"  # mov [rsp+0x18], eax
    code += b"\x48\x8b\x4c\x24\x20"  # mov rcx, [rsp+0x20]
    code += b"\xff\x15" + _rel32(X64_SLOT_RELEASE, len(code) + 6)  # call [rip+d2]
    code += b"\x8b\x44\x24\x18"  # mov eax, [rsp+0x18]
    code += b"\x48\x83\xc4\x28"  # add rsp, 0x28
    code += b"\xc3"  # ret
    if len(code) != X64_CODE_LEN:  # pragma: no cover - 防止手改机器码后忘记同步常量
        raise AssertionError(f"x64 桩长度 {len(code):#x} != {X64_CODE_LEN:#x}")

    blob = bytearray(code)
    blob += b"\x90" * (X64_DATA_OFFSET - len(blob))  # nop 填充，正常情况下不可达
    for key in ("ensure", "run", "release"):
        blob += struct.pack("<Q", pointers[key])
    blob += b"\x00" * (X64_SRC_OFFSET - len(blob))
    return blob


def _build_x86(pointers: Mapping[str, int], source: bytes, remote_base: int) -> bytearray:
    def abs32(offset: int) -> bytes:
        return struct.pack("<I", remote_base + offset)

    code = bytearray()
    code += b"\x83\xec\x08"  # sub esp, 8
    code += b"\xff\x15" + abs32(X86_SLOT_ENSURE)  # call [ensure]
    code += b"\x89\x04\x24"  # mov [esp], eax
    code += b"\x68" + abs32(X86_SRC_OFFSET)  # push <源码地址>
    code += b"\xff\x15" + abs32(X86_SLOT_RUN)  # call [run]
    code += b"\x83\xc4\x04"  # add esp, 4
    code += b"\x89\x44\x24\x04"  # mov [esp+4], eax
    code += b"\xff\x34\x24"  # push dword [esp]
    code += b"\xff\x15" + abs32(X86_SLOT_RELEASE)  # call [release]
    code += b"\x83\xc4\x04"  # add esp, 4
    code += b"\x8b\x44\x24\x04"  # mov eax, [esp+4]
    code += b"\x83\xc4\x08"  # add esp, 8
    code += b"\xc3"  # ret
    if len(code) != X86_CODE_LEN:  # pragma: no cover
        raise AssertionError(f"x86 桩长度 {len(code):#x} != {X86_CODE_LEN:#x}")

    blob = bytearray(code)
    blob += b"\x90" * (X86_DATA_OFFSET - len(blob))
    for key in ("ensure", "run", "release"):
        blob += struct.pack("<I", pointers[key])
    blob += b"\x00" * (X86_SRC_OFFSET - len(blob))
    return blob


def build_stub(
    arch: str,
    pointers: Mapping[str, int],
    source: bytes,
    remote_base: int = 0,
) -> bytes:
    """组装完整的桩字节串。

    :param arch: ``"x64"`` 或 ``"x86"``，取自目标 PE 头部的 Machine 字段
    :param pointers: ``{"ensure": va, "run": va, "release": va}``，均已是目标进程地址
    :param source: 引导源码（UTF-8 字节）；内部会追加 ``\\0`` 终止符
    :param remote_base: 桩在目标进程中的实际起始地址。传 0 时只用于测量长度，
        x86 桩会因此写入 0 的绝对地址，不可直接使用。
    """
    if arch not in SUPPORTED_ARCHES:
        raise ValueError(f"不支持的架构：{arch}（仅支持 {SUPPORTED_ARCHES}）")
    missing = [name for name in ("ensure", "run", "release") if not pointers.get(name)]
    if missing:
        raise ValueError(f"缺少函数指针：{missing}")
    if arch == X86:
        for key in ("ensure", "run", "release"):
            if not 0 < int(pointers[key]) < 0x1_0000_0000:
                raise ValueError(f"{key} 的地址 {pointers[key]:#x} 超出 32 位范围")

    blob = (
        _build_x64(pointers, source, remote_base)
        if arch == X64
        else _build_x86(pointers, source, remote_base)
    )
    blob += source
    blob += b"\x00"
    expected = stub_size(arch, len(source))
    if len(blob) != expected:  # pragma: no cover
        raise AssertionError(f"桩长度 {len(blob)} != 预期 {expected}")
    return bytes(blob)
