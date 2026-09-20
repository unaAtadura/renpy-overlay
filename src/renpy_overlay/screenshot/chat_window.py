"""AI 对话窗口：常规窗口向 AI 提问辅助深入学习外语（需求 .raw_plans/future_AI对话.txt）。

布局（需求给定）：上下两部分 —— 上部一行「"保留发送消息"标签 + 复选框 +
下拉列表 + OCR 按钮 + 发送按钮」，下部为无滚动条的文本输入框；色彩方案与
查看截图历史窗口一致（同一套默认控件外观）。

行为约定：

- 复选框：默认勾选，勾选时发送后不清空输入框，取消勾选时发送成功后清空；
  每次打开窗口（宿主调用 :meth:`refresh`）都恢复默认勾选，并按当前双击锁定
  的截图窗口重建下拉列表（色块 + 颜色名区分窗口）；
- OCR 按钮：仅当存在双击锁定的截图窗口时生效，识别选中颜色窗口的区域原文，
  成功后填充到输入框并置光标于末尾；不上屏正文窗，但遵守与发送相同的
  "同时只允许一个 API 调用"互斥原则，冲突时忽略点击且不更新提示；
- 发送按钮（含输入框内 Ctrl+Enter）：添加对话专用系统提示词（令 AI 回复
  无标签无格式的纯文本）发给 AI，回复经宿主回调显示在流式输出窗口的正文窗；
  成功后把用户消息与回复成对写入 chat.db；不保存上下文，每次只发本次消息；
- 标题提示（经宿主写入流式窗标题窗）：对话发送中... / 发送失败 / 发送成功 /
  OCR中，速度会稍慢... / OCR成功 / OCR失败；
- 线程模型与流式悬浮窗一致：Qt 只在主线程操作，OCR / 发送在守护线程执行，
  结果经内部队列由 QTimer（80ms）统一消费。

依赖全部经构造回调注入（store / 控制模块 / 宿主互斥与显示回调），本窗口
不依赖宿主类型；窗口关闭 = 隐藏（Qt 默认），单例复用，宿主退出时统一销毁。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import (
    QColor,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import translator
from ..screenshot.processing import (
    constrain_aspect_ratio,
    encode_jpeg_base64,
    scale_to_percent,
)
from ..screenshot.vision import recognize_image
from ..screenshot.window import FRAME_COLORS
from .chat_store import ChatStore

if TYPE_CHECKING:  # 仅类型标注：模块级导入会与 config 形成包初始化期循环
    from .. import config

logger = logging.getLogger("renpy_overlay.screenshot.chat_window")

#: 消息队列消费周期（与流式悬浮窗 DRAIN_INTERVAL_MS 一致）
DRAIN_INTERVAL_MS = 80
#: 下拉列表色块尺寸（像素）
SWATCH_SIZE = 14

# ---- 标题提示文案（需求给定，经宿主写入流式窗标题窗） ------------------------
TITLE_SENDING = "对话发送中..."
TITLE_SEND_FAILED = "发送失败"
TITLE_SEND_OK = "发送成功"
TITLE_OCR_RUNNING = "OCR中，速度会稍慢..."
TITLE_OCR_OK = "OCR成功"
TITLE_OCR_FAILED = "OCR失败"

#: 输入框为空时的背景占位文案（需求给定）
INPUT_PLACEHOLDER = "按 Ctrl+Enter 即可发送。"

#: 对话专用系统提示词（需求）：令 AI 回复无标签、无格式的纯文本，适配流式
#: 正文窗的纯文字显示方式
CHAT_SYSTEM_PROMPT = (
    "你是帮助用户深入学习外语的学习助手。请回答用户的问题。"
    "只输出无标签、无格式的纯文本正文，"
    "不要使用 Markdown 标记、列表符号、标题符号和引号。"
)

#: 八色边框的中文名（与 FRAME_COLORS 创建序一一对应：红橙黄绿青蓝紫黑）
FRAME_COLOR_NAMES = ("红", "橙", "黄", "绿", "青", "蓝", "紫", "黑")


def color_label(color: QColor) -> str:
    """边框颜色 → 下拉列表标签（纯函数，便于离线单测）。

    八色按创建序返回中文名（红橙黄绿青蓝紫黑）；其余颜色（理论上不会出现，
    防御性兜底）回退十六进制色值。
    """
    for frame_color, name in zip(FRAME_COLORS, FRAME_COLOR_NAMES, strict=True):
        if color == frame_color:
            return name
    return color.name()


def color_swatch_pixmap(color: QColor, size: int = SWATCH_SIZE) -> QPixmap:
    """下拉列表项的色块图标：纯色方块（需求：用色块区分截图窗口）。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(color)
    return pixmap


