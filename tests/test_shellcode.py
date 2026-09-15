"""引导桩的字节级校验。

桩是整条链路里唯一"写错一个字节就静默崩溃"的部分，因此这里逐字段断言机器码布局、
相对位移与数据槽位置 —— 一旦有人调整指令顺序而忘了同步偏移常量，测试会立刻报错。
"""

from __future__ import annotations

import struct

import pytest

from renpy_overlay import shellcode as sc

POINTERS = {"ensure": 0x7FFA_0000_1000, "run": 0x7FFA_0000_2000, "release": 0x7FFA_0000_3000}
SOURCE = b"# coding: utf-8\nprint('\xe4\xbd\xa0\xe5\xa5\xbd')\n"


def _u32(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<I", blob, offset)[0]


def _i32(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<i", blob, offset)[0]


def _u64(blob: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", blob, offset)[0]


# ------------------------------------------------------------------ x64


def test_x64_layout_and_length():
    blob = sc.build_stub(sc.X64, POINTERS, SOURCE, remote_base=0x1_0000_0000)
    assert len(blob) == sc.stub_size(sc.X64, len(SOURCE)) == sc.X64_SRC_OFFSET + len(SOURCE) + 1
    assert blob[:4] == b"\x48\x83\xec\x28", "首指令必须是 sub rsp, 0x28（影子空间）"
    assert blob[sc.X64_CODE_LEN - 1] == 0xC3, "机器码末尾必须是 ret"
    assert blob[sc.X64_CODE_LEN : sc.X64_DATA_OFFSET] == b"\x90" * (
        sc.X64_DATA_OFFSET - sc.X64_CODE_LEN
    ), "代码段与数据槽之间用 nop 填充"


def test_x64_rip_relative_displacements():
    """三个 call 的 rip 相对位移必须精确指向各自的数据槽。

    ``call qword ptr [rip+disp32]`` 的编码是 ``FF 15 disp32``：opcode 与 ModRM
    各占 1 字节，所以 disp32 从指令起始偏移 +2 处开始，而 rip 指向指令**之后**的地址。
    """
    blob = sc.build_stub(sc.X64, POINTERS, SOURCE, remote_base=0x1_0000_0000)
    for call_at, slot in (
        (0x04, sc.X64_SLOT_ENSURE),
        (0x19, sc.X64_SLOT_RUN),
        (0x28, sc.X64_SLOT_RELEASE),
    ):
        assert blob[call_at : call_at + 2] == b"\xff\x15", "call qword ptr [rip+disp32]"
        disp_at = call_at + 2
        rip_after = disp_at + 4
        assert rip_after + _i32(blob, disp_at) == slot


def test_x64_data_slots_and_source():
    base = 0x1_0000_0000
    blob = sc.build_stub(sc.X64, POINTERS, SOURCE, remote_base=base)
    assert _u64(blob, sc.X64_SLOT_ENSURE) == POINTERS["ensure"]
    assert _u64(blob, sc.X64_SLOT_RUN) == POINTERS["run"]
    assert _u64(blob, sc.X64_SLOT_RELEASE) == POINTERS["release"]
    # mov rcx, imm64 里的源码地址必须指向源码槽
    assert blob[0x0F:0x11] == b"\x48\xb9"
    assert _u64(blob, 0x11) == base + sc.X64_SRC_OFFSET
    assert blob[sc.X64_SRC_OFFSET : sc.X64_SRC_OFFSET + len(SOURCE)] == SOURCE
    assert blob[-1] == 0x00, "源码必须以 NUL 结尾（PyRun_SimpleString 需要 C 字符串）"


# ------------------------------------------------------------------ x86


def test_x86_layout_and_patches():
    base = 0x0040_0000
    blob = sc.build_stub(sc.X86, {k: v & 0xFFFF_FFFF for k, v in POINTERS.items()}, SOURCE, base)
    assert len(blob) == sc.stub_size(sc.X86, len(SOURCE)) == sc.X86_SRC_OFFSET + len(SOURCE) + 1
    assert blob[:3] == b"\x83\xec\x08", "首指令必须是 sub esp, 8（预留 gil 与返回值）"
    assert blob[sc.X86_CODE_LEN - 1] == 0xC3

    pointers = {k: v & 0xFFFF_FFFF for k, v in POINTERS.items()}
    assert _u32(blob, 0x05) == base + sc.X86_SLOT_ENSURE
    assert _u32(blob, 0x0D) == base + sc.X86_SRC_OFFSET
    assert _u32(blob, 0x13) == base + sc.X86_SLOT_RUN
    assert _u32(blob, 0x23) == base + sc.X86_SLOT_RELEASE
    assert _u32(blob, sc.X86_SLOT_ENSURE) == pointers["ensure"]
    assert _u32(blob, sc.X86_SLOT_RUN) == pointers["run"]
    assert _u32(blob, sc.X86_SLOT_RELEASE) == pointers["release"]
    assert blob[sc.X86_SRC_OFFSET : sc.X86_SRC_OFFSET + len(SOURCE)] == SOURCE
    assert blob[-1] == 0x00


def test_x86_rejects_out_of_range_pointer():
    bad = dict(POINTERS)
    bad["release"] = 0x1_0000_0000
    with pytest.raises(ValueError, match="超出 32 位范围"):
        sc.build_stub(sc.X86, bad, SOURCE, 0x0040_0000)


# ------------------------------------------------------------------ 参数校验


def test_length_is_independent_of_base():
    """先用 base=0 量长度、再用真实地址生成，两者长度必须一致（注入流程依赖这一点）。"""
    probe = sc.build_stub(sc.X64, POINTERS, SOURCE, 0)
    real = sc.build_stub(sc.X64, POINTERS, SOURCE, 0x7FF_1234_0000)
    assert len(probe) == len(real)
    assert probe != real, "源码地址不同，字节内容必然不同"


def test_unsupported_arch_and_missing_pointers():
    with pytest.raises(ValueError, match="不支持的架构"):
        sc.build_stub("arm64", POINTERS, SOURCE)
    with pytest.raises(ValueError, match="缺少函数指针"):
        sc.build_stub(sc.X64, {"ensure": 1, "run": 2}, SOURCE)


def test_source_address_helper_matches_data_slot():
    assert sc.source_address(0x1000, sc.X64) == 0x1000 + sc.X64_SRC_OFFSET
    assert sc.source_address(0x1000, sc.X86) == 0x1000 + sc.X86_SRC_OFFSET
