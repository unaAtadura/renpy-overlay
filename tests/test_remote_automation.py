"""遥控器自动化控制器的离线验证：纯函数 + stub 注入，不构造真实 QWidget。

覆盖：网格灰度感知签名（同图同签名、轻微噪点相似、大区域小字幕变化可检出）、
变化格占比与判变阈值、RemoteAutoCache 循环队列（尾插/满淘汰队首/命中移尾）、
控制器状态机（开启前提提示、A 点击首轮/重启轮、周期变化判定、忙碌挂起
与恢复、本控制器在途不置挂起、B 离开/换区/目标销毁停止、菜单关闭保留
单击语义、隐藏遥控器不停自动化、快捷键模式退出复位、宿主拒受保持挂起、
结果回执清在途并回填缓存、装填期提示框压制、注入契约 fail-fast）；
以及宿主侧「成功即落库」（stub ScreenshotStore：成功恰写一条，失败/中止/
忙碌被拒/缓存命中零写入，store 降级/抛错不影响上屏与在途回执）。

真实截图 / win32api / QTimer 均经构造注入替身；图像用 Pillow 现场生成；
视觉 API 用 monkeypatch 替换（命中/中止用例一旦被调用即失败）。
"""

from __future__ import annotations

import queue
import random
import threading
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from renpy_overlay.screenshot.remote_automation import (
    CACHE_PRUNE_CANDIDATE_FRACTION,
    CACHE_PRUNE_WATERMARK_RATIO,
    MSG_NEED_HOTKEY_MODE,
    REMOTE_AUTO_OFF_TEXT,
    REMOTE_AUTO_ON_TEXT,
    REMOTE_HIDE_TEXT,
    REMOTE_SHOW_TEXT,
    SIGNATURE_CACHE_HIT_RATIO,
    SIGNATURE_CACHE_INFORMATIVE_SPREAD,
    SIGNATURE_CACHE_PRESCREEN_TOLERANCE,
    SIGNATURE_CACHE_QUERY_LIMIT,
    SIGNATURE_CHANGE_RATIO,
    SIGNATURE_SIZE,
    RemoteAutoCache,
    RemoteAutomationController,
    bytes_to_signature,
    grid_signature,
    region_means,
    signature_change_ratio,
    signature_is_informative,
    signature_to_bytes,
    signatures_match_for_cache,
    signatures_similar,
)
from renpy_overlay.stream import screenshot_flow as flow_module
from renpy_overlay.stream.screenshot_flow import ScreenshotFlowMixin

# ---- 图像替身：确定性生成的测试画面 -------------------------------------------------


def _solid_image(color: int, size: tuple[int, int] = (64, 48)) -> Image.Image:
    """恒色图（所有格子灰度一致，作「空白画面」基准）。"""
    return Image.new("RGB", size, (color, color, color))


def _gradient_image(offset: int = 0, size: tuple[int, int] = (64, 48)) -> Image.Image:
    """水平渐变 + 行偏移的「有内容画面」。"""
    image = Image.new("L", size)
    image.putdata([(x * 4 + y + offset) % 256 for y in range(size[1]) for x in range(size[0])])
    return image.convert("RGB")


def _noisy_variant(image: Image.Image, seed: int = 7, noise_pixels: int = 6) -> Image.Image:
    """在副本上扰动少量像素亮度（模拟屏幕噪点，人眼难感知的变化）。"""
    rng = random.Random(seed)
    variant = image.copy()
    data = list(variant.getdata())
    for _ in range(noise_pixels):
        index = rng.randrange(len(data))
        r, g, b = data[index]
        delta = 10 if index % 2 == 0 else -10
        data[index] = (
            max(0, min(255, r + delta)),
            max(0, min(255, g + delta)),
            max(0, min(255, b + delta)),
        )
    variant.putdata(data)
    return variant


#: 字幕字体（测试套件本就依赖 Windows：win32api 为顶层依赖）
_SUBTITLE_FONT = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 30)


def _subtitle_frame(text: str) -> Image.Image:
    """真实漏检场景的等比例构造：1878x288 大横条 + 左上角白字黑边字幕。

    对应 screenshot.db 样本 11/12（背景完全相同、仅字幕文本不同，
    字幕占画面面积极小——64bit dHash 在该场景距离仅 0~1 而漏检）。
    """
    image = Image.new("RGB", (1878, 288), (150, 148, 145))
    draw = ImageDraw.Draw(image)
    draw.rectangle([900, 60, 1500, 288], fill=(40, 38, 40))  # 静态深色前景
    draw.ellipse([60, 120, 420, 288], fill=(210, 206, 198))  # 静态浅色前景
    draw.text(
        (55, 40),
        text,
        fill=(255, 255, 255),
        font=_SUBTITLE_FONT,
        stroke_width=2,
        stroke_fill=(0, 0, 0),
    )
    return image


