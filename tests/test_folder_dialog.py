"""IFileOpenDialog ctypes 封装的离线验证（不弹真实对话框）。

vtable 结构的槽位偏移用 SDK 头文件的真实顺序断言钉住——曾按 SDK 文档网页的
方法排列推断槽位（Advise/Unadvise 位置不同），导致 SetOptions/GetOptions 整体
错位，实测表现为调用错误方法（access violation / E_FAIL 抛异常）；并曾遗漏
COM 对象首成员到 vtable 的二级解引用（lpVtbl）。改为「对象 = lpVtbl 指针 +
vtable 结构体字段」后由本测试回归。GUID 解析走 ole32.CLSIDFromString
（Windows API，可离线调用）。真实对话框交互由人工联测覆盖。
"""

from __future__ import annotations

import ctypes

from renpy_overlay.folder_dialog import (
    CLSID_FILE_OPEN_DIALOG,
    _guid_from_string,
    _IFileOpenDialog,
    _IFileOpenDialogVtbl,
    _IShellItem,
    _IShellItemVtbl,
)


def test_vtable_slots_match_sdk_header_order():
    """槽位偏移必须与 SDK 头文件声明顺序一致（指针等宽 × 槽序）。"""
    ptr = ctypes.sizeof(ctypes.c_void_p)
    # COM 对象首成员 = lpVtbl 指针（二级解引用的依据）
    assert _IFileOpenDialog.lpVtbl.offset == 0
    vtbl = _IFileOpenDialogVtbl
    assert vtbl.QueryInterface.offset == 0
    assert vtbl.Show.offset == 3 * ptr  # IModalWindow 唯一方法
    assert vtbl.SetOptions.offset == 9 * ptr
    assert vtbl.GetOptions.offset == 10 * ptr
    assert vtbl.GetResult.offset == 20 * ptr
    # IShellItem：对象首成员 = lpVtbl 指针；QI/AddRef/Release + BindToHandler/
    # GetParent 之后是 GetDisplayName
    assert _IShellItem.lpVtbl.offset == 0
    item_vtbl = _IShellItemVtbl
    assert item_vtbl.QueryInterface.offset == 0
    assert item_vtbl.GetDisplayName.offset == 5 * ptr


def test_guid_from_string_parses_fields():
    guid = _guid_from_string(CLSID_FILE_OPEN_DIALOG)
    assert guid.Data1 == 0xDC1C5A9C
    assert guid.Data2 == 0xE88A
    assert guid.Data3 == 0x4DDE
    assert bytes(guid.Data4[:2]) == b"\xa5\xa1"
