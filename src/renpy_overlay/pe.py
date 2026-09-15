"""在目标进程内解析 PE 导出表，定位 Python C API 的入口地址。

本机 Python 版本与游戏内置版本通常不一致（本机 3.
为什么不直接使用本机 Python dll 的导出偏移：13，游戏可能是 Ren'Py 7.x 的
2.7、或 8.x 的 3.9 / 3.10 / 3.12），函数在导出表里的序号、甚至是否存在都会变化。
只依赖 PE 格式本身就能完全避开版本耦合 —— 导出表在哪、目标函数排第几，
全部由目标进程内存中的真实数据决定。

读取接口采用 RVA 语义：``reader(rva, size) -> bytes``。
- 注入场景：``lambda rva, size: process.read(base + rva, size)``
- 单元测试：直接读本地已加载模块 ``ctypes.string_at(base + rva, size)``
两种场景复用同一套解析逻辑，因此 PE 解析的正确性可以被离线验证。
"""

from __future__ import annotations

import logging
import re
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .win32api import ModuleEntry, RemoteProcess

logger = logging.getLogger("renpy_overlay.pe")

IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_ARMNT = 0x01C4
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64

ARCH_BY_MACHINE: dict[int, str] = {
    IMAGE_FILE_MACHINE_I386: "x86",
    IMAGE_FILE_MACHINE_ARMNT: "arm",
    IMAGE_FILE_MACHINE_AMD64: "x64",
    IMAGE_FILE_MACHINE_ARM64: "arm64",
}

#: Python 运行库 dll 的命名规则（大小写不敏感）：
#: - 标准 CPython 命名：python27.dll / python39.dll / python310.dll / python3.9.dll；
#: - Ren'Py 8.x 较新的 Windows 构建把 CPython 改名为 ``libpython3.9.dll``（lib 前缀 +
#:   点分版本号），因此前缀 lib 与两种版本号写法都要接受；
#: - "python310" 里的 "310" 是两位版本号，必须用 \d{1,2} 而不是 \d{2}。
#: 特意排除只做稳定 ABI 转发的 python3.dll（由下面的 FALLBACK 模式另行处理），
#: 以及 python.exe / pythonw.exe 这类可执行文件。
PYTHON_DLL_PATTERN = re.compile(
    r"^(?:lib)?python(?:2\d|3\d{1,2}|[23]\.\d{1,2})\.dll$", re.IGNORECASE
)
#: 稳定 ABI 转发层：本身不实现解释器，只在没有更好的候选时兜底尝试。
PYTHON_DLL_FALLBACK_PATTERN = re.compile(r"^python3\.dll$", re.IGNORECASE)
#: Ren'Py 8.x 自带的扩展集合 dll（librenpython.dll，一万多个导出但通常不含
#: Python C API；实测 Eternum 0.9.5 / Ren'Py 8.x 即如此）。个别自编译构建可能把
#: 解释器静态链接进去，所以留作最后兜底 —— 是否可用由导出解析结果裁决。
RENPYTHON_DLL_PATTERN = re.compile(r"^(?:lib)?renpython\.dll$", re.IGNORECASE)

# PE 头部尺寸常量
_DOS_HEADER_SIZE = 0x40
_NT_HEADERS_SIZE = 0x120
_EXPORT_DIR_SIZE = 40
_MAX_EXPORT_NAMES = 200_000


class PeError(OSError):
    """远程 PE 结构不符合预期（读到的不是有效镜像）。"""


def _u16(buffer: bytes, offset: int) -> int:
    return struct.unpack_from("<H", buffer, offset)[0]


def _u32(buffer: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buffer, offset)[0]


@dataclass(frozen=True)
class PeHeader:
    machine: int
    arch: str
    is_pe32_plus: bool
    size_of_image: int
    export_dir_rva: int
    export_dir_size: int
    num_sections: int