# ---- 网格签名 / 变化格占比 / 相似判定（纯函数） ------------------------------------


def test_grid_signature_deterministic_and_uniform():
    assert grid_signature(_gradient_image()) == grid_signature(_gradient_image())
    assert grid_signature(_solid_image(128)) == tuple([128] * (SIGNATURE_SIZE[0] * SIGNATURE_SIZE[1]))


def test_grid_signature_tolerates_tiny_noise():
    base = _subtitle_frame("Fata Morgana.")
    assert signatures_similar(grid_signature(base), grid_signature(_noisy_variant(base)))


def test_grid_signature_different_content_not_similar():
    assert not signatures_similar(
        grid_signature(_gradient_image()), grid_signature(_solid_image(200))
    )


def test_grid_signature_detects_subtitle_change_in_large_region():
    """回归（真实漏检样本场景）：大横条区域仅角部字幕变化必须判为变化。

    真实样本（screenshot.db id=11/12，1878x288）dHash(9x8) 距离仅 1 而
    漏检；网格签名同比例构造下变化格占比远超 1.5% 判变阈值。
    """
    first = grid_signature(_subtitle_frame("Fata Morgana."))
    second = grid_signature(
        _subtitle_frame(
            "*chuckles* Sounds cool, but no way in hell I'm calling you that all the time."
        )
    )
    ratio = signature_change_ratio(first, second)
    assert ratio >= SIGNATURE_CHANGE_RATIO, f"变化格占比 {ratio:.2%} 应达判变阈值"
    assert not signatures_similar(first, second)
    # 同帧重绘（背景/字幕全同）必须判为无变化
    assert signatures_similar(first, grid_signature(_subtitle_frame("Fata Morgana.")))


def test_signature_change_ratio_and_thresholds():
    """占比判据边界（合成签名，不依赖图像渲染）。"""
    base = tuple([100] * 512)

    def perturbed(cells: int, delta: int = 30) -> tuple[int, ...]:
        signature = list(base)
        for index in range(cells):
            signature[index] += delta
        return tuple(signature)

    assert signature_change_ratio(base, base) == 0.0
    assert signature_change_ratio(base, tuple([200] * 512)) == 1.0
    assert signature_change_ratio(base, base[:256]) == 1.0  # 长度不一致保守判变化
    # 8/512 = 1.56% ≥ 1.5% 判变阈值 → 变化；7/512 ≈ 1.37% → 无变化
    assert not signatures_similar(base, perturbed(8))
    assert signatures_similar(base, perturbed(7))


# ---- 持久化缓存纯函数：命中判定 / 区均值预筛 / 信息量护栏 / 序列化 ------------------


def test_signatures_match_for_cache_zero_tolerance():
    """零超阈格判据：逐格差恰 8 命中、恰 9 不命中（与判变阈值完全解耦）。"""
    base = tuple([100] * 512)
    at_threshold = tuple(v + 8 for v in base)  # 全部格 |Δ|=8：不超阈
    over_threshold = tuple(v + 9 for v in base)  # 全部格 |Δ|=9：全超阈
    assert signatures_match_for_cache(base, at_threshold) is True
    assert signatures_match_for_cache(base, over_threshold) is False
    assert signatures_match_for_cache(base, base) is True
    # 长度不一致保守不命中（与 signature_change_ratio 的 1.0 语义一致）
    assert signatures_match_for_cache(base, base[:256]) is False


