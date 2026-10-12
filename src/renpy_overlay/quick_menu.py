"""流式悬浮窗右键快捷菜单管理器：菜单入口的唯一扩展点。

管理右键菜单的构建与动作分发，并托管截图窗口池（最多 8 个，创建顺序依次
分配红橙黄绿青蓝紫黑边框，销毁按堆栈优先最新）。设计约束：

- 不依赖宿主类型：宿主能力（截图翻译、打开历史）全部经构造回调注入，
  后续新功能 = 在此加菜单项 + 注入对应回调，宿主零改动；
- 所有方法仅在 Qt 主线程调用（右键事件本就在主线程，菜单动作就地分发，
  不经过宿主的消息队列）；
- 布局锁定（lock_layout/unlock_layout）与窗口双击锁定相互独立：前者
  整体隐藏/恢复全部截图窗口并屏蔽创建/销毁入口，不改任何窗口的
  ``is_locked``；“已锁定窗口”（locked_windows）特指双击锁定（可识别）的窗口。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMenu

from .screenshot.frame_overlay import FrameOverlayLayer
from .screenshot.hotkeys import HOTKEY_MODE_OFF_TEXT, HOTKEY_MODE_ON_TEXT
from .screenshot.mouse_lock import MOUSE_LOCK_OFF_TEXT, MOUSE_LOCK_ON_TEXT
from .screenshot.remote_automation import (
    REMOTE_AUTO_OFF_TEXT,
    REMOTE_AUTO_ON_TEXT,
    REMOTE_HIDE_TEXT,
    REMOTE_SHOW_TEXT,
)
from .screenshot.window import FRAME_COLORS, ScreenshotWindow
from .video.asr_rt import ASR_RT_OFF_TEXT, ASR_RT_ON_TEXT

logger = logging.getLogger("renpy_overlay.quick_menu")

#: 截图窗口数量上限（需求：最多创建 8 个）
MAX_WINDOWS = 8
#: 级联创建时后一个窗口相对前一个的偏移（像素），避免完全重叠
CASCADE_STEP = 30
#: 录制中菜单项计时的刷新间隔（毫秒）：菜单打开期间按秒走动
RECORD_LABEL_REFRESH_MS = 1000


class QuickMenu:
    """右键快捷菜单 + 截图窗口池（仅 Qt 主线程使用）。"""

    def __init__(
        self,
        on_capture_click,
        on_open_history,
        on_recognize_song=None,
        on_open_song_history=None,
        on_open_chat=None,
        on_open_chat_history=None,
        on_open_translation_history=None,
        on_quit=None,
        on_record_video=None,
        on_open_video_editor=None,
        on_open_asr_history=None,
        frame_overlay_factory=None,
    ) -> None:
        self._on_capture_click = on_capture_click
        self._on_open_history = on_open_history
        self._on_recognize_song = on_recognize_song
        self._on_open_song_history = on_open_song_history
        self._on_open_chat = on_open_chat
        self._on_open_chat_history = on_open_chat_history
        self._on_open_translation_history = on_open_translation_history
        self._on_quit = on_quit
        # 视频功能（需求：录制视频/视频编辑入口 + 语音识别历史）：
        # 录制控制器经 attach_recorder 注入（菜单文案随录制状态变化）
        self._on_record_video = on_record_video
        self._on_open_video_editor = on_open_video_editor
        self._on_open_asr_history = on_open_asr_history
        self._recorder = None
        self._windows: list[ScreenshotWindow] = []  # 栈序 = 创建序，栈顶最新
        self._layout_locked = False  # 布局锁定：与窗口双击锁定相互独立
        self._hotkey_mode = None  # 快捷键模式（宿主经 attach_hotkey_mode 注入）
        self._mouse_lock = None  # 锁鼠标区域（宿主经 attach_mouse_lock 注入）
        # 遥控器自动化（宿主经 attach_remote_automation 注入；未注入时菜单不显示）
        self._remote_automation = None
        # 实时语音识别（宿主经 attach_asr_rt 注入；未注入时菜单不显示；
        # 开关文案随控制器 enabled 状态变化）
        self._asr_rt = None
        # 自动化装填期压制的提示框帧（截图窗口池索引集合；锁定期池序冻结）
        self._suppressed_frames: set[int] = set()
        # 框选提示覆盖层（锁定期线框）：惰性创建；工厂可注入（离线测试用）
        self._frame_overlay = None
        self._frame_overlay_factory = frame_overlay_factory or FrameOverlayLayer

    # ---- 右键菜单 -----------------------------------------------------------

    def show_context_menu(self, global_pos: QPoint) -> None:
        """构建并弹出右键菜单（标题窗 / 正文窗文字处右键触发）。

        菜单项顺序与分组按需求（.raw_plans/future_修改快捷菜单按键排序.txt +
        future_视频录制补充_实时asr.txt）：
        创建/销毁/快捷键模式 → 锁鼠标区域 → 遥控器自动化与隐藏遥控器
        （future_截图翻译功能补充_遥控截图自动化）→ 分隔线 → 录制视频/
        视频编辑 → 实时语音识别（开关式）→ 分隔线 → 听歌识曲 → 对话 →
        查看历史（二级菜单）→ 退出。

        菜单带 ``WindowStaysOnTopHint``：与流式双窗同属 TOPMOST 层组且后显示，
        保证菜单始终完整可见（配合宿主在菜单打开期间暂停周期性置顶重申，
        避免流式窗被 reassert 后反盖住菜单）。
        """
        menu = QMenu()
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        count = len(self._windows)
        create_action = menu.addAction(f"创建截图窗口（{count}/{MAX_WINDOWS}）")
        create_action.setEnabled(count < MAX_WINDOWS and not self._layout_locked)
        destroy_action = menu.addAction("销毁截图窗口")
        destroy_action.setEnabled(count > 0 and not self._layout_locked)
        hotkey_mode_action = None
        if self._hotkey_mode is not None:
            hotkey_mode_action = menu.addAction(
                HOTKEY_MODE_ON_TEXT if self._hotkey_mode.enabled else HOTKEY_MODE_OFF_TEXT
            )
        # 锁鼠标区域（需求：快捷键模式之下、与听歌识曲的分割线之上，
        # 与快捷键模式按钮间不需要分割线）
        mouse_lock_action = None
        if self._mouse_lock is not None:
            mouse_lock_action = menu.addAction(
                MOUSE_LOCK_ON_TEXT if self._mouse_lock.enabled else MOUSE_LOCK_OFF_TEXT
            )
        # 遥控器自动化（需求：锁定鼠标区域之下、分隔线之上两项；始终可点，
        # 快捷键模式未开启时点击由控制器经宿主提示——用户决议不用置灰）
        remote_automation_action = None
        remote_hidden_action = None
        if self._remote_automation is not None:
            remote_automation_action = menu.addAction(
                REMOTE_AUTO_ON_TEXT
                if self._remote_automation.enabled
                else REMOTE_AUTO_OFF_TEXT
            )
            remote_hidden_action = menu.addAction(
                REMOTE_SHOW_TEXT
                if self._remote_automation.remote_hidden
                else REMOTE_HIDE_TEXT
            )
        menu.addSeparator()
        # 视频功能（需求：位于「锁定鼠标区域」与「听歌识曲」之间，含分隔线）：
        # 录制中菜单项显示「录制视频 mm:ss」，未录制显示「录制视频」
        record_action = menu.addAction(
            self._recorder.menu_label()
            if self._recorder is not None
            else "录制视频"
        )
        editor_action = menu.addAction("视频编辑")
        # 实时语音识别（需求：视频编辑之下、听歌识曲之前的分隔线之上，
        # 开关式纯后台功能；未注入控制器时不显示）
        asr_rt_action = None
        if self._asr_rt is not None:
            asr_rt_action = menu.addAction(
                ASR_RT_ON_TEXT if self._asr_rt.enabled else ASR_RT_OFF_TEXT
            )
        menu.addSeparator()
        song_action = menu.addAction("听歌识曲")
        menu.addSeparator()
        chat_action = menu.addAction("对话")
        menu.addSeparator()
        # 查看历史二级菜单（需求给定顺序）：翻译历史 / 截图历史 / 识曲历史 /
        # 对话历史 / 语音识别历史（末尾追加，不需分隔线）
        view_history_menu = menu.addMenu("查看历史")
        translation_history_action = view_history_menu.addAction("翻译历史")
        screenshot_history_action = view_history_menu.addAction("截图历史")
        song_history_action = view_history_menu.addAction("识曲历史")
        chat_history_action = view_history_menu.addAction("对话历史")
        asr_history_action = view_history_menu.addAction("语音识别历史")
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        # 录制中菜单项计时实时刷新：QMenu.exec 的模态事件循环仍会派发
        # QTimer 事件，按秒更新「录制视频 mm:ss」（不依赖重新打开菜单）；
        # QTimer 无父（QuickMenu 非 QObject），局部引用在 exec 阻塞期间存活，
        # exec 返回后停止
        refresh_timer = None
        if self._recorder is not None and self._recorder.recording:
            refresh_timer = QTimer()
            refresh_timer.setInterval(RECORD_LABEL_REFRESH_MS)

            def _refresh_record_label() -> None:
                if self._recorder is not None:
                    record_action.setText(self._recorder.menu_label())

            refresh_timer.timeout.connect(_refresh_record_label)
            refresh_timer.start()
        try:
            chosen = menu.exec(global_pos)
        finally:
            if refresh_timer is not None:
                refresh_timer.stop()
        if chosen is create_action:
            self.create_window()
        elif chosen is destroy_action:
            self.destroy_latest()
        elif hotkey_mode_action is not None and chosen is hotkey_mode_action:
            self._hotkey_mode.toggle()
        elif mouse_lock_action is not None and chosen is mouse_lock_action:
            self._mouse_lock.toggle()
        elif remote_automation_action is not None and chosen is remote_automation_action:
            self._remote_automation.toggle_automation()
        elif remote_hidden_action is not None and chosen is remote_hidden_action:
            self._remote_automation.toggle_remote_hidden()
        elif chosen is record_action and callable(self._on_record_video):
            self._on_record_video()
        elif chosen is editor_action and callable(self._on_open_video_editor):
            self._on_open_video_editor()
        elif asr_rt_action is not None and chosen is asr_rt_action:
            self._asr_rt.toggle()
        elif chosen is song_action and callable(self._on_recognize_song):
            self._on_recognize_song()
        elif chosen is chat_action and callable(self._on_open_chat):
            self._on_open_chat()
        elif chosen is translation_history_action and callable(
            self._on_open_translation_history
        ):
            self._on_open_translation_history()
        elif chosen is screenshot_history_action and callable(self._on_open_history):
            self._on_open_history()
        elif chosen is song_history_action and callable(self._on_open_song_history):
            self._on_open_song_history()
        elif chosen is chat_history_action and callable(self._on_open_chat_history):
            self._on_open_chat_history()
        elif chosen is asr_history_action and callable(self._on_open_asr_history):
            self._on_open_asr_history()
        elif chosen is quit_action and callable(self._on_quit):
            self._on_quit()

    # ---- 截图窗口池 ---------------------------------------------------------

    def create_window(self) -> ScreenshotWindow | None:
        """新建一个截图窗口（颜色按创建顺序分配）；已满/布局锁定返回 None。"""
        if self._layout_locked:
            logger.info("截图布局已锁定，忽略创建截图窗口请求")
            return None
        if len(self._windows) >= MAX_WINDOWS:
            logger.info("截图窗口已达上限 %d 个，忽略创建请求", MAX_WINDOWS)
            return None
        index = len(self._windows)
        window = ScreenshotWindow(index, FRAME_COLORS[index], on_click=self._on_capture_click)
        window.show()  # 先 show 再定位（Qt 渲染管线要求），随后级联摆放
        center = self._screen_center()
        w, h = window.width(), window.height()
        offset = index * CASCADE_STEP
        window.move(center.x() - w // 2 + offset, center.y() - h // 2 + offset)
        self._windows.append(window)
        logger.info(
            "已创建截图窗口 #%d（边框色 %s），当前 %d/%d 个",
            index,
            FRAME_COLORS[index].name(),
            len(self._windows),
            MAX_WINDOWS,
        )
        return window

    def destroy_latest(self) -> bool:
        """堆栈式销毁：优先销毁最新创建的窗口；无窗口/布局锁定返回 False。"""
        if self._layout_locked:
            logger.info("截图布局已锁定，忽略销毁截图窗口请求")
            return False
        if not self._windows:
            logger.info("没有可销毁的截图窗口")
            return False
        window = self._windows.pop()
        logger.info("已销毁截图窗口 #%d（剩余 %d 个）", window.index, len(self._windows))
        window.close()
        window.deleteLater()
        return True

    def hide_all(self) -> None:
        """抓屏前短暂隐藏全部截图窗口与框选提示窗口（宿主负责流式窗）。

        布局锁定（快捷键模式）期间，框选提示窗口与截屏抓取范围精确重合、
        边框画在其内沿，抓屏前必须一并隐藏，否则线框会被截进识别画面；
        与截图窗口同一对 hide/show 原语，恢复侧见 :meth:`show_all`。
        """
        self._hide_frame_overlay()
        for window in self._windows:
            window.hide()

    def show_all(self) -> None:
        """抓屏结束后恢复显示（翻译流程 finally 中无条件调用）。

        布局锁定（快捷键模式）期间窗口被刻意整体隐藏，此时只恢复框选
        提示窗口、不放出截图窗口——否则锁定期内触发一次截图翻译，本方法
        就会把窗口全部放出来；提示窗口与隐藏侧成对恢复（抓屏后立即，
        几何保持 hide 前状态），异常路径同样恢复；自动化装填期压制的帧
        恢复后立即压回隐藏（抓屏时序不得复活被压制的提示框）。
        """
        if self._layout_locked:
            if self._frame_overlay is not None:
                self._frame_overlay.restore()
                for index in self._suppressed_frames:
                    self._frame_overlay.set_frame_visible(index, False)
            return
        for window in self._windows:
            window.show()

    def close_all(self) -> None:
        """销毁全部截图窗口（宿主退出时调用）。"""
        for window in self._windows:
            window.close()
            window.deleteLater()
        self._windows.clear()
        if self._frame_overlay is not None:
            self._frame_overlay.destroy()
            self._frame_overlay = None

    def reassert_topmost(self) -> None:
        """把全部截图窗口重新压回最顶层（对抗独占全屏被激活时的覆盖）。"""
        from . import win32api  # 局部导入：仅 Windows 存在

        for window in self._windows:
            try:
                win32api.set_topmost(window.hwnd)
            except Exception:  # pragma: no cover - 窗口销毁竞态
                return
        if self._frame_overlay is not None:  # 框选提示窗口随周期一并重申置顶
            self._frame_overlay.reassert_topmost()

    @property
    def window_count(self) -> int:
        return len(self._windows)

    # ---- 布局锁定（控制模块接口：供未来功能调用） ------------------------------

    def attach_hotkey_mode(self, hotkey_mode) -> None:
        """注入快捷键模式实例（菜单项按其状态显示文案与切换）。

        不走构造参数：HotkeyMode 需要 controller（本实例）先存在才能构造，
        宿主按「先建菜单、再附着模式」的顺序组装。
        """
        self._hotkey_mode = hotkey_mode

    def attach_mouse_lock(self, mouse_lock) -> None:
        """注入锁鼠标区域控制器（菜单项按其状态显示文案与切换）。

        同 attach_hotkey_mode 的附着约定。隔离不变量（需求）：控制器持有
        自己的窗口，不进本实例的截图窗口池 —— lock_layout / unlock_layout /
        locked_windows / hide_all / show_all / reassert_topmost 均只遍历池内
        窗口，不会隐藏、恢复或捕获锁鼠标区域的任何窗口。
        """
        self._mouse_lock = mouse_lock

    def attach_remote_automation(self, remote_automation) -> None:
        """注入遥控器自动化控制器（菜单项按其状态显示文案与切换）。

        同 attach_hotkey_mode 的附着约定：自动化控制器依赖遥控截图控制器
        与宿主回调，由宿主组装后注入。两项始终可点：快捷键模式未开启时
        由控制器经宿主提示（需求决议 A3，不在此处置灰）。
        """
        self._remote_automation = remote_automation

    def attach_recorder(self, recorder) -> None:
        """注入视频录制控制器（菜单项按其状态显示「录制视频 mm:ss」）。

        同 attach_hotkey_mode 的附着约定：VideoRecorder 需要 QuickMenu
        先存在才能由宿主组装（先建菜单、再附着控制器）。
        """
        self._recorder = recorder

    def attach_asr_rt(self, asr_rt) -> None:
        """注入实时语音识别控制器（菜单项按其 enabled 显示开/关文案）。

        同 attach_hotkey_mode 的附着约定：AsrRtController 依赖宿主回调
        （互斥槽/队列/三键提供者），由宿主组装后注入（先建菜单、再附着）。
        """
        self._asr_rt = asr_rt

    def lock_layout(self) -> None:
        """锁定截图布局：整体隐藏全部截图窗口 + 屏蔽创建/销毁入口。

        窗口彻底隐藏（不显示、不参与命中测试、不接收任何鼠标事件），
        从根本上保证不阻挡鼠标与屏幕交互；框选矩形保留在窗口对象上
        （``GetWindowRect`` 对隐藏窗口仍返回几何），快捷键触发截图翻译
        时现取坐标。只影响布局，不改任何窗口的双击锁定状态；幂等：
        重复调用无额外副作用。
        """
        self._layout_locked = True
        self.hide_all()
        self._show_frame_overlay()
        logger.info(
            "截图布局已锁定（%d 个窗口已隐藏，创建/销毁入口已屏蔽）",
            len(self._windows),
        )

    def unlock_layout(self) -> None:
        """解锁截图布局：恢复显示全部截图窗口 + 恢复创建/销毁入口。

        hide/show 不触碰窗口样式、标志与几何，边框、位置、尺寸、双击
        锁定态视觉与全部鼠标交互在恢复后与锁定前完全一致（hide_all/
        show_all 即截图翻译流程反复验证的同一对原语），多轮开启/关闭
        稳定一致。
        """
        self._layout_locked = False
        self._suppressed_frames.clear()  # 解锁即快照失效：压制集合一并清除
        self.show_all()
        self._hide_frame_overlay()
        logger.info("截图布局已解锁（窗口已恢复显示，创建/销毁入口已恢复）")

    def _show_frame_overlay(self) -> None:
        """显示锁定期框选提示窗口组（整窗穿透，不阻挡交互）。

        快照在锁定时刻生成：几何取各窗口 ``geometry()``（Qt 全局逻辑
        坐标，进程 Per-Monitor DPI Aware、与物理像素一致），颜色取各自
        ``border_color``；提示窗口与截屏抓取范围精确一致（零偏移），
        边框画在其内沿，抓屏时经 hide_all 隐藏、不污染识别截图。
        """
        frames = [
            (
                (
                    window.geometry().x(),
                    window.geometry().y(),
                    window.geometry().x() + window.geometry().width(),
                    window.geometry().y() + window.geometry().height(),
                ),
                window.border_color,
            )
            for window in self._windows
        ]
        if self._frame_overlay is None:
            self._frame_overlay = self._frame_overlay_factory()
        self._frame_overlay.show_frames(frames)
        for index in self._suppressed_frames:  # 自动化压制中的帧不显示
            self._frame_overlay.set_frame_visible(index, False)

    def set_frame_suppressed(self, window, suppressed: bool) -> None:
        """自动化装填期的提示框单帧压制（B 进入/离开装填区域时由控制器调用）。

        压制集合按截图窗口对象换算为池序索引（锁定期创建/销毁被屏蔽，
        索引稳定），hide_all/show_all 的批量隐藏与恢复不复活被压制帧；
        窗口不在池内（异常时序）静默忽略。
        """
        try:
            index = self._windows.index(window)
        except ValueError:
            return
        if suppressed:
            self._suppressed_frames.add(index)
        else:
            self._suppressed_frames.discard(index)
        if self._frame_overlay is not None:
            self._frame_overlay.set_frame_visible(index, not suppressed)

    def _hide_frame_overlay(self) -> None:
        """清除锁定期全部线框（覆盖层隐藏，下次锁定重新快照）。"""
        if self._frame_overlay is not None:
            self._frame_overlay.hide_overlay()

    def locked_windows(self) -> list[ScreenshotWindow]:
        """全部双击锁定的截图窗口（栈序；边框色可作窗口唯一标识）。

        特指双击锁定（可识别）的窗口，不含布局锁定锁定的窗口——遵守
        “只有双击锁定的窗口才能进行识别”的既有原则。
        """
        return [window for window in self._windows if window.is_locked]

    # ---- 内部 ---------------------------------------------------------------

    @staticmethod
    def _screen_center() -> QPoint:
        """主屏可用区域中心（避开任务栏），窗口初始摆放参照点。"""
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            return QPoint(geo.center().x(), geo.center().y())
        return QPoint(400, 300)
