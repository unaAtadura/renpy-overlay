"""遥控器自动化：周期截图 + 感知签名变化检测 + 自动翻译的控制器与缓存。

需求（future_截图翻译功能补充_遥控截图自动化）：基于遥控截图实现的自动化
截屏翻译——

- 右键快捷菜单「遥控器自动化: 关/开」开关：开启前提为 A/B 已生成（快捷键
  模式开启中），否则经宿主提示并保持关闭；退出快捷键模式自动复位为关
  （用户决议 A3：菜单项始终可点 + 点击提示）；
- A（触发器）左键单击启动第一轮截图翻译并进入自动状态；自动状态中再次
  单击 A = 重启一轮（重置基准并立即提交，周期继续，用户决议 A5）；
- B（瞄准器）离开装填区域或切换装填目标 → 停止周期、重置变化状态，等待
  再次点击 A 开始第一轮；
- 周期截图按 ``remote_screenshot_interval``（默认 0.5s）调度，网格灰度感知
  签名（32x16 均值网格，aHash 家族）比较近两次画面（容忍屏幕噪点与 OLED
  防烧屏像素偏移，对「大画面小字幕」的内容变化敏感——64bit dHash 在该场
  景实测距离仅 0~1 而漏检，见 :func:`grid_signature`），变化才进入翻译流程；
- API 忙碌（任意在途翻译/识曲/对话）期间保持截图变化状态、继续周期截图
  但不再比较；线路通畅后把新的截图先查缓存、未命中才提交（用户决议 A7），
  同时只允许一个在途 API 请求（经宿主互斥槽）；
- 自动化全程不隐藏任何窗口（防止反复隐藏/显示的爆闪；A/B 半透明像素入镜
  为已确认取舍，用户决议 A4）；「隐藏遥控器」不停止自动化（决议 A6）；
- 译文进内存循环队列缓存（``remote_cache_size_count``，默认 50，仅存感知
  签名与译文，命中移尾），命中直接上屏；自动化截图翻译不写 screenshot.db。

与宿主的边界（同 RemoteScreenshotController 的注入约定）：本控制器不依赖
宿主类型，忙碌查询 / 翻译提交 / 上屏 / 提示 / 提示框压制 / A-B 显隐全部经
构造回调注入，离线测试用 stub 替身（tests/test_remote_automation.py）。
仅 Qt 主线程使用（QTimer 调度与全部回调同线程）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PIL import Image

logger = logging.getLogger("renpy_overlay.screenshot.remote_automation")

# ---- 菜单文案（需求给定措辞，QuickMenu 按控制器状态选用） -----------------------

REMOTE_AUTO_OFF_TEXT = "遥控器自动化: 关"
REMOTE_AUTO_ON_TEXT = "遥控器自动化: 开"
REMOTE_HIDE_TEXT = "隐藏遥控器"
REMOTE_SHOW_TEXT = "显示遥控器"

#: 快捷键模式未开启时点击菜单项的提示（用户决议 A3：始终可点 + 点击提示）
MSG_NEED_HOTKEY_MODE = "请先开启快捷键模式"

# ---- 感知签名参数（方案 B：网格灰度签名，替代 64bit dHash） ----------------------

#: 签名网格：灰度 32x16 = 512 格逐格均值（aHash 家族感知哈希）
SIGNATURE_SIZE = (32, 16)
#: 单格灰度差超过该值（0-255）才计入「变化格」：均值化后屏幕噪点与 OLED
#: 防烧屏像素偏移每格变化仅 1~2 级（实测 1px 平移最大 |Δ|=8，未超阈）
SIGNATURE_CELL_THRESHOLD = 8
#: 变化格占比达到该比例判为画面变化：真实漏检样本（1878x288 大横条区域仅
#: 角部字幕变化，dHash 距离仅 1）实测超阈占比 9.4%，同图/平移为 0%，
#: 取 1.5% 留约 5 倍裕量
SIGNATURE_CHANGE_RATIO = 0.015

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)，物理像素
Signature = tuple[int, ...]  # 感知签名：逐格灰度均值（长度 = SIGNATURE_SIZE 乘积）


def grid_signature(image: Image.Image) -> Signature:
    """感知签名：灰度 32x16 均值网格（aHash 家族，纯函数，离线可测）。

    逐格灰度均值对「大画面小字幕」的内容变化敏感（64bit dHash 的梯度位在
    该场景信号趋零，真实样本实测距离仅 0~1），而均值化又对屏幕噪点与像素
    偏移免疫；判变阈值见 :data:`SIGNATURE_CELL_THRESHOLD` 与
    :data:`SIGNATURE_CHANGE_RATIO`。
    """
    gray = image.convert("L").resize(SIGNATURE_SIZE, Image.LANCZOS)
    return tuple(gray.getdata())


def signature_change_ratio(a: Signature, b: Signature) -> float:
    """两签名的「变化格占比」＝超阈格数 / 总格数（纯函数）。

    长度不一致（理论上仅常数变更后的跨版本残留）或空签名按 1.0 处理，
    保守判为变化。
    """
    if not a or len(a) != len(b):
        return 1.0
    over = sum(
        1 for x, y in zip(a, b, strict=False) if abs(x - y) > SIGNATURE_CELL_THRESHOLD
    )
    return over / len(a)


def signatures_similar(a: Signature, b: Signature) -> bool:
    """感知签名相似判定：变化格占比未达阈值即视为同一画面（纯函数）。"""
    return signature_change_ratio(a, b) < SIGNATURE_CHANGE_RATIO


class RemoteAutoCache:
    """自动化翻译的内存循环队列缓存：仅存（感知签名, 译文），命中移尾。

    需求：新内容顶掉最早内容；翻译成功时添加到队列末尾；缓存命中时把
    命中条目移动到队列末尾。容量下限 1（配置层已做非法值回退，此处防御
    再钳制）。仅进程内存，不持久化、不含图像数据（每条签名 512 字节）。
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._entries: list[tuple[Signature, str]] = []  # 队首最早 → 队尾最新

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._entries)

    def put(self, signature: Signature, text: str) -> None:
        """翻译成功写入队尾；超出容量则淘汰队首（新顶旧）。"""
        self._entries.append((tuple(signature), str(text)))
        while len(self._entries) > self._capacity:
            self._entries.pop(0)
            logger.debug("遥控器自动化缓存已满，淘汰最早条目（容量 %d）", self._capacity)

    def lookup(self, signature: Signature) -> str | None:
        """按感知签名相似命中：返回译文并把命中条目移至队尾；未命中返回 None。"""
        for index, (known, text) in enumerate(self._entries):
            if signatures_similar(known, signature):
                self._entries.append(self._entries.pop(index))
                logger.debug("遥控器自动化缓存命中（命中条目已移至队尾）")
                return text
        return None