def test_region_means_partition_order_and_uniform():
    """分区顺序：上排四区（区 0-3）后下排四区（区 4-7）；恒色签名各区等于该色。"""
    uniform = tuple([128] * 512)
    assert region_means(uniform) == (128,) * 8
    # 区 k = row*4 + col（row/col 为 8 格分块的行/列）；行主序存储下每行
    # 前 8 格属同一区，跨 8 行共 64 格/区
    segmented = tuple(
        ((y // 8) * 4 + (x // 8)) * 10 for y in range(16) for x in range(32)
    )
    assert region_means(segmented) == tuple(k * 10 for k in range(8))


def test_region_means_prescreen_is_lossless_for_matches():
    """无损性正向：零超阈命中对的全部区均值差 ≤ 预筛阈值（不会被误排斥）。"""
    rng = random.Random(2026)
    for _ in range(32):
        base = tuple(rng.randrange(0, 256) for _ in range(512))
        near = tuple(
            max(0, min(255, v + rng.randint(-8, 8))) for v in base
        )
        assert signatures_match_for_cache(base, near)
        for a_mean, b_mean in zip(region_means(base), region_means(near), strict=True):
            assert abs(a_mean - b_mean) <= SIGNATURE_CACHE_PRESCREEN_TOLERANCE


def test_region_means_prescreen_excludes_distant_pairs():
    """无损性逆否：区均值差超预筛阈值的对必然不命中（排除是安全的）。"""
    blank = tuple([10] * 512)
    bright = tuple([250] * 512)
    for a_mean, b_mean in zip(region_means(blank), region_means(bright), strict=True):
        assert abs(a_mean - b_mean) > SIGNATURE_CACHE_PRESCREEN_TOLERANCE
    assert not signatures_match_for_cache(blank, bright)


def test_signature_is_informative_boundaries():
    """信息量护栏：极差恰阈值可写入、低一档拒写、恒色拒写、空签名拒写。"""
    informative = tuple([100] * 256 + [100 + SIGNATURE_CACHE_INFORMATIVE_SPREAD] * 256)
    barely_blank = tuple([100] * 256 + [100 + SIGNATURE_CACHE_INFORMATIVE_SPREAD - 1] * 256)
    assert signature_is_informative(informative)
    assert not signature_is_informative(barely_blank)
    assert not signature_is_informative(tuple([128] * 512))
    assert not signature_is_informative(())


def test_signature_bytes_roundtrip():
    """序列化往返无损：512 格签名 → bytes → 签名完全相等。"""
    rng = random.Random(7)
    signature = tuple(rng.randrange(0, 256) for _ in range(512))
    payload = signature_to_bytes(signature)
    assert len(payload) == 512
    assert bytes_to_signature(payload) == signature


def test_cache_hit_ratio_is_stricter_than_change_ratio():
    """命中阈值（0.0）必须严格严于判变阈值（0.015）：缓存宁漏命中不误命中。"""
    assert SIGNATURE_CACHE_HIT_RATIO < SIGNATURE_CHANGE_RATIO


def test_cache_prune_constants_in_range():
    """水位与冷端候选比例的取值约束：水位 0<ratio<1、候选 0<ratio<=1。"""
    assert 0 < CACHE_PRUNE_WATERMARK_RATIO < 1
    assert 0 < CACHE_PRUNE_CANDIDATE_FRACTION <= 1
    assert SIGNATURE_CACHE_QUERY_LIMIT > 0


# ---- RemoteAutoCache：循环队列语义 -------------------------------------------------

#: 测试签名常量：恒色网格两两逐格差 ≥50，变化格占比 100%（互不相似）
_SIG_A = tuple([100] * 512)
_SIG_B = tuple([200] * 512)
_SIG_C = tuple([50] * 512)
_SIG_D = tuple([0] * 512)


def test_cache_put_lookup_and_capacity_eviction():
    cache = RemoteAutoCache(2)
    cache.put(_SIG_A, "最早")
    cache.put(_SIG_B, "次新")
    assert len(cache) == 2
    cache.put(_SIG_C, "最新")  # 满：淘汰队首
    assert len(cache) == 2
    assert cache.lookup(_SIG_A) is None  # 最早的已被顶掉
    assert cache.lookup(_SIG_C) == "最新"


def test_cache_lookup_hit_moves_entry_to_tail():
    cache = RemoteAutoCache(3)
    cache.put(_SIG_A, "A")
    cache.put(_SIG_B, "B")
    cache.put(_SIG_C, "C")
    assert cache.lookup(_SIG_A) == "A"  # 命中移尾
    cache.put(_SIG_D, "D")  # 满淘汰队首（应为 B 而非刚命中的 A）
    assert cache.lookup(_SIG_B) is None
    assert cache.lookup(_SIG_A) == "A"


def test_cache_lookup_near_signature_hits():
    """近似命中：单格超阈（占比 1/512 < 1.5%）视为同一画面。"""
    cache = RemoteAutoCache(2)
    cache.put(_SIG_A, "A")
    near = list(_SIG_A)
    near[0] += 30
    assert cache.lookup(tuple(near)) == "A"


def test_cache_lookup_miss_returns_none_and_capacity_one():
    cache = RemoteAutoCache(3)
    assert cache.lookup(_SIG_A) is None
    tiny = RemoteAutoCache(1)
    tiny.put(_SIG_A, "旧")
    tiny.put(_SIG_B, "新")
    assert len(tiny) == 1
    assert tiny.lookup(_SIG_B) == "新"


# ---- 控制器测试替身 -----------------------------------------------------------------


class _StubTimer:
    """QTimer 替身：记录启动/停止与间隔，on_tick 由测试手动触发。"""

    def __init__(self) -> None:
        self.interval_ms: int | None = None
        self.on_tick = None
        self.started = 0
        self.running = False

    def start(self) -> None:
        self.started += 1
        self.running = True

    def stop(self) -> None:
        self.running = False


class _StubWindow:
    """装填目标（截图窗口）替身：仅作对象标识与 bbox 记录。"""

    def __init__(self, name: str) -> None:
        self.name = name


class _Harness:
    """控制器 + 全套注入记录器（提交/上屏/提示/压制/显隐/截图/计时器）。"""

    def __init__(self, submit_ok: bool = True) -> None:
        self.timer = _StubTimer()
        self.windows = True  # has_windows 返回值
        self.busy = False
        self.submit_ok = submit_ok
        self.windows_hidden = False
        self.fail_bbox = False
        self.fail_capture = False
        self.image: Image.Image = _gradient_image()
        self.notified: list[str] = []
        self.displayed: list[str] = []
        self.submitted: list[tuple[object, int]] = []
        self.frames: list[tuple[object, bool]] = []
        self.controller = RemoteAutomationController(
            interval=0.5,
            cache_size=3,
            has_windows=lambda: self.windows,
            is_busy=lambda: self.busy,
            display=self.displayed.append,
            submit=self._submit,
            notify=self.notified.append,
            suppress_frame=lambda window, suppressed: self.frames.append((window, suppressed)),
            get_windows_hidden=lambda: self.windows_hidden,
            set_windows_hidden=self._set_windows_hidden,
            bbox_provider=self._bbox,
            capture_fn=self._capture,
            timer_factory=self._make_timer,
        )

    def _submit(self, window, image, hash_value: int) -> bool:
        if not self.submit_ok:
            return False
        self.submitted.append((window, hash_value))
        return True

    def _set_windows_hidden(self, value: bool) -> None:
        self.windows_hidden = value

    def _bbox(self, window):
        if self.fail_bbox:
            raise RuntimeError("window gone")
        return (0, 0, 100, 100)

    def _capture(self, bbox) -> Image.Image:
        if self.fail_capture:
            raise RuntimeError("capture failed")
        return self.image

    def _make_timer(self, interval_ms: int, on_tick) -> _StubTimer:
        self.timer.interval_ms = interval_ms
        self.timer.on_tick = on_tick
        return self.timer

    def tick(self) -> None:
        self.timer.on_tick()

    def finish_flight(self) -> None:
        """以当前唯一在途提交的结果回执清除在途标记（默认成功译文）。"""
        window, hash_value = self.submitted[-1]
        self.controller.on_translation_result(hash_value, "译文")


# ---- 菜单文案与开启前提（用户决议 A3） ----------------------------------------------


def test_menu_text_constants():
    assert REMOTE_AUTO_OFF_TEXT == "遥控器自动化: 关"
    assert REMOTE_AUTO_ON_TEXT == "遥控器自动化: 开"
    assert REMOTE_HIDE_TEXT == "隐藏遥控器"
    assert REMOTE_SHOW_TEXT == "显示遥控器"


def test_constructor_rejects_non_callable_injection():
    """注入契约回归：宿主误传 property 取值（bool）时构造期即报错。

    背景：曾因 stream_window 组装处传 windows_created（property 取值）而非
    lambda，菜单点击「遥控器自动化」时抛 TypeError（'bool' object is not
    callable）。构造期 fail-fast 后此类装配错误在启动即暴露。
    """
    callbacks = dict(
        is_busy=lambda: False,
        display=lambda text: None,
        submit=lambda window, image, hash_value: True,
        notify=lambda text: None,
        suppress_frame=lambda window, suppressed: None,
        get_windows_hidden=lambda: False,
        set_windows_hidden=lambda value: None,
    )
    with pytest.raises(TypeError, match="has_windows"):
        RemoteAutomationController(0.5, 3, has_windows=False, **callbacks)


def test_enable_without_windows_notifies_and_stays_off():
    h = _Harness()
    h.windows = False  # 快捷键模式未开启（A/B 未生成）
    h.controller.toggle_automation()
    assert not h.controller.enabled
    assert h.notified == [MSG_NEED_HOTKEY_MODE]
    assert MSG_NEED_HOTKEY_MODE == "请先开启快捷键模式"


def test_enable_activates_capability_without_cycle():
    h = _Harness()
    h.controller.toggle_automation()
    assert h.controller.enabled
    assert not h.timer.running  # 不启动周期：等待 A 单击


# ---- A 单击：首轮 / 重启轮（用户决议 A5） -------------------------------------------


def test_first_click_submits_and_starts_cycle():
    h = _Harness()
    h.controller.toggle_automation()
    window = _StubWindow("w")
    h.controller.on_trigger_clicked(window)
    assert len(h.submitted) == 1
    assert h.submitted[0][0] is window
    assert h.timer.running
    assert h.timer.interval_ms == 500


def test_first_click_cache_hit_displays_without_api():
    h = _Harness()
    h.controller.toggle_automation()
    signature_a = grid_signature(h.image)
    h.controller.on_translation_result(signature_a, "预存译文")  # 预填缓存
    h.controller.on_trigger_clicked(_StubWindow("w"))
    assert h.displayed == ["预存译文"]  # 命中直接上屏
    assert h.submitted == []  # 不发 API
    assert h.timer.running  # 仍进入自动状态


def test_click_during_auto_restarts_round_and_keeps_cycle():
    h = _Harness()
    h.controller.toggle_automation()
    window = _StubWindow("w")
    h.controller.on_trigger_clicked(window)
    h.finish_flight()
    h.image = _gradient_image(offset=40)
    h.controller.on_trigger_clicked(window)  # auto 中点击 A = 重启一轮
    assert len(h.submitted) == 2
    assert h.timer.running  # 周期继续
    h.tick()  # 与重启基准相同：不再提交
    assert len(h.submitted) == 2


# ---- 周期 tick：变化判定 / 缓存命中 / 忙碌挂起（决议 A7） ----------------------------


def test_tick_without_change_does_not_resubmit():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.finish_flight()
    h.tick()  # 同一画面
    assert len(h.submitted) == 1


def test_tick_change_submits_and_updates_baseline():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.finish_flight()
    h.image = _gradient_image(offset=40)  # 画面变化
    h.tick()
    assert len(h.submitted) == 2
    h.tick()  # 与新基准相同
    assert len(h.submitted) == 2


def test_cache_hit_on_return_to_previous_content():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.controller.on_translation_result(h.submitted[-1][1], "A 的译文")
    h.image = _gradient_image(offset=40)  # → B
    h.tick()
    h.controller.on_translation_result(h.submitted[-1][1], "B 的译文")
    h.image = _gradient_image()  # → 回到 A
    h.tick()
    assert h.displayed == ["A 的译文"]  # 命中缓存直接上屏
    assert len(h.submitted) == 2  # 不发 API


def test_busy_tick_keeps_pending_and_recovers_on_next_tick():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.finish_flight()
    h.busy = True  # 他人占用在途（如手动翻译/识曲/对话）
    h.image = _gradient_image(offset=40)
    h.tick()
    assert len(h.submitted) == 1  # 忙碌：保持变化状态不提交
    h.busy = False
    h.tick()  # 恢复分支：先查缓存（未命中）→ 提交
    assert len(h.submitted) == 2


def test_own_flight_does_not_mark_pending():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))  # 本控制器在途（未回执）
    h.busy = True  # 忙碌源于自己的在途请求
    h.image = _gradient_image(offset=40)
    h.tick()
    assert len(h.submitted) == 1  # 不重复提交、也不置恢复挂起
    h.controller.on_translation_result(h.submitted[0][1], "译文")
    h.busy = False
    h.tick()  # 回执后正常与基准比较：检测到变化 → 提交
    assert len(h.submitted) == 2