def is_python_runtime_dll(name: str) -> bool:
    return bool(PYTHON_DLL_PATTERN.match(name or ""))


def rank_python_modules(modules: Iterable[ModuleEntry]) -> list[ModuleEntry]:
    """挑出疑似 Python 运行库的模块，按"越像"越靠前排序。

    排序只影响尝试顺序：注入器会依次解析每个候选的 PE 导出表，直到某个模块同时
    提供全部所需函数为止（``librenpython.dll`` 这类扩展集合会被自然跳过），
    因此排序不影响最终正确性。
    """

    def score(item: ModuleEntry) -> tuple[int, str]:
        name = item.name or ""
        lowered = name.lower()
        if PYTHON_DLL_PATTERN.match(name):
            priority = 0
        elif PYTHON_DLL_FALLBACK_PATTERN.match(name):
            priority = 1
        elif RENPYTHON_DLL_PATTERN.match(name):
            priority = 2
        elif "python" in lowered:
            priority = 3
        else:
            priority = 4
        return (priority, lowered)

    # "renpython" 自身包含 "python" 子串，因此一个过滤条件即可覆盖两种命名
    candidates = [
        item
        for item in modules
        if (item.name or "").lower().endswith(".dll") and "python" in (item.name or "").lower()
    ]
    return sorted(candidates, key=score)


