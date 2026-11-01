"""注入入口条与翻译模式切换的门面 Mixin：注入确认、预构建入口、模式切换。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``skip_injection`` / ``_injected`` / ``_title_window`` /
``_auto_translate_on`` / ``_prebuild_window`` / ``_game_dir`` / ``_config`` /
``_asr_rt`` / ``_queue``，以及 ``_update_title``。
"""

from __future__ import annotations

import logging

from ..bound_window import BoundAction
from .icons import BOUND_LOCATE_ICON, BOUND_PREBUILD_ICON, translation_mode_icon

logger = logging.getLogger("renpy_overlay.stream_window")


class InjectionFlowMixin:
    """注入确认入口条、预构建窗口入口、翻译模式运行时切换。"""

    # ------------------------------------------------------------ 注入入口条

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

    def request_prebuild(self) -> None:
        """预构建入口回调（标题窗入口条 / 右键菜单共用）：打开或唤回主窗口。"""
        if self._closed:
            return
        if self.skip_injection or not self._game_dir:
            # 跳过注入模式下无游戏目录可扫描：明确提示，不报错
            self._update_title("预构建：需注入游戏后可用")
            logger.info("跳过注入模式下忽略预构建请求（无游戏目录）")
            return
        if self._asr_rt.enabled:
            # P5 决议：实时识别开启期间阻塞预构建，正文窗仅显示实时识别内容
            logger.info("实时语音识别开启期间忽略预构建请求")
            return
        from ..prebuild_window import PrebuildWindow  # 局部导入：仅入口时加载

        window = self._prebuild_window
        if window is not None:
            # 已打开：唤回置顶（任务在途或选择态都直接显示）
            window.show()
            window.raise_()
            window.activateWindow()
            return
        self._prebuild_window = PrebuildWindow(
            self._config,
            initial_game_dir=str(self._game_dir),
            on_closed=lambda: setattr(self, "_prebuild_window", None),
        )
        self._prebuild_window.show()
        logger.info("预构建窗口已打开（game_dir=%s）", self._game_dir)

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