def test_submit_rejected_keeps_pending_for_retry():
    h = _Harness(submit_ok=False)
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))  # 首轮提交被宿主拒绝
    assert h.submitted == []
    h.submit_ok = True
    h.tick()  # 恢复分支重试
    assert len(h.submitted) == 1


def test_failed_result_not_cached_and_in_flight_cleared():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.controller.on_translation_result(h.submitted[0][1], None)  # 失败回执
    assert len(h.controller._cache) == 0  # 失败不污染缓存
    h.image = _gradient_image(offset=40)
    h.tick()  # 在途已清：变化正常进入提交路径
    assert len(h.submitted) == 2


# ---- B 装填变化：停止 / 目标失效 / 提示框压制 ----------------------------------------


def test_armed_leave_stops_cycle_and_waits_for_click():
    h = _Harness()
    h.controller.toggle_automation()
    window = _StubWindow("w")
    h.controller.on_trigger_clicked(window)
    h.finish_flight()
    h.controller.on_armed_changed(None)  # B 离开截图区域
    assert not h.timer.running
    h.image = _gradient_image(offset=40)
    h.tick()  # 停止后周期不再工作
    assert len(h.submitted) == 1
    h.controller.on_armed_changed(window)  # B 重新进入
    h.controller.on_trigger_clicked(window)  # 等待再次点击启动第一轮
    assert h.timer.running
    assert len(h.submitted) == 2