class AIChatWindow(QWidget):
    """AI 对话窗口（单例复用：宿主持有，打开时 refresh 重置交互状态）。"""

    def __init__(
        self,
        *,
        store: ChatStore | None,
        controller,
        app_config: config.AppConfig,
        notify,
        display_reply,
        api_available,
        api_acquire,
        api_release,
        capture_locked,
    ) -> None:
        super().__init__()
        self._store = store
        self._controller = controller  # 控制模块（QuickMenu）：locked_windows 唯一来源
        self._config = app_config
        self._notify = notify  # 宿主标题窗文案出口
        self._display_reply = display_reply  # 宿主流式正文窗显示出口
        self._api_available = api_available  # 宿主互斥：当前是否有 API 请求在途
        self._api_acquire = api_acquire  # 宿主互斥：占用（占用后方可在后台调 API）
        self._api_release = api_release  # 宿主互斥：释放
        self._capture_locked = capture_locked  # 宿主抓屏：隐藏窗口取锁定窗口干净画面
        self._seq = 0  # 请求世代号：过期结果不入界面（互斥下理论不重叠，兜底）

        self.setWindowTitle("AI 对话")
        self.resize(
            max(1, self._config.chat_window_width),
            max(1, self._config.chat_window_height),
        )

        # ---- 上部：标签 + 复选框 + 下拉列表 + OCR + 发送 --------------------
        self._keep_label = QLabel("保留发送消息")
        self._keep_checkbox = QCheckBox()
        self._keep_checkbox.setChecked(True)  # 需求：默认勾选
        self._keep_checkbox.setToolTip("勾选时发送消息后不清空输入框")
        self._combo = QComboBox()
        self._combo.setToolTip("双击锁定的截图窗口（按边框颜色区分）")
        self._ocr_button = QPushButton("OCR")
        self._ocr_button.setToolTip("识别选中颜色窗口的区域原文并填充到输入框")
        self._ocr_button.clicked.connect(self._on_ocr)
        self._send_button = QPushButton("发送")
        self._send_button.setToolTip("把输入框内容发送给 AI，回复显示在流式正文窗")
        self._send_button.clicked.connect(self._on_send)
        top = QHBoxLayout()
        top.addWidget(self._keep_label)
        top.addWidget(self._keep_checkbox)
        top.addWidget(self._combo, stretch=1)
        top.addWidget(self._ocr_button)
        top.addWidget(self._send_button)

        # ---- 下部：无滚动条文本输入框 ---------------------------------------
        self._input = QPlainTextEdit()
        self._input.setPlaceholderText(INPUT_PLACEHOLDER)
        self._input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._input.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._input, stretch=1)

        # 输入框内 Ctrl+Enter 发送（WidgetShortcut：仅光标在输入框内时生效）
        self._send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self._input)
        self._send_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._send_shortcut.activated.connect(self._on_send)

        # ---- 线程模型：结果只经队列回主线程（与流式悬浮窗同模式） ------------
        self._queue: queue.Queue = queue.Queue()
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_INTERVAL_MS)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()

    # ---- 对外接口 -----------------------------------------------------------

    def refresh(self) -> None:
        """每次打开窗口时由宿主调用：复选框恢复默认勾选，重建锁定窗口下拉列表。"""
        self._keep_checkbox.setChecked(True)
        self._rebuild_combo()

    # ---- 下拉列表 ------------------------------------------------------------

    def _rebuild_combo(self) -> None:
        """按当前双击锁定的截图窗口重建列表：色块图标 + 颜色名，数据存窗口本身。"""
        self._combo.clear()
        windows = self._controller.locked_windows()
        for window in windows:
            color = window.border_color
            self._combo.addItem(QIcon(color_swatch_pixmap(color)), color_label(color), window)
        if not windows:
            logger.info("当前没有双击锁定的截图窗口，OCR 入口不生效")

    # ---- OCR（识别选中窗口区域的原文） ----------------------------------------

    def _on_ocr(self) -> None:
        """OCR 点击（主线程）：无锁定窗口不生效；API 冲突忽略且不更新提示。"""
        window = self._combo.currentData()
        if window is None:
            logger.info("没有可选的双击锁定截图窗口，忽略本次 OCR 点击")
            return
        if not self._api_available():
            logger.info("已有请求在途，忽略本次 OCR 点击（不更新提示）")
            return
        self._api_acquire()
        self._seq += 1
        seq = self._seq
        self._notify(TITLE_OCR_RUNNING)
        logger.info("发起 OCR（seq=%d），识别选中截图窗口区域", seq)
        # 抓屏走宿主时序（隐藏窗口 → 等合成 → 抓屏 → 恢复），结果回主线程起线程
        self._capture_locked(window, lambda image: self._start_ocr_worker(seq, image))

    def _start_ocr_worker(self, seq: int, image) -> None:
        """抓屏终点（主线程）：抓屏失败按 OCR 失败收尾，成功则起后台识别线程。"""
        if image is None:
            logger.warning("截图窗口区域抓取失败，OCR 中止（seq=%d）", seq)
            self._api_release()
            self._notify(TITLE_OCR_FAILED)
            return
        thread = threading.Thread(
            target=self._ocr_worker,
            args=(seq, image, self._vision_api_options()),
            name=f"chat-ocr-{seq}",
            daemon=True,
        )
        thread.start()

    def _ocr_worker(self, seq: int, image, api_options: dict) -> None:
        """后台线程：压缩副本 → vision API 识别原文 → 结果只经队列回主线程。"""
        try:
            ratio_image = constrain_aspect_ratio(image)
            api_image = scale_to_percent(ratio_image, self._config.screenshot_compress_percent)
            jpeg_base64 = encode_jpeg_base64(api_image)
            text = recognize_image(jpeg_base64, **api_options)
            self._queue.put(("ocr_done", {"seq": seq, "text": text}))
        except Exception as exc:  # 网络/协议/图像错误统一收敛为 OCR 失败
            logger.debug("OCR 识别失败（seq=%d）：%s", seq, exc)
            self._queue.put(("ocr_fail", {"seq": seq, "error": str(exc)}))

    def _handle_ocr_done(self, payload: dict) -> None:
        """主线程：OCR 成功收尾——释放互斥、提示成功、原文填充输入框并置光标末尾。"""
        self._api_release()
        self._notify(TITLE_OCR_OK)
        text = str(payload.get("text") or "")
        self._input.setPlainText(text)
        cursor = self._input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._input.setTextCursor(cursor)
        logger.info("OCR 成功（seq=%s，原文 %d 字），已填充到输入框", payload.get("seq"), len(text))

    def _handle_ocr_fail(self, payload: dict) -> None:
        """主线程：OCR 失败收尾——释放互斥、提示失败。"""
        self._api_release()
        self._notify(TITLE_OCR_FAILED)
        logger.warning("OCR 失败（seq=%s）：%s", payload.get("seq"), payload.get("error"))

    # ---- 发送 -----------------------------------------------------------------

    def _on_send(self) -> None:
        """发送点击（主线程）：空文本忽略；API 冲突忽略且不更新提示。"""
        text = self._input.toPlainText().strip()
        if not text:
            logger.info("输入框为空，忽略本次发送")
            return
        if not self._api_available():
            logger.info("已有请求在途，忽略本次发送（不更新提示）")
            return
        self._api_acquire()
        self._seq += 1
        seq = self._seq
        self._notify(TITLE_SENDING)
        logger.info("发起 AI 对话（seq=%d，消息 %d 字）", seq, len(text))
        thread = threading.Thread(
            target=self._send_worker,
            args=(seq, text, self._text_api_options()),
            name=f"chat-send-{seq}",
            daemon=True,
        )
        thread.start()

    def _send_worker(self, seq: int, user_msg: str, api_options: dict) -> None:
        """后台线程：流式请求（对话专用系统提示词），完整回复只经队列回主线程。"""
        chunks: list[str] = []
        try:
            for piece in translator.translate_text_stream(user_msg, **api_options):
                chunks.append(piece)
            reply = "".join(chunks).strip()
            if not reply:
                raise translator.TranslationError("模型返回了空回复")
            self._queue.put(("chat_done", {"seq": seq, "user_msg": user_msg, "reply": reply}))
        except Exception as exc:  # 网络/协议错误统一收敛为发送失败
            logger.debug("对话发送失败（seq=%d）：%s", seq, exc)
            self._queue.put(("chat_fail", {"seq": seq, "error": str(exc)}))

    def _handle_chat_done(self, payload: dict) -> None:
        """主线程：发送成功收尾——释放互斥、提示成功、正文窗显示回复、成对落库。"""
        self._api_release()
        self._notify(TITLE_SEND_OK)
        reply = str(payload.get("reply") or "").strip()
        self._display_reply(reply)
        logger.info("对话发送成功（seq=%s，回复 %d 字），已在流式正文窗显示", payload.get("seq"), len(reply))
        if self._store is not None:
            record_id = self._store.insert(
                str(payload.get("user_msg") or ""), reply
            )
            if record_id is None:
                logger.warning("对话记录入库失败，仅保留正文显示")
        else:
            logger.info("对话数据库不可用，本条记录不入库")
        if not self._keep_checkbox.isChecked():
            self._input.clear()  # 需求：取消勾选时发送（成功）后清空文本框

    def _handle_chat_fail(self, payload: dict) -> None:
        """主线程：发送失败收尾——释放互斥、提示失败（输入框内容保留）。"""
        self._api_release()
        self._notify(TITLE_SEND_FAILED)
        logger.warning("对话发送失败（seq=%s）：%s", payload.get("seq"), payload.get("error"))

    # ---- 队列消费 --------------------------------------------------------------

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if payload.get("seq") != self._seq:
                    logger.info("对话窗口结果已过期（seq=%s，当前=%s），丢弃",
                                payload.get("seq"), self._seq)
                    continue
                if kind == "ocr_done":
                    self._handle_ocr_done(payload)
                elif kind == "ocr_fail":
                    self._handle_ocr_fail(payload)
                elif kind == "chat_done":
                    self._handle_chat_done(payload)
                elif kind == "chat_fail":
                    self._handle_chat_fail(payload)
        except queue.Empty:
            pass
        except Exception:  # pragma: no cover - UI 异常不应终止循环
            logger.exception("处理对话窗口消息时出错")

    # ---- API 请求参数 -----------------------------------------------------------

    def _vision_api_options(self) -> dict:
        """OCR 请求参数：与截图翻译同源（vision 模型退避 + 备选链路）。"""
        from .. import config  # 局部导入：config 反向依赖本包（hotkeys），避免包初始化期循环

        return {
            "base_url": self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": config.resolve_screenshot_model(
                self._config.screenshot_model, self._config.model
            ),
            "api_key": self._config.api_key,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
            "base_url_stanby": self._config.api_base_url_stanby,
            "api_key_stanby": self._config.api_key_stanby,
            "model_stanby": config.resolve_screenshot_model(
                self._config.screenshot_model_stanby, self._config.model_stanby
            ),
        }

    def _text_api_options(self) -> dict:
        """对话请求参数：与流式翻译同源，仅系统提示词换为对话专用（纯文本回复）。"""
        return {
            "base_url": self._config.api_base_url,
            "timeout": self._config.api_timeout,
            "model": self._config.model,
            "system_prompt": CHAT_SYSTEM_PROMPT,
            "api_key": self._config.api_key,
            "enable_thinking": self._config.enable_thinking,
            "reasoning_effort": self._config.reasoning_effort,
            "base_url_stanby": self._config.api_base_url_stanby,
            "api_key_stanby": self._config.api_key_stanby,
            "model_stanby": self._config.model_stanby,
        }
