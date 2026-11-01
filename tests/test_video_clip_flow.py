"""视频剪辑入口链路的离线验证：打开注入、单例复用、PyAV 缺失降级与销毁清引用。

EditorWindow 以 stub 类替换（不构造真实引擎/SDL 窗口），验证宿主侧的组装
参数与生命周期约定（``video_flow._open_video_clip``）；关闭即销毁
（WA_DeleteOnClose）的约定由 ``destroyed`` 回调清空单例引用覆盖。
"""

from __future__ import annotations

import pytest

from renpy_overlay.stream.video_flow import VideoFlowMixin
from renpy_overlay.video.clip import editor_window as editor_window_module


class _StubSignal:
    """Qt 信号替身：仅支持 connect（销毁回调注册）。"""

    def __init__(self) -> None:
        self.handlers = []

    def connect(self, handler) -> None:
        self.handlers.append(handler)


class _StubEditorWindow:
    """EditorWindow 替身：记录构造参数与窗口原语调用（不触引擎/SDL）。"""

    instances: list[_StubEditorWindow] = []

    def __init__(self, *, config, api_acquire, api_release, video_dir, parent=None):
        self.init_kwargs = {
            "config": config,
            "api_acquire": api_acquire,
            "api_release": api_release,
            "video_dir": video_dir,
        }
        self.destroyed = _StubSignal()
        self.show_calls = 0
        self.raise_calls = 0
        _StubEditorWindow.instances.append(self)

    def show(self) -> None:
        self.show_calls += 1

    def raise_(self) -> None:  # noqa: N802 - Qt 约定
        self.raise_calls += 1

    def activateWindow(self) -> None:  # noqa: N802 - Qt 约定
        pass


def _make_host(**overrides):
    """构造 VideoFlowMixin 宿主 stub：仅提供 _open_video_clip 触碰的成员。"""

    class _Host(VideoFlowMixin):
        def __init__(self):
            self._closed = False
            self._video_clip_window = None
            self._config = object()  # 任意配置对象（身份断言用）
            self._game_dir = None
            self.acquire_calls = 0
            self.release_calls = 0
            self.hints: list[str] = []

        def _acquire_chat_slot(self) -> None:
            self.acquire_calls += 1

        def _release_chat_slot(self) -> None:
            self.release_calls += 1

        def hint(self, text: str) -> None:
            self.hints.append(text)

    host = _Host()
    for key, value in overrides.items():
        setattr(host, key, value)
    return host


@pytest.fixture(autouse=True)
def _reset_and_stub(monkeypatch):
    """每个用例：清空 stub 实例记录 + 替换 EditorWindow 与 default_video_dir。"""
    _StubEditorWindow.instances.clear()
    monkeypatch.setattr(editor_window_module, "EditorWindow", _StubEditorWindow)
    monkeypatch.setattr(
        "renpy_overlay.video.recorder.default_video_dir",
        lambda game_dir: f"video-dir-for-{game_dir}",
    )


def test_open_creates_window_with_host_injections():
    """首次打开：EditorWindow 收到宿主配置/互斥回调/默认目录；窗口置前。"""
    host = _make_host()
    host._open_video_clip()
    assert len(_StubEditorWindow.instances) == 1
    window = _StubEditorWindow.instances[0]
    assert window.init_kwargs["config"] is host._config
    assert window.init_kwargs["api_acquire"] == host._acquire_chat_slot
    assert window.init_kwargs["api_release"] == host._release_chat_slot
    assert window.init_kwargs["video_dir"] == "video-dir-for-None"
    assert window.show_calls == 1
    assert window.raise_calls == 1
    assert host._video_clip_window is window


def test_reopen_reuses_singleton():
    """再次打开复用单例：不新建实例，仅重新 show/置前。"""
    host = _make_host()
    host._open_video_clip()
    host._open_video_clip()
    assert len(_StubEditorWindow.instances) == 1
    assert _StubEditorWindow.instances[0].show_calls == 2


def test_import_failure_degrades_with_hint(monkeypatch):
    """PyAV/SDL2 缺失（导入失败）：标题窗降级提示，单例保持 None。"""
    monkeypatch.delattr(editor_window_module, "EditorWindow")
    host = _make_host()
    host._open_video_clip()
    assert host._video_clip_window is None
    assert host.hints == ["视频剪辑不可用：缺少 PyAV/SDL2 组件"]


def test_destroyed_clears_singleton():
    """窗口销毁回调：单例引用清空（关闭即销毁，下次打开重建）。"""
    host = _make_host()
    host._open_video_clip()
    assert host._video_clip_window is not None
    host._on_video_clip_destroyed()
    assert host._video_clip_window is None


def test_closed_host_ignores_open():
    """宿主已关闭：打开请求被忽略（不创建窗口）。"""
    host = _make_host(_closed=True)
    host._open_video_clip()
    assert _StubEditorWindow.instances == []
    assert host._video_clip_window is None