def test_armed_switch_to_other_window_stops_cycle():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("1"))
    h.controller.on_armed_changed(_StubWindow("2"))  # 捕获新的截图区域
    assert not h.timer.running


def test_armed_target_destroyed_stops_cycle_safely():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.fail_bbox = True  # 装填目标窗口销毁（GetWindowRect 失败）
    h.tick()
    assert not h.timer.running


def test_capture_failure_keeps_cycle_for_retry():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.finish_flight()
    h.fail_capture = True
    h.tick()
    assert h.timer.running  # 普通抓屏失败：保持周期
    h.fail_capture = False
    h.tick()
    assert h.timer.running


def test_armed_suppresses_frame_only_when_enabled():
    h = _Harness()
    window = _StubWindow("w")
    h.controller.on_armed_changed(window)  # 未开启：不压制提示框
    assert h.frames == []
    h.controller.toggle_automation()  # 开启时 B 已装填：同步压制（隐藏）
    assert h.frames == [(window, True)]
    h.controller.on_armed_changed(window)  # 同一目标重复通知：无事件
    assert h.frames == [(window, True)]
    h.controller.on_armed_changed(None)  # 离开：解除压制，恢复显示
    assert h.frames == [(window, True), (window, False)]


# ---- 菜单关闭 / 隐藏遥控器 / 生命周期 -------------------------------------------------


