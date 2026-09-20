"""系统文件夹选择对话框：IFileOpenDialog（COM）的 ctypes 封装。

供跳过注入模式选择数据目录（需求 .raw_plans/future_跳过注入.txt：调用
IFileOpenDialog API 选择文件夹）。只依赖标准库 ctypes：IFileOpenDialog 不是
IDispatch 接口，pywin32 的动态绑定不适用。

实现要点（三处实测教训，均已用诊断脚本定位）：

1. **vtable 顺序必须取自 SDK 头文件**（``um/shobjidl_core.h``）：真实顺序是
   ``Show=3, SetFileTypes=4, SetFileTypeIndex=5, GetFileTypeIndex=6,
   Advise=7, Unadvise=8, SetOptions=9, GetOptions=10, ..., GetResult=20``——
   按 SDK 文档网页的方法排列推断（Advise/Unadvise 位置不同）会整体错位，
   实测调用到 SetFileName/GetFileName（把标志值当字符串指针解引用 AV、
   返回 E_FAIL）。因此用结构体字段按序声明全部槽位，不做下标运算。
2. **COM 对象首成员是 vtable 指针，必须经 ``lpVtbl`` 二级解引用**：对象结构
   只有 ``lpVtbl`` 一个字段，方法原型全部声明在 vtable 结构上——把函数指针
   直接挂在对象字段上等于把 vtable 数组地址当代码调用（实测 AV）。
3. **restype 为 ``ctypes.HRESULT`` 的调用在失败时由 ctypes 自动抛
   ``OSError``**（winerror = 失败 HRESULT），因此逐段调用用 try/except 收敛，
   不依赖返回值判错。

线程约束：在 Tk 选择窗的主线程调用；COM 按单元线程初始化
（CoInitializeEx COINIT_APARTMENTTHREADED），成功（S_OK/S_FALSE）都配对
CoUninitialize。对话框以选择窗为属主模态弹出：期间选择窗保持打开但不响应
交互，对话框关闭（含取消）后控制权回到选择窗。
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger("renpy_overlay.folder_dialog")

CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_I_FILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"

FOS_PICKFOLDERS = 0x20  # 限制为选择文件夹
FOS_FORCEFILESYSTEM = 0x40  # 只允许文件系统路径（取文件路径名的前提）
SIGDN_FILESYSPATH = 0x80058000
S_OK = 0
S_FALSE = 1  # CoInitializeEx：COM 已在此线程初始化过（仍需配对 CoUninitialize）
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
ERROR_CANCELLED = 0x800704C7  # 用户取消对话框

_HRESULT = ctypes.HRESULT
_CVOID = ctypes.c_void_p
_WINFUNCTYPE = ctypes.WINFUNCTYPE

_QueryInterfaceProto = _WINFUNCTYPE(_HRESULT, _CVOID, _CVOID, _CVOID)
_AddRefProto = _WINFUNCTYPE(ctypes.c_ulong, _CVOID)
_ReleaseProto = _AddRefProto
_ShowProto = _WINFUNCTYPE(_HRESULT, _CVOID, _CVOID)
_SetOptionsProto = _WINFUNCTYPE(_HRESULT, _CVOID, ctypes.c_ulong)
_GetOptionsProto = _WINFUNCTYPE(_HRESULT, _CVOID, ctypes.POINTER(ctypes.c_ulong))
_GetResultProto = _WINFUNCTYPE(_HRESULT, _CVOID, _CVOID)
_GetDisplayNameProto = _WINFUNCTYPE(_HRESULT, _CVOID, ctypes.c_ulong, _CVOID)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _IShellItemVtbl(ctypes.Structure):
    """IShellItem 的 vtable（槽位顺序 = SDK 头文件真实顺序）。"""

    _fields_ = [
        ("QueryInterface", _QueryInterfaceProto),
        ("AddRef", _AddRefProto),
        ("Release", _ReleaseProto),
        ("BindToHandler", _CVOID),
        ("GetParent", _CVOID),
        ("GetDisplayName", _GetDisplayNameProto),
    ]


class _IShellItem(ctypes.Structure):
    """COM 对象结构：首成员是指向 vtable 的指针（SDK C 接口约定）。"""

    _fields_ = [("lpVtbl", ctypes.POINTER(_IShellItemVtbl))]


class _IFileOpenDialogVtbl(ctypes.Structure):
    """IFileOpenDialog 的 vtable（槽位顺序 = SDK ``shobjidl_core.h`` 真实声明）。

    未调用的槽位以 ``c_void_p`` 占位：所有槽位都是等宽函数指针，占位不影响
    后续字段的偏移；给出完整原型的只有 Show / SetOptions / GetOptions /
    GetResult 四个本封装实际调用的方法。
    """

    _fields_ = [
        ("QueryInterface", _QueryInterfaceProto),
        ("AddRef", _AddRefProto),
        ("Release", _ReleaseProto),
        ("Show", _ShowProto),
        ("SetFileTypes", _CVOID),
        ("SetFileTypeIndex", _CVOID),
        ("GetFileTypeIndex", _CVOID),
        ("Advise", _CVOID),
        ("Unadvise", _CVOID),
        ("SetOptions", _SetOptionsProto),
        ("GetOptions", _GetOptionsProto),
        ("SetDefaultFolder", _CVOID),
        ("SetFolder", _CVOID),
        ("GetFolder", _CVOID),
        ("GetCurrentSelection", _CVOID),
        ("SetFileName", _CVOID),
        ("GetFileName", _CVOID),
        ("SetTitle", _CVOID),
        ("SetOkButtonLabel", _CVOID),
        ("SetFileNameLabel", _CVOID),
        ("GetResult", _GetResultProto),
        ("AddPlace", _CVOID),
        ("SetDefaultExtension", _CVOID),
        ("Close", _CVOID),
        ("SetClientGuid", _CVOID),
        ("ClearClientData", _CVOID),
    ]


class _IFileOpenDialog(ctypes.Structure):
    """COM 对象结构：首成员是指向 vtable 的指针（SDK C 接口约定）。"""

    _fields_ = [("lpVtbl", ctypes.POINTER(_IFileOpenDialogVtbl))]


def _guid_from_string(text: str) -> _GUID:
    """把 ``{...}`` 形式的 GUID 字符串解析为二进制 GUID（ole32.CLSIDFromString）。"""
    guid = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(
        ctypes.c_wchar_p(text if text.startswith("{") else "{" + text + "}"),
        ctypes.byref(guid),
    )
    if hr != S_OK:
        raise OSError(f"CLSIDFromString 失败：0x{hr & 0xFFFFFFFF:08X}")
    return guid


def pick_folder(owner_hwnd: int = 0) -> str | None:
    """弹出系统文件夹选择对话框，返回所选目录路径；取消或失败返回 None。

    ``owner_hwnd`` 为对话框属主窗口（选择进程窗的原生句柄，0 = 无属主）。
    对话框以属主模态弹出：期间属主窗口保持打开、不可交互（需求：选择期间
    不关闭选择进程窗口）；取消（ERROR_CANCELLED）或任何失败都返回 None，
    由调用方退回选择进程窗口，不抛异常。
    """
    if sys.platform != "win32":  # pragma: no cover - 非 Windows 无此对话框
        return None
    ole32 = ctypes.windll.ole32
    iface = ctypes.POINTER(_IFileOpenDialog)()
    item = ctypes.POINTER(_IShellItem)()
    hr_init = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        if hr_init not in (S_OK, S_FALSE):
            logger.warning("COM 初始化失败：0x%08X", hr_init & 0xFFFFFFFF)
            return None
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid_from_string(CLSID_FILE_OPEN_DIALOG)),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid_from_string(IID_I_FILE_OPEN_DIALOG)),
            ctypes.byref(iface),
        )
        if hr != S_OK or not iface:
            logger.warning("创建 IFileOpenDialog 失败：0x%08X", hr & 0xFFFFFFFF)
            return None
        vtbl = iface.contents.lpVtbl.contents

        # 读取既有选项后叠加文件夹选择所需标志（GetOptions 失败则仅设必需标志）
        options = ctypes.c_ulong()
        vtbl.GetOptions(iface, ctypes.byref(options))
        hr = vtbl.SetOptions(
            iface, options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
        )
        if hr != S_OK:
            logger.warning("设置对话框选项失败：0x%08X", hr & 0xFFFFFFFF)
            return None

        try:
            hr = vtbl.Show(iface, ctypes.c_void_p(owner_hwnd))
        except OSError as exc:
            # ctypes 对失败 HRESULT 自动抛 OSError；winerror 为有符号数，
            # 与无符号常量比较前先按 32 位无符号归一
            if (getattr(exc, "winerror", 0) & 0xFFFFFFFF) == ERROR_CANCELLED:
                logger.debug("文件夹选择对话框被用户取消。")
                return None
            raise  # 其余失败交外层统一收敛
        if hr != S_OK:
            logger.warning("文件夹选择对话框失败：0x%08X", hr & 0xFFFFFFFF)
            return None

        hr = vtbl.GetResult(iface, ctypes.byref(item))
        if hr != S_OK or not item:
            logger.warning("获取对话框结果失败：0x%08X", hr & 0xFFFFFFFF)
            return None

        path_ptr = _CVOID()
        hr = item.contents.lpVtbl.contents.GetDisplayName(
            item, SIGDN_FILESYSPATH, ctypes.byref(path_ptr)
        )
        if hr != S_OK or not path_ptr:
            logger.warning("获取所选路径失败：0x%08X", hr & 0xFFFFFFFF)
            return None
        try:
            path = ctypes.wstring_at(path_ptr)
        finally:
            ole32.CoTaskMemFree(path_ptr)
        logger.info("已选择数据目录：%s", path)
        return path or None
    except Exception as exc:  # pragma: no cover - COM 环境异常统一降级
        logger.warning("文件夹选择对话框异常：%s", exc)
        return None
    finally:
        if item:
            item.contents.lpVtbl.contents.Release(item)
        if iface:
            iface.contents.lpVtbl.contents.Release(iface)
        if hr_init in (S_OK, S_FALSE):
            ole32.CoUninitialize()
