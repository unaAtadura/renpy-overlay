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

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 约定
        self.text = text


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


class _StubSignal:
    """信号 stub：记录连接的回调，可手动触发。"""

    def __init__(self):
        self.handlers = []

    def connect(self, handler):
        self.handlers.append(handler)

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _StubTimer:
    """QTimer stub：记录 interval/start/stop，timeout 可手动触发。"""

    def __init__(self, parent=None) -> None:
        self.timeout = _StubSignal()
        self.interval = None
        self.started = False
        self.stopped = False

    def setInterval(self, ms) -> None:  # noqa: N802 - Qt 约定
        self.interval = ms

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _StubRecorder:
    """VideoRecorder 鸭子接口 stub：菜单文案 + 录制状态。"""

    def __init__(self, label: str, recording: bool = False) -> None:
        self._label = label
        self.recording = recording

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


def _open(monkeypatch, *, recorder=None, asr_rt=None, pick=None, **callbacks):
    """弹出一次 stub 菜单，返回 (主菜单 stub, 各回调的调用记录)。

    ``pick`` 在 exec 时以当前菜单实例调用，返回要选中的动作
    （跨实例的对象身份在 QuickMenu 内以 ``is`` 比较，必须同源）。
    QTimer 同步替换为 stub（实例收集在主菜单 stub 的 ``timers`` 列表）。
    """
    holder: list[_StubMenu] = []
    timers: list[_StubTimer] = []

    class _CapturingMenu(_StubMenu):
        def __init__(self):
            super().__init__()
            self.pick = pick
            self.timers = timers
            holder.append(self)

    class _CapturingTimer(_StubTimer):
        def __init__(self, parent=None):
            super().__init__()
            timers.append(self)

    monkeypatch.setattr(quick_menu, "QMenu", _CapturingMenu)
    monkeypatch.setattr(quick_menu, "QTimer", _CapturingTimer)

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
    if asr_rt is not None:
        menu_obj.attach_asr_rt(asr_rt)
    menu_obj.show_context_menu(QPoint(0, 0))
    return holder[0], {
        "record": record_calls,
        "editor": editor_calls,
        "asr": asr_calls,
    }


#: 选中动作的取法（exec 时从当前菜单实例自取，保证对象身份一致）
#: 条目序（无快捷键模式/锁鼠标时）：0 创建 1 销毁 2 分隔线 3 录制 4 编辑
#: 5 实时语音识别（未注入控制器时不占位）…
PICK_RECORD = lambda m: m.items[3][1]  # noqa: E731 - 分隔线之后第一项：录制视频
PICK_EDITOR = lambda m: m.items[4][1]  # noqa: E731 - 紧随其后：视频编辑
PICK_ASR_RT = lambda m: m.items[5][1]  # noqa: E731 - 紧随其后：实时语音识别
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


# ---------------------------------------------------------------- 实时语音识别


class _StubAsrRt:
    """实时语音识别控制器 stub：enabled 状态 + toggle 调用记录。"""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.toggles = 0

    def toggle(self) -> None:
        self.toggles += 1


def test_asr_rt_entry_layout_when_attached(monkeypatch):
    """注入控制器后：实时语音识别位于视频编辑之下、分隔线与听歌识曲之上
    （需求布局：视频编辑 → 实时语音识别: 关 → 分隔线 → 听歌识曲）。"""
    menu, _calls = _open(monkeypatch, asr_rt=_StubAsrRt(enabled=False))
    layout = _layout(menu)
    assert layout.index("视频编辑") < layout.index("实时语音识别: 关")
    sep_after = layout.index("---", layout.index("实时语音识别: 关"))
    assert layout.index("实时语音识别: 关") < sep_after < layout.index("听歌识曲")


def test_asr_rt_entry_label_follows_state(monkeypatch):
    """菜单文案随控制器 enabled 切换（默认关，开启后显示「开」）。"""
    menu, _calls = _open(monkeypatch, asr_rt=_StubAsrRt(enabled=True))
    assert "实时语音识别: 开" in _layout(menu)


def test_asr_rt_toggle_dispatch(monkeypatch):
    """选中实时语音识别项分发 toggle；未注入控制器时点击不崩溃。"""
    controller = _StubAsrRt()
    _menu, _calls = _open(monkeypatch, asr_rt=controller, pick=PICK_ASR_RT)
    assert controller.toggles == 1
    # 未注入控制器：条目不占位，条目 5 是分隔线，选中它不匹配任何动作，
    # 不应崩溃也不触发任何回调
    _menu, _calls = _open(monkeypatch, pick=lambda m: m.items[5][1])
    assert _calls["asr"] == []


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


# ---------------------------------------------------------------- 录制计时实时刷新


class _LiveRecorder:
    """录制中的 recorder stub：菜单文案随内部秒数变化（验证实时刷新）。"""

    recording = True

    def __init__(self) -> None:
        self.seconds = 60

    def menu_label(self) -> str:
        return f"录制视频 {self.seconds // 60:02d}:{self.seconds % 60:02d}"


def test_record_label_refresh_timer_while_recording(monkeypatch):
    """录制中菜单打开期间：QTimer 按秒刷新「录制视频 mm:ss」文案
    （不依赖重新打开菜单）。"""
    recorder = _LiveRecorder()
    menu, _calls = _open(monkeypatch, recorder=recorder)
    assert len(menu.timers) == 1, "录制中应创建刷新定时器"
    timer = menu.timers[0]
    assert timer.started is True and timer.interval == 1000
    record_action = menu.items[3][1]  # 分隔线之后第一项：录制视频
    assert record_action.text == "录制视频 01:00"
    recorder.seconds = 75  # 时间流逝
    timer.timeout.emit()  # 定时器触发：文案实时更新
    assert record_action.text == "录制视频 01:15", "菜单打开期间计时应实时走动"


def test_refresh_timer_stopped_after_menu_closed(monkeypatch):
    """菜单关闭（exec 返回）后停止刷新定时器。"""
    menu, _calls = _open(monkeypatch, recorder=_LiveRecorder())
    timer = menu.timers[0]
    assert timer.started is True
    assert timer.stopped is True, "exec 返回后应停止刷新定时器"


def test_no_refresh_timer_when_not_recording(monkeypatch):
    """未录制时菜单项静态「录制视频」，不创建/启动刷新定时器。"""
    menu, _calls = _open(monkeypatch, recorder=_StubRecorder("录制视频"))
    assert menu.timers == []