def test_disable_stops_cycle_and_keeps_state_for_manual():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.finish_flight()
    h.controller.toggle_automation()  # 菜单关闭
    assert not h.controller.enabled
    assert not h.timer.running
    h.controller.on_trigger_clicked(_StubWindow("w"))  # 关闭后 A 单击不再被接管
    assert len(h.submitted) == 1


def test_disable_restores_suppressed_frame():
    h = _Harness()
    h.controller.toggle_automation()
    window = _StubWindow("w")
    h.controller.on_armed_changed(window)
    h.controller.toggle_automation()
    assert h.frames[-1] == (window, False)  # 关闭自动化：解除压制（恢复显示）


def test_toggle_remote_hidden_flips_user_state():
    h = _Harness()
    assert h.controller.remote_hidden is False
    h.controller.toggle_remote_hidden()
    assert h.windows_hidden is True
    assert h.controller.remote_hidden is True
    h.controller.toggle_remote_hidden()
    assert h.windows_hidden is False


def test_remote_hidden_does_not_stop_automation():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.controller.toggle_remote_hidden()  # 决议 A6：隐藏不停止自动化
    assert h.timer.running


def test_hotkey_mode_off_resets_automation():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.controller.on_hotkey_mode_changed(False)
    assert not h.controller.enabled
    assert not h.timer.running
    h.controller.on_trigger_clicked(_StubWindow("w"))  # 复位后单击不再被接管
    assert len(h.submitted) == 1


def test_shutdown_stops_and_resets():
    h = _Harness()
    h.controller.toggle_automation()
    h.controller.on_trigger_clicked(_StubWindow("w"))
    h.controller.shutdown()
    assert not h.controller.enabled
    assert not h.timer.running
    h.controller.shutdown()  # 幂等
    assert not h.timer.running


# ---- 宿主侧「成功即落库」（ScreenshotFlowMixin + stub ScreenshotStore） -------------
#
# 需求：自动化翻译成功即入库（与手动截图翻译同规则）；失败/中止/忙碌被拒/
# 缓存命中一律零写入；store 降级/抛错只降级（上屏与在途回执不受影响）。


class _StubStore:
    """ScreenshotStore 替身：记录成功写入；fail=降级返回 None，explode=抛异常。"""

    def __init__(self, fail: bool = False, explode: bool = False) -> None:
        self.fail = fail
        self.explode = explode
        self.inserts: list[dict] = []

    def insert(self, ts: int, ocr_text: str, img_original: bytes, img_thumbnail: bytes) -> int | None:
        if self.explode:
            raise RuntimeError("db exploded")
        if not self.fail:
            self.inserts.append(
                {
                    "ts": ts,
                    "ocr_text": ocr_text,
                    "img_original": img_original,
                    "img_thumbnail": img_thumbnail,
                }
            )
        return None if self.fail else len(self.inserts)


