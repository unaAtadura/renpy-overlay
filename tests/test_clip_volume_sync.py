"""音量同步 bug 回归测试（offscreen 窗口 + 桩引擎，无 SDL/av 依赖）。

覆盖场景（bug：首次播放音量条失效、静音不生效，拖动一次后恢复）：
- 构造完成后引擎必须收到音量条初值（静音 0 → setVolume(0.0)）；
- 拖动音量条照常传导（回归保护）；
- open_path 的「打开新视频默认静音」在值变化时照常传导。

桩引擎只记录 setVolume/open 调用，不启解码线程。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from renpy_overlay.video.clip.player_pane import PlayerPane


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class StubPlayer(QObject):
    """桩引擎：记录音量/打开调用；信号面与 VideoPlayer 门面对齐。"""

    media_opened = pyqtSignal(object)
    position_changed = pyqtSignal(int)
    state_changed = pyqtSignal(object)
    open_failed = pyqtSignal(str)
    ended = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.volume_calls: list[float] = []
        self.open_calls: list[str] = []

    # ---- 引擎门面 API（仅记录，不实现） ----

    def open(self, path: str) -> None:
        self.open_calls.append(path)

    def setPosition(self, position_ms: int) -> None: ...

    def play(self) -> None: ...

    def pause(self) -> None: ...

    def playbackState(self) -> int:
        return 0

    def position(self) -> int:
        return 0

    def duration(self) -> int:
        return -1

    def hasMedia(self) -> bool:
        return False

    def setVolume(self, volume: float) -> None:
        self.volume_calls.append(volume)

    def volume(self) -> float:
        return self.volume_calls[-1] if self.volume_calls else 1.0

    def ensure_sdl(self) -> None: ...

    def set_renderer(self, renderer) -> None: ...

    def renderer_wiring(self) -> dict:
        return {}

    def stats_snapshot(self) -> dict:
        return {}


def _make_window() -> tuple[PlayerPane, StubPlayer]:
    holder: dict = {}

    def factory(parent):
        stub = StubPlayer(parent)
        holder["player"] = stub
        return stub

    window = PlayerPane(player_factory=factory)
    return window, holder["player"]


def test_initial_volume_synced_to_engine(qapp):
    """构造完成后引擎音量必须与音量条初值一致（静音 0 → setVolume(0.0)）。

    bug 复现：修复前 valueChanged 连接时滑块已是 0（构造期设值早于连接，
    且值未变化不发射信号），引擎从未收到 setVolume、保持默认全音量 1.0，
    首次播放静音失效；断言 volume_calls == [0.0] 修复前失败。
    """
    window, player = _make_window()
    assert window._volume_slider.value() == 0
    assert player.volume_calls == [0.0]


def test_slider_change_propagates(qapp):
    """拖动/滚轮改变音量条 → 照常传导到引擎（回归保护）。"""
    window, player = _make_window()
    window._volume_slider.setValue(50)
    assert player.volume_calls[-1] == pytest.approx(0.5)
    window._volume_slider.setValue(100)
    assert player.volume_calls[-1] == pytest.approx(1.0)


def test_open_path_resets_volume_when_slider_dirty(qapp, tmp_path, monkeypatch):
    """open_path 静音重置：滑块值有变化时照常传导（打开新视频默认静音）。"""
    # av_available 为 from-import：patch 消费模块（player_pane）的本地引用
    import renpy_overlay.video.clip.player_pane as pane_mod

    monkeypatch.setattr(pane_mod, "av_available", lambda: True)
    media = tmp_path / "a.mp4"
    media.write_bytes(b"")
    window, player = _make_window()
    window._volume_slider.setValue(60)
    window.open_path(str(media))
    assert window._volume_slider.value() == 0
    assert player.volume_calls[-1] == 0.0
    assert player.open_calls == [str(media)]


def test_engine_volume_follows_slider_after_mute_cycle(qapp):
    """静音 → 调高 → 再静音的全周期传导（修复不得破坏既有交互）。"""
    window, player = _make_window()
    window._volume_slider.setValue(80)
    window._volume_slider.setValue(0)
    window._volume_slider.setValue(30)
    assert player.volume_calls == [
        pytest.approx(v) for v in (0.0, 0.8, 0.0, 0.3)
    ]


