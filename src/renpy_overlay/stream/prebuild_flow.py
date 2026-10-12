"""预构建翻译缓存与注入入口的门面 Mixin：扫描弹窗、后台任务、模式切换。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``skip_injection`` / ``_injected`` / ``_title_window`` /
``_auto_translate_on`` / ``_prebuild_dialog`` / ``_prebuild_seq`` /
``_prebuild_stop`` / ``_game_dir`` / ``_config`` / ``_asr_rt`` / ``_queue``，
以及 ``_update_title``。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .. import translation_store
from ..bound_window import BoundAction
from ..pretranslate import PrebuildTask, scan_rpy_files
from ..pretranslate_dialog import PretranslateDialog
from .icons import (
    BOUND_LOCATE_ICON,
    BOUND_PREBUILD_ICON,
    translation_mode_icon,
)

logger = logging.getLogger("renpy_overlay.stream_window")


class PrebuildFlowMixin:
    """注入确认入口条、预构建扫描/后台任务、翻译模式运行时切换。"""

    # ------------------------------------------------------------ 预构建翻译缓存

    def mark_injected(self) -> None:
        """注入成功（Hook 层确认）：显示绑定窗口（跨线程安全，经队列）。"""
        self._queue.put(("injected", None))

    def _handle_injected(self) -> None:
        """主线程：注入确认后把入口条并入标题窗（跳过注入模式不注入）。"""
        if self._closed or self.skip_injection or self._injected:
            return
        self._injected = True
        self._title_window.set_title_entries(
            [
                BoundAction(
                    id="prebuild",
                    label="预构建翻译缓存",
                    on_click=self.request_prebuild,
                    icon=BOUND_PREBUILD_ICON,
                ),
                BoundAction(
                    id="locate-translation",
                    label="定位翻译位置",
                    on_click=self._locate_last_translation,
                    icon=BOUND_LOCATE_ICON,
                ),
            ]
        )
        # 「切换翻译模式」入口追加在既有入口条末端（图标由当前模式决定）
        self._sync_translation_mode_entry()
        logger.info(
            "注入成功：预构建/定位/翻译模式切换入口已并入标题窗（图标跟随标题文本长度）"
        )

    def _sync_translation_mode_entry(self) -> None:
        """把标题窗「切换翻译模式」入口图标同步为当前生效模式。

        复用入口条同 id 覆盖语义：set_title_entries 按 id 合并覆盖后重排
        标题（set_text → update），图标立即随模式更新，不影响其它入口。
        """
        mode = "自动翻译" if self._auto_translate_on else "单击翻译"
        self._title_window.set_title_entries(
            [
                BoundAction(
                    id="toggle-translate-mode",
                    label=f"切换翻译模式（当前：{mode}）",
                    on_click=self._toggle_translation_mode,
                    icon=translation_mode_icon(self._auto_translate_on),
                )
            ]
        )

    def _toggle_translation_mode(self) -> None:
        """入口回调：运行时在「单击翻译 / 自动翻译」间切换并同步图标。

        只改内存状态（``_auto_translate_on``），不回写 config.json：重启后
        仍以配置文件原值为准。开启即启动轮询定时器（行为与锁定一致：仅
        锁定状态生效，新捕获台词走事件驱动路径即时评估）；关闭即停表。
        """
        self._auto_translate_on = not self._auto_translate_on
        if self._auto_translate_on:
            interval_ms = self._auto_interval_ms()
            self._auto_timer.setInterval(interval_ms)
            self._auto_timer.start()
            logger.info(
                "翻译模式已切换：自动翻译（每 %.1f 秒轮询，仅锁定状态生效；不写入 config.json）",
                interval_ms / 1000.0,
            )
            self._update_title("翻译模式：自动翻译（锁定后按间隔自动翻译）")
        else:
            self._auto_timer.stop()
            logger.info("翻译模式已切换：单击翻译（自动轮询停用；不写入 config.json）")
            self._update_title("翻译模式：单击翻译（锁定后单击触发翻译）")
        self._sync_translation_mode_entry()

    def request_prebuild(self) -> None:
        """绑定窗口入口回调：任务在途则唤回弹窗，否则重新进入扫描流程。"""
        if self._closed or self.skip_injection:
            return
        if self._asr_rt.enabled:
            # P5 决议：实时识别开启期间阻塞预构建（含其标题回写），
            # 正文窗仅显示实时识别内容
            logger.info("实时语音识别开启期间忽略预构建请求")
            return
        if self._prebuild_dialog is not None:
            if self._prebuild_running:
                # 隐藏到后台后的唤回：显示当前进度，不重启任务
                self._prebuild_dialog.show()
                self._prebuild_dialog.raise_()
                return
            # 上一轮已终态未关闭：按需求重新进入扫描流程（非断点续跑 UI）
            self._prebuild_dialog.deleteLater()
            self._prebuild_dialog = None
        try:
            files = scan_rpy_files(Path(self._game_dir)) if self._game_dir else []
        except Exception as exc:  # pragma: no cover - 目录不可读等
            logger.warning("预构建扫描失败：%s", exc)
            files = []
        if not files:
            self._update_title("预构建：未发现可解析的 .rpy 文件")
            logger.warning("预构建扫描未发现 .rpy 文件（game_dir=%s）", self._game_dir)
            return
        self._prebuild_dialog = PretranslateDialog(
            files,
            on_parse=self._start_prebuild,
            on_cancel=self._cancel_prebuild,
            on_hide=self._hide_prebuild_dialog,
        )
        self._prebuild_dialog.show()
        logger.info("预构建扫描完成：%d 个候选脚本文件，等待勾选", len(files))

    def _start_prebuild(self, files: list[str]) -> None:
        """解析回调：启动后台任务（独立连接 + 世代号；豁免前台 API 互斥）。"""
        if self._closed:
            return
        self._prebuild_seq += 1
        seq = self._prebuild_seq
        stop = threading.Event()
        self._prebuild_stop = stop
        store = translation_store.open_store(self._game_dir or None)
        if store is None:
            if self._prebuild_dialog is not None:
                self._prebuild_dialog.set_finished("error")
            self._update_title("预构建：翻译数据库不可用")
            logger.error("预构建启动失败：翻译数据库不可用（game_dir=%s）", self._game_dir)
            return
        task = PrebuildTask(
            files,
            Path(self._game_dir),
            self._config,
            store,
            stop,
            on_phase=lambda phase, info, _seq=seq: self._queue.put(
                ("prebuild_phase", {"seq": _seq, "phase": phase, "info": info})
            ),
            on_progress=lambda done, failed, skipped, _seq=seq: self._queue.put(
                (
                    "prebuild_progress",
                    {"seq": _seq, "done": done, "failed": failed, "skipped": skipped},
                )
            ),
            on_finished=lambda status, _seq=seq: self._queue.put(
                ("prebuild_finished", {"seq": _seq, "status": status})
            ),
        )
        threading.Thread(target=task.run, name=f"prebuild-{seq}", daemon=True).start()
        self._update_title("预构建：后台翻译进行中")
        logger.info(
            "预构建任务启动（seq=%d，%d 个文件；不受前台 API 互斥约束，写库经进程级写锁）",
            seq,
            len(files),
        )

    @property
    def _prebuild_running(self) -> bool:
        """本轮预构建任务在途（未取消未收束）。"""
        return self._prebuild_stop is not None and not self._prebuild_stop.is_set()

    def _cancel_prebuild(self) -> None:
        """弹窗取消：中止本轮任务并销毁弹窗（下次入口重新扫描）。"""
        dialog = self._prebuild_dialog
        self._abort_prebuild("取消")
        if dialog is not None:
            dialog.deleteLater()
        self._prebuild_dialog = None

    def _hide_prebuild_dialog(self) -> None:
        """隐藏到后台：只藏窗口，任务继续（再次点击入口按钮唤回）。"""
        if self._prebuild_dialog is not None:
            self._prebuild_dialog.hide()
            logger.info("预构建弹窗已隐藏到后台（任务继续；点击入口按钮可唤回）")

    def _abort_prebuild(self, reason: str) -> None:
        """中止本轮预构建（窗口关闭/游戏退出/取消共用）：在途结果作废不落库。"""
        if self._prebuild_stop is not None and not self._prebuild_stop.is_set():
            self._prebuild_stop.set()
            self._prebuild_seq += 1  # 在途进度/结束消息作废
            logger.info("预构建任务已中止：%s（已入库条目保留，可重新发起补译）", reason)
        self._prebuild_stop = None

    def _handle_prebuild_phase(self, payload: dict) -> None:
        if payload.get("seq") != self._prebuild_seq or self._prebuild_dialog is None:
            return  # 过期任务（已取消/已重开）或弹窗已销毁：丢弃
        phase = str(payload.get("phase") or "")
        info = payload.get("info") or {}
        if phase == "counting":
            self._prebuild_dialog.set_counting(int(info.get("discovered") or 0))
        elif phase == "ready":
            self._prebuild_dialog.set_ready(int(info.get("total") or 0))

    def _handle_prebuild_progress(self, payload: dict) -> None:
        if payload.get("seq") != self._prebuild_seq or self._prebuild_dialog is None:
            return
        self._prebuild_dialog.set_progress(
            int(payload.get("done") or 0),
            int(payload.get("failed") or 0),
            int(payload.get("skipped") or 0),
        )

    def _handle_prebuild_finished(self, payload: dict) -> None:
        status = str(payload.get("status") or "error")
        if payload.get("seq") == self._prebuild_seq:
            if self._prebuild_dialog is not None:
                self._prebuild_dialog.set_finished(status)
            self._prebuild_stop = None  # 任务已收束（线程随后自行退出）
            self._update_title(f"预构建结束（{status}）")
        logger.info("预构建任务收束（seq=%s，status=%s）", payload.get("seq"), status)