class _StubAutomation:
    """RemoteAutomationController 替身：记录在途回执（签名, 译文）。"""

    def __init__(self) -> None:
        self.results: list[tuple[object, str | None]] = []

    def on_translation_result(self, signature: object, text: str | None) -> None:
        self.results.append((signature, text))


class _AutoHost(ScreenshotFlowMixin):
    """最小宿主替身：仅自动化提交/收尾路径依赖的属性与方法（不建 QWidget）。"""

    def __init__(self, store: _StubStore | None) -> None:
        self._closed = False
        self._translating = False
        self._recognizing = False
        self._chat_busy = False
        self._translation_seq = 1
        self._config = SimpleNamespace(screenshot_compress_percent=50)
        self._queue = queue.Queue()
        self._screenshot_store = store
        self._remote_automation = _StubAutomation()
        self.displayed: list[str] = []
        self.titles: list[str] = []

    def _display_text(self, text: str) -> None:
        self.displayed.append(text)

    def _update_title(self, text: str) -> None:
        self.titles.append(text)

    def worker_result(self) -> dict:
        """取走 worker 入队的一条回执（payload 部分）。"""
        return self._queue.get(timeout=5)[1]


_AUTO_SIG = tuple([100] * 512)


def _done_item(**overrides: object) -> dict:
    """worker 成功回执的默认形态（可按用例覆盖字段）。"""
    item = {
        "seq": 1,
        "signature": _AUTO_SIG,
        "text": "自动化译文",
        "aborted": False,
        "error": None,
        "original_jpeg": b"\xff\xd8original",
        "thumb_jpeg": b"\xff\xd8thumb",
    }
    item.update(overrides)
    return item


def test_auto_success_writes_exactly_one_record():
    """成功路径：恰好入库一条，字段与手动同规则（13 位毫秒 ts/译文/两图）。"""
    store = _StubStore()
    host = _AutoHost(store)
    host._handle_remote_auto_done(_done_item())
    assert len(store.inserts) == 1
    record = store.inserts[0]
    assert record["ocr_text"] == "自动化译文"  # 视觉链路只返回译文：与手动一致存译文
    assert record["img_original"] == b"\xff\xd8original"
    assert record["img_thumbnail"] == b"\xff\xd8thumb"
    assert 1_000_000_000_000 <= record["ts"] < 10_000_000_000_000  # 13 位毫秒
    assert host.displayed == ["自动化译文\n"]
    assert host.titles == ["翻译完成"]
    assert host._translating is False
    assert host._remote_automation.results == [(_AUTO_SIG, "自动化译文")]


def test_auto_failure_and_abort_write_nothing():
    """失败（空文本）与中止路径：零写入、不上屏；中止回执不带译文。"""
    store = _StubStore()
    host = _AutoHost(store)
    host._handle_remote_auto_done(_done_item(text="", error="空译文"))
    assert store.inserts == []
    assert host.displayed == []
    assert host.titles == ["翻译失败"]
    host_aborted = _AutoHost(store)
    host_aborted._handle_remote_auto_done(_done_item(aborted=True, text=None))
    assert store.inserts == []
    assert host_aborted.displayed == []
    assert host_aborted._remote_automation.results == [(_AUTO_SIG, None)]


def test_auto_stale_seq_writes_nothing():
    """seq 过期：零写入、不上屏；仅存结果在途回执仍送达控制器。"""
    store = _StubStore()
    host = _AutoHost(store)
    host._translation_seq = 2
    host._translating = True  # 互斥槽已被新一轮请求占用
    host._handle_remote_auto_done(_done_item())
    assert store.inserts == []
    assert host.displayed == []
    assert host._translating is True  # 过期结果不复位他人占用的互斥槽
    assert host._remote_automation.results == [(_AUTO_SIG, "自动化译文")]


def test_auto_submit_rejected_when_busy_writes_nothing():
    """忙碌被拒：提交不受理（不起 worker），store 零写入。"""
    store = _StubStore()
    host = _AutoHost(store)
    host._translating = True  # 任意在途翻译（互斥槽占用）
    accepted = host._submit_remote_auto_translation(
        _StubWindow("w"), _gradient_image(), _AUTO_SIG
    )
    assert accepted is False
    assert store.inserts == []


