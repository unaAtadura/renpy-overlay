"""QuickMenu 视频功能菜单的离线验证：结构顺序、录制中文案与回调分发。

QMenu 以轻量 stub 替换（记录条目序列，exec 返回预设动作），不弹真实菜单。
覆盖需求（future_视频录制.txt）：录制视频/视频编辑位于锁鼠标区域与听歌
识曲之间（含分隔线）；录制中显示「录制视频 mm:ss」；语音识别历史追加在
查看历史末尾；三个新回调正确分发。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint

from renpy_overlay import quick_menu
from renpy_overlay.quick_menu import QuickMenu


class _StubAction:
    def __init__(self, text: str) -> None:
        self.text = text

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 约定
        pass


class _StubMenu:
    """QMenu stub：记录条目序列；exec 经 pick 回调从当前菜单自取动作。"""

    def __init__(self) -> None:
        self.items: list = []
        self.title = ""
        self.pick = None  # exec(self) -> action | None（由子类注入）

    def windowFlags(self) -> int:  # noqa: N802 - Qt 约定
        return 0

    def setWindowFlags(self, flags) -> None:  # noqa: N802 - Qt 约定
        pass

    def addAction(self, text: str) -> _StubAction:
        action = _StubAction(text)
        self.items.append(("action", action))
        return action

    def addSeparator(self):
        self.items.append(("sep", None))
        return ("sep", None)

    def addMenu(self, title: str) -> _StubMenu:
        submenu = _StubMenu()
        submenu.title = title
        self.items.append(("submenu", submenu))
        return submenu

    def exec(self, pos) -> _StubAction | None:  # noqa: N802 - Qt 约定
        return self.pick(self) if self.pick else None


class _StubRecorder:
    """VideoRecorder 鸭子接口 stub：只暴露菜单文案。"""

    def __init__(self, label: str) -> None:
        self._label = label

    def menu_label(self) -> str:
        return self._label


def _layout(menu: _StubMenu) -> list:
    """菜单条目序列化：action 用文本表示，子菜单递归展开。"""
    result: list = []
    for kind, value in menu.items:
        if kind == "action":
            result.append(value.text)
        elif kind == "sep":
            result.append("---")
        else:
            result.append((value.title, _layout(value)))
    return result


def _open(monkeypatch, *, recorder=None, pick=None, **callbacks):
    """弹出一次 stub 菜单，返回 (主菜单 stub, 各回调的调用记录)。

    ``pick`` 在 exec 时以当前菜单实例调用，返回要选中的动作
    （跨实例的对象身份在 QuickMenu 内以 ``is`` 比较，必须同源）。
    """
    holder: list[_StubMenu] = []

    class _CapturingMenu(_StubMenu):
        def __init__(self):
            super().__init__()
            self.pick = pick
            holder.append(self)

    monkeypatch.setattr(quick_menu, "QMenu", _CapturingMenu)

    def _capture():
        calls: list = []
        return calls, lambda *args: calls.append(args)

    record_calls, on_record = _capture()
    editor_calls, on_editor = _capture()
    asr_calls, on_asr = _capture()
    menu_obj = QuickMenu(
        on_capture_click=lambda w: None,
        on_open_history=lambda: None,
        on_record_video=callbacks.get("on_record_video", on_record),
        on_open_video_editor=callbacks.get("on_open_video_editor", on_editor),
        on_open_asr_history=callbacks.get("on_open_asr_history", on_asr),
    )
    if recorder is not None:
        menu_obj.attach_recorder(recorder)
    menu_obj.show_context_menu(QPoint(0, 0))
    return holder[0], {
        "record": record_calls,
        "editor": editor_calls,
        "asr": asr_calls,
    }


#: 选中动作的取法（exec 时从当前菜单实例自取，保证对象身份一致）
#: 条目序（无快捷键模式/锁鼠标时）：0 创建 1 销毁 2 分隔线 3 录制 4 编辑 …
PICK_RECORD = lambda m: m.items[3][1]  # noqa: E731 - 分隔线之后第一项：录制视频
PICK_EDITOR = lambda m: m.items[4][1]  # noqa: E731 - 紧随其后：视频编辑
PICK_ASR = lambda m: m.items[-3][1].items[-1][1]  # noqa: E731 - 查看历史末项


def test_menu_layout_and_order(monkeypatch):
    """菜单顺序（需求）：…分隔线 → 录制视频 → 视频编辑 → 分隔线 → 听歌识曲 …；
    查看历史末尾追加语音识别历史（无分隔线）。"""
    menu, _calls = _open(monkeypatch, recorder=_StubRecorder("录制视频"))
    assert _layout(menu) == [
        "创建截图窗口（0/8）",
        "销毁截图窗口",
        # 无快捷键模式与锁鼠标控制器时无对应项
        "---",
        "录制视频",
        "视频编辑",
        "---",
        "听歌识曲",
        "---",
        "对话",
        "---",
        (
            "查看历史",
            [
                "翻译历史",
                "截图历史",
                "识曲历史",
                "对话历史",
                "语音识别历史",
            ],
        ),
        "---",
        "退出",
    ]


def test_recording_label_shown_while_recording(monkeypatch):
    menu, _calls = _open(monkeypatch, recorder=_StubRecorder("录制视频 01:23"))
    assert "录制视频 01:23" in _layout(menu), "录制中菜单应显示 mm:ss 文案"


def test_record_video_callback_dispatch(monkeypatch):
    """exec 返回对应动作时，三个新回调被正确分发。"""
    for pick, key in (
        (PICK_RECORD, "record"),
        (PICK_EDITOR, "editor"),
        (PICK_ASR, "asr"),
    ):
        _menu, calls = _open(monkeypatch, pick=pick)
        assert calls[key] == [()]


def test_missing_callbacks_are_ignored(monkeypatch):
    """回调替换为 None 时点击对应菜单项不崩溃（现状兼容）。"""
    holder: list[_StubMenu] = []

    class _PlainMenu(_StubMenu):
        def __init__(self):
            super().__init__()
            self.pick = PICK_ASR
            holder.append(self)

    monkeypatch.setattr(quick_menu, "QMenu", _PlainMenu)
    menu_obj = QuickMenu(on_capture_click=lambda w: None, on_open_history=lambda: None)
    menu_obj.show_context_menu(QPoint(0, 0))  # 无回调选中语音识别历史：不应抛异常
