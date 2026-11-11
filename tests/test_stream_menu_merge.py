"""Menu 合并译文呈现（show_original_text=false）的离线单元测试。

choice 事件登记提问原文 → 自动翻译守卫通过后两级缓存解析提问译文
（未命中走独立线程、经队列回传，独立世代号作废迟到结果）→ 与选项译文
整合为一段整替上屏；选项翻译目标（编号拼接，兼作缓存键）与既有链路不变。

stub 宿主沿用 test_stream_follow.py 的模式：继承 StreamTranslateMixin 获得
翻译链路真实实现，host_core 的 StreamOverlayCore 方法以未绑定调用
（``StreamOverlayCore._handle_choice(host, ...)``）复用；正文写入经
``displayed`` 列表记录，不构造真实窗口、不触网络线程（worker 与
_start_translation 经 monkeypatch 受控）。
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from renpy_overlay.stream.host_core import StreamOverlayCore
from renpy_overlay.stream.translate_flow import StreamTranslateMixin
from renpy_overlay.translation_cache import TranslationCache

PROMPT = "Practice with Darci?"
CHOICE_TEXT = "1. Yes.\n2. No."
PROMPT_TRANSLATED = "要和 Darci 练习吗？"
CHOICE_TRANSLATED = "1. 好。\n2. 不了。"
MERGED = PROMPT_TRANSLATED + "\n" + CHOICE_TRANSLATED + "\n"


class _MenuMergeHostStub(StreamTranslateMixin):
    """翻译链路最小宿主 stub（离线，不触 GUI 与网络线程）。"""

    _closed = False
    skip_injection = False
    _translating = False
    _recognizing = False
    _chat_busy = False
    _locked = True
    _lock_started_at = 0.0
    _auto_translate_on = True
    _auto_skip_reason: str | None = None
    _current_stop = None
    _translation_seq = 0
    _stream_seq = 0
    _last_say: dict | None = None
    _last_translation_input: str | None = None
    _translation_store = None
    _asr_rt = SimpleNamespace(enabled=False)

    def __init__(self, show_original_text: bool = False) -> None:
        self._config = SimpleNamespace(
            show_original_text=show_original_text,
            translate_window_style=1,
            auto_translate_interval=1.0,
            api_base_url="http://127.0.0.1:1",
            api_timeout=5.0,
            model="m",
            system_prompt="",
            api_key="",
            enable_thinking=False,
            reasoning_effort="none",
        )
        self._menu_prompt_pending_input: str | None = None
        self._menu_prompt_input: str | None = None
        self._menu_prompt_state = "idle"
        self._menu_prompt_translated: str | None = None
        self._menu_prompt_seq = 0
        self._menu_prompt_stop = None
        self._menu_choice_translated: str | None = None
        self._translation_cache = TranslationCache(max_bytes=64 * 1024)
        self._queue: queue.Queue = queue.Queue()
        self.displayed: list[str] = []
        self.titles: list[str] = []

    def _display_text(self, text: str) -> None:
        self.displayed.append(text)

    def _update_title(self, text: str) -> None:
        self.titles.append(text)


@pytest.fixture()
def _no_network(monkeypatch: pytest.MonkeyPatch) -> dict:
    """替换线程入口为记录器：离线测试不真发翻译请求，按需手动模拟结果。"""
    calls: list[tuple[int, str]] = []

    def fake_worker(self, seq: int, what: str, api_options: dict) -> None:
        calls.append((seq, what))

    monkeypatch.setattr(StreamTranslateMixin, "_translate_menu_prompt_worker", fake_worker)

    def fake_start(self, origin: str = "manual") -> None:
        # 模拟 _start_translation 的世代号递增（不真发请求）
        self._translation_seq += 1
        calls.append((-1, str((self._last_say or {}).get("what") or "")))

    monkeypatch.setattr(StreamTranslateMixin, "_start_translation", fake_start)
    return calls


def _feed_choice(host: _MenuMergeHostStub, what: str = PROMPT) -> None:
    StreamOverlayCore._handle_choice(
        host, {"items": ["Yes.", "No."], "who": "", "what": what}
    )


def test_cached_both_sides_merge_presents_once(_no_network):
    """两级缓存双命中：一次 auto check 完成提问解析与选项呈现，合并整替上屏。"""
    host = _MenuMergeHostStub()
    host._translation_cache.put(PROMPT, PROMPT_TRANSLATED)
    host._translation_cache.put(CHOICE_TEXT, CHOICE_TRANSLATED)

    _feed_choice(host)
    assert host._menu_prompt_pending_input == PROMPT
    assert host.displayed == []  # 登记阶段不上屏

    host._auto_translate_check(immediate=True)

    assert host.displayed == [MERGED]  # 恰一次整段替换（打字机由正文窗实现）
    assert host._menu_prompt_input is None  # 呈现后清空合并状态
    assert host._menu_prompt_state == "idle"
    assert host._last_translation_input == CHOICE_TEXT  # 选项翻译去重标记照常


def test_prompt_pending_defers_until_worker_result(_no_network, monkeypatch):
    """提问未命中缓存：选项译文暂存等待，线程结果回队后合并上屏并写缓存。"""
    host = _MenuMergeHostStub()
    host._translation_cache.put(CHOICE_TEXT, CHOICE_TRANSLATED)
    monkeypatch.setattr(
        StreamTranslateMixin,
        "_translate_menu_prompt_worker",
        lambda self, seq, what, api_options: None,  # 线程立即返回，结果手动模拟
    )

    _feed_choice(host)
    host._auto_translate_check(immediate=True)

    seq = host._menu_prompt_seq
    assert seq >= 1 and host._menu_prompt_state == "pending"
    assert host._menu_prompt_input == PROMPT
    assert host._menu_choice_translated == CHOICE_TRANSLATED  # 选项译文已暂存
    assert host.displayed == []  # 提问在途：不呈现

    host._handle_menu_prompt_result(
        {"seq": seq, "ok": True, "what": PROMPT_TRANSLATED, "input": PROMPT}
    )
    assert host.displayed == [MERGED]
    assert host._translation_cache.get(PROMPT) == PROMPT_TRANSLATED  # 结果写缓存


def test_new_say_invalidates_pending_merge(_no_network):
    """新对话到达：作废未完成的合并，迟到的提问结果被世代号丢弃、不上屏。"""
    host = _MenuMergeHostStub()
    host._translation_cache.put(CHOICE_TEXT, CHOICE_TRANSLATED)

    _feed_choice(host)
    host._auto_translate_check(immediate=True)
    stale_seq = host._menu_prompt_seq
    assert host._menu_prompt_input == PROMPT

    StreamOverlayCore._handle_say(host, {"who": "Darci", "what": "New line."})
    assert host._menu_prompt_input is None

    host._handle_menu_prompt_result(
        {"seq": stale_seq, "ok": True, "what": PROMPT_TRANSLATED, "input": PROMPT}
    )
    assert host.displayed == []  # 迟到结果不整替正文


def test_no_prompt_menu_presents_choice_only(_no_network):
    """无提问菜单（what 为空）：只呈现选项译文，不用历史台词冒充提问。"""
    host = _MenuMergeHostStub()
    host._translation_cache.put(CHOICE_TEXT, CHOICE_TRANSLATED)

    _feed_choice(host, what="")
    host._auto_translate_check(immediate=True)

    assert host.displayed == [CHOICE_TRANSLATED + "\n"]


def test_chunk_suppressed_and_translation_finalizes_merge(_no_network):
    """选项走流式 API：合并期间增量不入正文，完成后与提问译文整合上屏。"""
    host = _MenuMergeHostStub()
    host._translation_cache.put(PROMPT, PROMPT_TRANSLATED)

    _feed_choice(host)
    host._auto_translate_check(immediate=True)  # 提问缓存命中；选项未命中 → 发起
    seq = host._translation_seq
    assert seq >= 1

    StreamOverlayCore._handle_chunk(host, {"seq": seq, "text": CHOICE_TRANSLATED})
    assert host.displayed == []  # 合并期间增量被抑制

    StreamTranslateMixin._handle_translation(
        host,
        {
            "seq": seq,
            "ok": True,
            "who": "",
            "what": CHOICE_TRANSLATED,
            "input": CHOICE_TEXT,
            "origin": "auto",
        },
    )
    assert host.displayed == [MERGED]
    assert host._translation_cache.get(CHOICE_TEXT) == CHOICE_TRANSLATED


def test_original_text_mode_merges_translation_too(_no_network):
    """show_original_text=true：先显示原文合并段，译文就绪后整替为合并译文
    （提问译文 + 选项译文），与 false 模式共用同一合并链路。"""
    host = _MenuMergeHostStub(show_original_text=True)
    host._translation_cache.put(PROMPT, PROMPT_TRANSLATED)
    host._translation_cache.put(CHOICE_TEXT, CHOICE_TRANSLATED)

    _feed_choice(host)
    assert host.displayed == [PROMPT + "\n" + CHOICE_TEXT + "\n"]  # 原文合并段先显
    assert host._menu_prompt_pending_input == PROMPT  # 提问同样登记进合并链路

    host._auto_translate_check(immediate=True)
    assert len(host.displayed) == 2
    assert host.displayed[-1] == MERGED  # 合并译文整段替换原文段（两模式同构）