# ---- 默认实现（惰性导入，便于离线测试注入替身） ----------------------------------


def _default_timer_factory(interval_ms: int, on_tick: Callable[[], None]) -> object:
    """真实计时器工厂：QTimer 周期触发（主线程事件循环驱动）。"""
    from PyQt6.QtCore import QTimer  # noqa: PLC0415 - 惰性导入

    timer = QTimer()
    timer.setInterval(int(interval_ms))
    timer.timeout.connect(on_tick)
    return timer


def _default_bbox_provider(window) -> Rect:
    """装填目标 → 截图区域 ltrb（物理像素，与手动截图翻译入口同源）。"""
    from .. import win32api  # noqa: PLC0415 - 局部导入：仅 Windows 存在

    return win32api.window_rect(window.hwnd)


def _default_capture(bbox: Rect) -> Image.Image:
    """真实截图函数：屏幕物理像素区域 → PIL Image（同 capture.capture_region）。"""
    from .capture import capture_region  # noqa: PLC0415 - 惰性导入

    return capture_region(bbox)


class RemoteAutomationController:
    """遥控器自动化控制器：开关状态机 + 周期调度 + 缓存命中分发。

    状态机（用户决议与需求逐条对应）::

        closed（开关关）
          ├─ 菜单「开」（A/B 已生成）→ ready（能力激活，不启动周期）
          └─ 菜单「开」（A/B 未生成）→ 提示并保持 closed
        ready（开关开、等待 A 点击）
          └─ A 左键单击 → 第一轮截图翻译（建立基准）→ auto
        auto（周期运行）
          ├─ B 离开/切换装填区域 → 停止周期、重置变化状态 → ready
          ├─ A 左键单击 → 重启一轮（重置基准并立即提交，周期继续）
          ├─ 装填目标销毁 → 安全停止 → ready
          └─ 菜单「关」→ closed（A/B 与单击遥控截图保留）
        快捷键模式退出（任意状态）→ 复位为 closed（决议 A3）

    构造注入（同 QuickMenu/HotkeyMode/RemoteScreenshotController 的宿主
    能力注入约定，不依赖宿主类型）：

    - ``has_windows``：A/B 是否已生成（自动化开启前提）；
    - ``is_busy``：宿主互斥查询（存在任意在途翻译/识曲/对话）；
    - ``display``：译文上屏回调（缓存命中直接显示，不发 API）；
    - ``submit``：翻译提交回调 ``(window, image, signature) -> bool``，返回
      True 表示宿主已受理（进入在途，结果经 :meth:`on_translation_result`
      回执），False 表示宿主忙碌被拒（保持变化状态等待恢复）；
    - ``notify``：标题窗提示回调（如 StreamWindow._update_title）；
    - ``suppress_frame``：提示框单帧压制回调 ``(window, suppressed)``；
    - ``get_windows_hidden`` / ``set_windows_hidden``：A/B 用户级显隐
      （「隐藏遥控器/显示遥控器」，RemoteScreenshotController 提供）；
    - ``capture_fn`` / ``bbox_provider`` / ``timer_factory``：截图函数、
      区域换算与计时器工厂（离线测试注入 stub，缺省为真实实现）。
    """

    def __init__(
        self,
        interval: float,
        cache_size: int,
        *,
        has_windows: Callable[[], bool],
        is_busy: Callable[[], bool],
        display: Callable[[str], None],
        submit: Callable[[object, Image.Image, Signature], bool],
        notify: Callable[[str], None],
        suppress_frame: Callable[[object, bool], None],
        get_windows_hidden: Callable[[], bool],
        set_windows_hidden: Callable[[bool], None],
        capture_fn: Callable[[Rect], Image.Image] | None = None,
        bbox_provider: Callable[[object], Rect] | None = None,
        timer_factory: Callable[[int, Callable[[], None]], object] | None = None,
    ) -> None:
        # 注入契约（构造期 fail-fast，同 HotkeyMode 构造校验先例）：宿主误把
        # property 取值当回调传入（如 windows_created 不加括号/不包 lambda）时，
        # 在启动即报错而非等到菜单点击时 TypeError（曾致右键菜单 CRITICAL）
        for _name, _callback in (
            ("has_windows", has_windows),
            ("is_busy", is_busy),
            ("display", display),
            ("submit", submit),
            ("notify", notify),
            ("suppress_frame", suppress_frame),
            ("get_windows_hidden", get_windows_hidden),
            ("set_windows_hidden", set_windows_hidden),
        ):
            if not callable(_callback):
                raise TypeError(
                    f"RemoteAutomationController 注入参数 {_name} 必须可调用"
                    f"（收到 {type(_callback).__name__}；property 取值需包 lambda）"
                )
        # 周期下限 100ms 双保险（配置层已按 0.1s 下限回退，此处防御硬钳制）
        self._interval_ms = max(100, int(float(interval) * 1000))
        self._cache = RemoteAutoCache(cache_size)
        self._has_windows = has_windows
        self._is_busy = is_busy
        self._display = display
        self._submit = submit
        self._notify = notify
        self._suppress_frame = suppress_frame
        self._get_windows_hidden = get_windows_hidden
        self._set_windows_hidden = set_windows_hidden
        self._capture_fn = capture_fn or _default_capture
        self._bbox_provider = bbox_provider or _default_bbox_provider
        self._timer_factory = timer_factory or _default_timer_factory
        # ---- 状态机 ----
        self._enabled = False  # 菜单开关「遥控器自动化: 关/开」
        self._running = False  # 自动状态：周期运行中（A 单击后进入）
        self._armed: object | None = None  # 当前装填目标（遥控截图控制器转发）
        self._baseline: Signature | None = None  # 基准感知签名（首轮/命中/提交时更新）
        self._pending_change = False  # 截图变化状态（忙碌期间保持，恢复后提交）
        self._in_flight = False  # 本控制器提交的翻译在途中（宿主回执清除）
        self._timer: object | None = None

    # ---- 状态检视（QuickMenu 文案用） -------------------------------------------

    @property
    def enabled(self) -> bool:
        """菜单开关当前态（「遥控器自动化: 关/开」文案依据）。"""
        return self._enabled

    @property
    def remote_hidden(self) -> bool:
        """A/B 当前是否被用户隐藏（「隐藏遥控器/显示遥控器」文案依据）。"""
        return bool(self._get_windows_hidden())

    # ---- 菜单入口（QuickMenu 点击分发） -------------------------------------------

    def toggle_automation(self) -> None:
        """菜单「遥控器自动化」点击：开启/关闭自动化能力。"""
        self.set_enabled(not self._enabled)

    def set_enabled(self, enable: bool) -> None:
        """切换自动化开关（状态机 closed ↔ ready 的入口）。

        开启前提：A/B 已生成（快捷键模式开启中），否则经宿主提示并保持
        关闭（用户决议 A3）。开启只激活能力（装填期提示框压制等），不启动
        周期——等待 A 单击启动第一轮。关闭仅停止周期截图与变化判定等自动
        化能力，A/B 与遥控截图原有单击功能保留（需求）。
        """
        if enable == self._enabled:
            return
        if enable and not self._has_windows():
            self._notify(MSG_NEED_HOTKEY_MODE)
            logger.info("遥控器自动化开启失败：快捷键模式未开启（A/B 未生成）")
            return
        self._enabled = enable
        if enable:
            if self._armed is not None:
                # 开启时 B 已在装填中：同步压制该区域提示框（与装填事件同语义；
                # 回调第二参为 suppressed：True=压制隐藏 / False=解除显示）
                self._suppress_frame(self._armed, True)
            logger.info("遥控器自动化已开启（等待左键点击 A 窗口启动第一轮）")
            return
        self._stop_cycle()
        if self._armed is not None:  # 解除装填期的提示框压制（恢复显示）
            self._suppress_frame(self._armed, False)
        logger.info("遥控器自动化已关闭（保留遥控截图原有单击功能）")

    def toggle_remote_hidden(self) -> None:
        """菜单「隐藏遥控器/显示遥控器」点击：切换 A/B 用户级显隐。

        自动化按文档字面继续运行（用户决议 A6）：隐藏导致的截图区域像素
        变化复用既有变化检测流程，不需额外处理。
        """
        hidden = not self.remote_hidden
        self._set_windows_hidden(hidden)
        logger.info("遥控器 A/B 已%s（自动化状态不受影响）", "隐藏" if hidden else "恢复显示")

    # ---- 装填变化（RemoteScreenshotController 转发） -------------------------------

    def on_armed_changed(self, window) -> None:
        """B 装填状态变化：离开/切换目标时停止自动；装填期压制该区域提示框。

        需求：B 离开截图区域或捕获新的截图区域时停止自动截图、重置截图
        变化状态，等候再次左键点击 A 开始第一轮。提示框隐藏/恢复仅在自动
        化开启期间生效（需求「开启时」限定）。
        """
        if window is self._armed:
            return
        previous = self._armed
        self._armed = window
        if self._running:
            logger.info("遥控器自动化：B 离开/切换截图区域，停止自动截图")
            self._stop_cycle()
        if self._enabled:
            if previous is not None:
                self._suppress_frame(previous, False)  # 离开：解除压制，恢复该区域提示框
            if window is not None:
                self._suppress_frame(window, True)  # 进入：压制，隐藏该区域提示框

    # ---- A 单击（RemoteScreenshotController 转发，自动化开启时接管） ----------------

    def on_trigger_clicked(self, window) -> None:
        """A 左键单击（自动化开启且已装填时由遥控截图控制器转发）。

        ready 态 → 启动第一轮并进入 auto；auto 态 → 重启一轮（重置基准并
        立即提交，周期继续，用户决议 A5）。
        """
        if not self._enabled or window is None:
            return
        self._armed = window
        self._start_round()

    def _start_round(self) -> None:
        """首轮/重启轮共用入口：立即截图一轮（先查缓存，未命中提交）并进入自动。"""
        capture = self._capture_current()
        if capture is None:
            return  # 抓屏失败/目标失效：不进入自动状态，等候再次点击
        image, current = capture
        self._baseline = current
        self._pending_change = False
        if self._running:
            logger.info("遥控器自动化：重启一轮截图翻译（周期继续）")
        else:
            self._running = True
            self._ensure_timer().start()
            logger.info(
                "遥控器自动化：第一轮截图翻译已启动（周期 %d ms）", self._interval_ms
            )
        self._process_change(image, current)

    # ---- 周期 tick 与变化判定 -------------------------------------------------------

    def _ensure_timer(self) -> object:
        """惰性创建周期计时器（首次进入自动状态时；便于离线测试免建 QTimer）。"""
        if self._timer is None:
            self._timer = self._timer_factory(self._interval_ms, self._on_tick)
        return self._timer

    def _on_tick(self) -> None:
        """周期 tick（主线程）：忙碌保持变化状态；恢复后先查缓存再提交。"""
        if not self._enabled or not self._running:
            self._stop_cycle()
            return
        if self._armed is None:
            logger.info("遥控器自动化：装填目标已不存在，停止自动截图")
            self._stop_cycle()
            return
        capture = self._capture_current()
        if capture is None:
            return  # 本轮抓屏失败：保持周期，下一 tick 重试
        image, current = capture
        if self._is_busy():
            # 需求：API 阻塞期间保持截图变化状态，继续周期性截图，不再比较。
            # 本控制器自己提交的在途请求不置挂起（结果回执后正常与基准比较，
            # 避免每次翻译完成后多一次冗余的「恢复提交」）
            if not self._in_flight:
                self._pending_change = True
            return
        if self._pending_change:
            # 线路通畅后的第一 tick：把新的截图走变化路径（先查缓存，决议 A7）
            self._process_change(image, current)
            return
        if self._baseline is None:
            self._process_change(image, current)
            return
        ratio = signature_change_ratio(current, self._baseline)
        if ratio >= SIGNATURE_CHANGE_RATIO:
            self._process_change(image, current)
            return
        # 无变化（基准不随无变化的 tick 前移，缓慢漂移累计到阈值才翻译）；
        # 留 debug 足迹：曾因该分支静默，漏检问题只能靠样本数据库回溯定位
        logger.debug(
            "遥控器自动化：画面无变化（超阈格占比 %.2f%%，判变阈值 %.2f%%）",
            ratio * 100,
            SIGNATURE_CHANGE_RATIO * 100,
        )

    def _process_change(self, image: Image.Image, current: Signature) -> None:
        """「画面已变化」的统一路径（首轮/重启轮/常规变化/恢复分支共用）：

        先查缓存（决议 A7：命中直接上屏不发 API）；未命中且宿主空闲则提交
        API（成功进入请求流程即重置变化状态，需求）；宿主恰好在途则保持
        变化状态等待恢复分支重试（同时只允许一个在途）。
        """
        self._baseline = current
        self._pending_change = False
        hit = self._cache.lookup(current)
        if hit is not None:
            logger.info("遥控器自动化：缓存命中，直接上屏（不发 API）")
            self._display(hit)
            return
        if self._is_busy():
            self._pending_change = True
            logger.info("遥控器自动化：宿主在途，保持截图变化状态等待恢复")
            return
        self._submit_capture(image, current)

    def _submit_capture(self, image: Image.Image, current: Signature) -> None:
        """提交翻译：宿主受理后标记在途（回执清除）；被拒则挂起变化状态。"""
        if self._submit(self._armed, image, current):
            self._in_flight = True
            logger.info("遥控器自动化：截图已提交翻译（签名 %d 格）", len(current))
        else:
            self._pending_change = True
            logger.info("遥控器自动化：宿主忙碌拒绝提交，保持变化状态等待恢复")

    def _capture_current(self) -> tuple[Image.Image, Signature] | None:
        """抓取当前装填目标区域并计算感知签名；失败/目标失效返回 None。

        装填目标区域不可用（窗口销毁竞态）时安全停止周期（需求：目标失效
        → 停止自动）；普通抓屏失败（锁屏等）保持周期，下一 tick 重试。
        """
        try:
            bbox = self._bbox_provider(self._armed)
        except Exception:  # pragma: no cover - 窗口销毁竞态（真实路径）
            logger.info("遥控器自动化：装填目标区域不可用（窗口可能已销毁），停止自动截图")
            self._stop_cycle()
            return None
        try:
            image = self._capture_fn(bbox)
        except Exception as exc:
            logger.warning("遥控器自动化周期截图失败：%s", exc)
            return None
        return image, grid_signature(image)

    def _stop_cycle(self) -> None:
        """停止周期截图（回到等待点击态）：清变化状态与基准签名。"""
        self._running = False
        self._pending_change = False
        self._baseline = None
        if self._timer is not None:
            self._timer.stop()

    # ---- 宿主回执与生命周期 ---------------------------------------------------------

    def on_translation_result(
        self, signature: Signature | None, text: str | None
    ) -> None:
        """宿主回传自动化翻译结果（主线程）：成功回填缓存，并清除在途标记。

        中止/失败传 ``text=None``：不污染缓存；在途标记无论成败都必须清除，
        否则忙碌分支会永久跳过变化判定（自动化停在「在途」态）。
        """
        self._in_flight = False
        if text and signature is not None:
            self._cache.put(signature, text)

    def on_hotkey_mode_changed(self, enabled: bool) -> None:
        """快捷键模式开/关（经 RemoteScreenshotController 转发）。

        退出模式即 A/B 销毁：自动化复位为「关」（用户决议 A3），周期与
        全部状态清空；模式开启无需处理（装填状态经 :meth:`on_armed_changed`
        送达）。
        """
        if enabled:
            return
        if self._enabled or self._running:
            logger.info("快捷键模式退出：遥控器自动化复位为关")
        self._armed = None
        self._enabled = False
        self._stop_cycle()

    def shutdown(self) -> None:
        """宿主退出时调用：停周期并复位（幂等）。"""
        self.on_hotkey_mode_changed(False)