def test_auto_cache_hit_writes_nothing(monkeypatch):
    """缓存命中：直接上屏、不经 worker（API 桩一旦被调用即失败）→ store 零写入。"""

    def _forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("缓存命中不应发起 API")

    monkeypatch.setattr(flow_module, "translate_image_verified", _forbidden)
    store = _StubStore()
    host = _AutoHost(store)
    controller = RemoteAutomationController(
        interval=0.5,
        cache_size=3,
        has_windows=lambda: True,
        is_busy=lambda: False,
        display=host._display_remote_auto_text,
        submit=host._submit_remote_auto_translation,
        notify=lambda text: None,
        suppress_frame=lambda window, suppressed: None,
        get_windows_hidden=lambda: False,
        set_windows_hidden=lambda value: None,
        bbox_provider=lambda window: (0, 0, 100, 100),
        capture_fn=lambda bbox: _gradient_image(),
        timer_factory=lambda interval_ms, on_tick: _StubTimer(),
    )
    controller.toggle_automation()
    controller.on_translation_result(grid_signature(_gradient_image()), "预存译文")
    controller.on_trigger_clicked(_StubWindow("w"))
    assert host.displayed == ["预存译文\n"]
    assert store.inserts == []


def test_auto_store_degraded_keeps_display():
    """store 降级（insert 返回 None）：仅上屏不入库，回执照常送达。"""
    store = _StubStore(fail=True)
    host = _AutoHost(store)
    host._handle_remote_auto_done(_done_item())
    assert store.inserts == []
    assert host.displayed == ["自动化译文\n"]
    assert host.titles == ["翻译完成"]
    assert host._remote_automation.results == [(_AUTO_SIG, "自动化译文")]


def test_auto_store_error_keeps_display_and_receipt():
    """store 抛错：上屏与在途回执不受影响（异常不得打断收尾链）。"""
    store = _StubStore(explode=True)
    host = _AutoHost(store)
    host._handle_remote_auto_done(_done_item())
    assert host.displayed == ["自动化译文\n"]
    assert host.titles == ["翻译完成"]
    assert host._remote_automation.results == [(_AUTO_SIG, "自动化译文")]


def test_auto_store_missing_keeps_display():
    """store 不可用（None）：仅上屏不入库。"""
    host = _AutoHost(None)
    host._handle_remote_auto_done(_done_item())
    assert host.displayed == ["自动化译文\n"]
    assert host.titles == ["翻译完成"]


def test_auto_worker_success_encodes_original_and_thumbnail(monkeypatch):
    """worker 成功：随结果回传原图与缩略图 JPEG 编码（复用 processing 纯函数）。"""
    monkeypatch.setattr(
        flow_module, "translate_image_verified", lambda *args, **kwargs: "worker 译文"
    )
    host = _AutoHost(_StubStore())
    host._remote_auto_worker(
        1, _gradient_image(size=(200, 100)), _AUTO_SIG, threading.Event(), {}
    )
    item = host.worker_result()
    assert item["text"] == "worker 译文"
    assert item["aborted"] is False
    assert item["original_jpeg"][:2] == b"\xff\xd8"  # JPEG 魔数
    assert item["thumb_jpeg"][:2] == b"\xff\xd8"


def test_auto_worker_aborted_skips_encoding(monkeypatch):
    """worker 中止：携带 aborted 回执、不生成入库图像。"""

    def _forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("已中止不应发起 API")

    monkeypatch.setattr(flow_module, "translate_image_verified", _forbidden)
    host = _AutoHost(_StubStore())
    stop = threading.Event()
    stop.set()
    host._remote_auto_worker(1, _gradient_image(), _AUTO_SIG, stop, {})
    item = host.worker_result()
    assert item["aborted"] is True
    assert item["text"] is None
    assert item["original_jpeg"] is None
    assert item["thumb_jpeg"] is None


def test_auto_worker_failure_reports_error_without_images(monkeypatch):
    """worker 失败：收敛为 error 回执、不生成入库图像。"""

    def _fail(*args: object, **kwargs: object) -> str:
        raise RuntimeError("网络错误")

    monkeypatch.setattr(flow_module, "translate_image_verified", _fail)
    host = _AutoHost(_StubStore())
    host._remote_auto_worker(1, _gradient_image(), _AUTO_SIG, threading.Event(), {})
    item = host.worker_result()
    assert item["aborted"] is False
    assert item["error"] == "网络错误"
    assert item["text"] is None
    assert item["original_jpeg"] is None
    assert item["thumb_jpeg"] is None