class RemotePE:
    """一个已加载进目标进程的 PE 模块的只读视图。"""

    def __init__(
        self,
        reader: Callable[[int, int], bytes],
        base: int = 0,
        name: str = "",
        path: str = "",
    ):
        self._reader = reader
        self.base = base
        self.name = name
        self.path = path
        self.header = self._parse_header()
        self._names_rva = 0
        self._funcs_rva = 0
        self._ordinals_rva = 0
        self._name_count = 0
        self._func_count = 0
        self._names_loaded = False

    @classmethod
    def from_process(cls, process: RemoteProcess, entry: ModuleEntry) -> RemotePE:
        reader = lambda rva, size: process.read(entry.base + rva, size)  # noqa: E731
        return cls(reader, base=entry.base, name=entry.name, path=entry.path)

    # -- 头部解析 -------------------------------------------------

    def _parse_header(self) -> PeHeader:
        dos = self._reader(0, _DOS_HEADER_SIZE)
        if len(dos) < _DOS_HEADER_SIZE or dos[:2] != b"MZ":
            raise PeError(f"{self.name or '模块'} 不是有效的 PE 镜像（缺少 MZ 头）")
        e_lfanew = _u32(dos, 0x3C)
        if not 0 < e_lfanew < 0x1000:
            raise PeError(f"{self.name} 的 e_lfanew 异常：0x{e_lfanew:X}")

        nt = self._reader(e_lfanew, _NT_HEADERS_SIZE)
        if nt[:4] != b"PE\x00\x00":
            raise PeError(f"{self.name} 缺少 PE 签名")

        machine = _u16(nt, 4)
        num_sections = _u16(nt, 6)
        size_of_optional = _u16(nt, 20)
        magic = _u16(nt, 24)
        is_pe32_plus = magic == 0x20B

        # DataDirectory 数组在可选头内的偏移：PE32 = 96，PE32+ = 112
        directory_offset = 24 + (112 if is_pe32_plus else 96)
        export_dir_rva = _u32(nt, directory_offset)
        export_dir_size = _u32(nt, directory_offset + 4)
        size_of_image = _u32(nt, 24 + 56)

        header = PeHeader(
            machine=machine,
            arch=ARCH_BY_MACHINE.get(machine, f"unknown(0x{machine:04X})"),
            is_pe32_plus=is_pe32_plus,
            size_of_image=size_of_image,
            export_dir_rva=export_dir_rva,
            export_dir_size=export_dir_size,
            num_sections=num_sections,
        )
        logger.debug(
            "解析 %s：arch=%s base=0x%X image=0x%X 节=%d 导出表=0x%X(+0x%X) 可选头=%d",
            self.name or "<memory>",
            header.arch,
            self.base,
            size_of_image,
            num_sections,
            export_dir_rva,
            export_dir_size,
            size_of_optional,
        )
        return header

    # -- 导出表 ---------------------------------------------------

    def _load_exports(self) -> None:
        if self._names_loaded:
            return
        self._names_loaded = True
        header = self.header
        if not header.export_dir_rva or not header.export_dir_size:
            logger.debug("%s 没有导出表", self.name)
            return

        directory = self._reader(header.export_dir_rva, _EXPORT_DIR_SIZE)
        self._func_count = _u32(directory, 20)
        self._name_count = _u32(directory, 24)
        self._funcs_rva = _u32(directory, 28)
        self._names_rva = _u32(directory, 32)
        self._ordinals_rva = _u32(directory, 36)

        if self._name_count > _MAX_EXPORT_NAMES:
            raise PeError(f"{self.name} 的导出名字数量异常：{self._name_count}")
        logger.debug(
            "%s 导出表：函数 %d 个，具名 %d 个", self.name, self._func_count, self._name_count
        )

    def _name_rva(self, index: int) -> int:
        return _u32(self._reader(self._names_rva + index * 4, 4), 0)

    def _ordinal_for(self, index: int) -> int:
        return _u16(self._reader(self._ordinals_rva + index * 2, 2), 0)

    def _name_at(self, index: int) -> bytes:
        return self._reader(self._name_rva(index), 128).split(b"\x00", 1)[0]

    def _is_forwarder(self, func_rva: int) -> bool:
        return (
            self.header.export_dir_rva
            <= func_rva
            < (self.header.export_dir_rva + self.header.export_dir_size)
        )

    def export_rva(self, name: str) -> int | None:
        """按名字查找导出函数，返回 RVA；未找到或指向转发器时返回 None。

        PE 的 AddressOfNames 数组按字典序升序排列，直接用二分查找即可，
        每次探测只读一个字符串，整个查找过程只有 log2(N) 次远程读取。
        """
        self._load_exports()
        if not self._name_count:
            return None
        target = name.encode("ascii")
        low, high = 0, self._name_count - 1
        while low <= high:
            mid = (low + high) // 2
            current = self._name_at(mid)
            if current == target:
                ordinal = self._ordinal_for(mid)
                if ordinal >= self._func_count:
                    logger.warning("%s 中 %s 的序号 %d 越界", self.name, name, ordinal)
                    return None
                func_rva = _u32(self._reader(self._funcs_rva + ordinal * 4, 4), 0)
                if not func_rva:
                    return None
                if self._is_forwarder(func_rva):
                    # 转发到别的 dll，不能当成本模块内的可调用地址
                    logger.debug("%s!%s 是转发导出，跳过", self.name, name)
                    return None
                return func_rva
            if current < target:
                low = mid + 1
            else:
                high = mid - 1
        return None

    def export_va(self, name: str) -> int | None:
        rva = self.export_rva(name)
        return None if rva is None else self.base + rva

    def resolve(self, names: Iterable[str]) -> dict[str, int | None]:
        return {name: self.export_va(name) for name in names}

    @property
    def export_name_count(self) -> int:
        self._load_exports()
        return self._name_count


def load_self_module(name: str) -> tuple[int, int]:
    """取当前进程内已加载模块的 ``(基址, 大小)``，供离线自检与单测使用。"""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    handle = kernel32.GetModuleHandleW(name)
    if not handle:
        raise PeError(f"当前进程未加载模块 {name}")
    return int(handle), 0


def self_reader(base: int) -> Callable[[int, int], bytes]:
    """构造"读取本进程内存"的 reader，用于离线验证 PE 解析逻辑。"""
    import ctypes

    return lambda rva, size: ctypes.string_at(base + rva, size)
