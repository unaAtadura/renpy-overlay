"""遥控器自动化控制器的离线验证：纯函数 + stub 注入，不构造真实 QWidget。

覆盖：网格灰度感知签名（同图同签名、轻微噪点相似、大区域小字幕变化可检出）、
变化格占比与判变阈值、RemoteAutoCache 循环队列（尾插/满淘汰队首/命中移尾）、
控制器状态机（开启前提提示、A 点击首轮/重启轮、周期变化判定、忙碌挂起
与恢复、本控制器在途不置挂起、B 离开/换区/目标销毁停止、菜单关闭保留
单击语义、隐藏遥控器不停自动化、快捷键模式退出复位、宿主拒受保持挂起、
结果回执清在途并回填缓存、装填期提示框压制、注入契约 fail-fast）。

真实截图 / win32api / QTimer 均经构造注入替身；图像用 Pillow 现场生成。
"""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageDraw, ImageFont

from renpy_overlay.screenshot.remote_automation import (
    MSG_NEED_HOTKEY_MODE,
    REMOTE_AUTO_OFF_TEXT,
    REMOTE_AUTO_ON_TEXT,
    REMOTE_HIDE_TEXT,
    REMOTE_SHOW_TEXT,
    SIGNATURE_CHANGE_RATIO,
    SIGNATURE_SIZE,
    RemoteAutoCache,
    RemoteAutomationController,
    grid_signature,
    signature_change_ratio,
    signatures_similar,
)

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
