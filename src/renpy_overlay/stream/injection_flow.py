"""注入入口条与翻译模式切换的门面 Mixin：注入确认、预构建入口、模式切换。

宿主依赖（经 ``self`` 访问，由 :class:`StreamOverlayCore.__init__` 初始化）：
``_closed`` / ``skip_injection`` / ``_injected`` / ``_title_window`` /
``_auto_translate_on`` / ``_prebuild_window`` / ``_game_dir`` / ``_config`` /
``_asr_rt`` / ``_queue`` / ``_choice_log`` / ``_choice_log_window``，以及
``_update_title``。
"""

from __future__ import annotations

import logging

from ..bound_window import BoundAction
from ..pretranslate.formatting import prebuild_initial_dir
from .icons import (
    BOUND_LOCATE_ICON,
    BOUND_PREBUILD_ICON,
    CHOICE_LOG_ICON,
    translation_mode_icon,
)

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
        # 「选项副作用日志」入口同样追加在末端（开合状态由 label 同步）
        self._sync_choice_log_entry(opened=False)
        logger.info(
            "注入成功：预构建/定位/翻译模式切换/副作用日志入口已并入标题窗（图标跟随标题文本长度）"
        )

    def request_prebuild(self) -> None:
        """预构建入口回调（标题窗入口条 / 右键菜单共用）：打开或唤回主窗口。

        注入模式：game_dir 是进程 exe 所在目录（如 ``<根>/lib/windows-i686``），
        经 :func:`prebuild_initial_dir` 向上定位含 ``game/`` 子目录的游戏根后
        预填窗口目录行（四库的 game_dir 用法与位置不受影响）；定位失败回退
        原值，窗口内提示目录无效后由用户改选。
        跳过注入模式：不在入口处拦截——窗口目录行留空占位，由用户在窗口内
        「选择目录…」指定游戏根目录；可处理性完全由扫描结果决定。
        实时语音识别开启期间沿用 P5 决议阻塞预构建请求。
        """
        if self._closed:
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
        initial_dir = prebuild_initial_dir(self._game_dir)
        self._prebuild_window = PrebuildWindow(
            self._config,
            initial_game_dir=initial_dir,
            on_closed=lambda: setattr(self, "_prebuild_window", None),
        )
        self._prebuild_window.show()
        if initial_dir:
            logger.info("预构建窗口已打开（game_dir=%s）", initial_dir)
        else:
            logger.info("预构建窗口已打开（未预置目录，等待用户在窗口内选择）")

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

    # ------------------------------------------------------------ 选项副作用日志

    def _sync_choice_log_entry(self, opened: bool) -> None:
        """同步「选项副作用日志」入口 label（同 id 覆盖语义，开合状态一眼可辨）。"""
        label = "选项副作用日志（已开启）" if opened else "选项副作用日志"
        self._title_window.set_title_entries(
            [
                BoundAction(
                    id="choice-log",
                    label=label,
                    on_click=self._toggle_choice_log,
                    icon=CHOICE_LOG_ICON,
                )
            ]
        )

    def _toggle_choice_log(self) -> None:
        """入口回调：在「打开 / 关闭」两态间切换；关闭即清空累积日志。

        打开 = 创建单例半透明弹窗并锚定到本图标右上角（仅打开时计算一次，
        打开期间不自动跟随标题窗移动，拖动位置保持到关闭）；关闭 = 销毁
        弹窗并清空日志历史，下次打开从空白重新累积。构造失败兜底：引用
        保持 None、label 还原、提示走标题窗唯一出口。
        """
        if self._closed:
            return
        window = self._choice_log_window
        if window is not None:
            window.close()
            window.deleteLater()
            self._choice_log_window = None
            self._choice_log.clear()
            self._sync_choice_log_entry(opened=False)
            logger.info("选项副作用日志已关闭，累积日志已清空")
            return
        from .choice_log_window import ChoiceLogWindow  # 局部导入：仅入口时加载

        try:
            window = ChoiceLogWindow(
                self._choice_log,
                width=self._config.choice_log_window_width,
                height=self._config.choice_log_window_height,
            )
            window.anchor_to(self._title_window.entry_top_right_global("choice-log"))
            window.show()
        except Exception:
            logger.exception("选项副作用日志窗口打开失败")
            self._update_title("副作用日志窗口打开失败")
            self._sync_choice_log_entry(opened=False)
            return
        self._choice_log_window = window
        self._sync_choice_log_entry(opened=True)
        logger.info("选项副作用日志已打开（锚定到入口图标右上角）")
