"""手动选择目标进程：默认弹出 Tkinter 列表窗，不可用时回退命令行菜单。

选择窗刻意显示"分数 + 判定依据"，而不是只给一个进程名列表 —— 识别策略总会有
误判（例如某些游戏的 exe 叫 python.exe），把依据摊开给用户看，比让用户猜更可靠。

另提供「跳过注入」入口（需求 .raw_plans/future_跳过注入.txt）：向非 Ren'Py
游戏提供部分功能 —— 点击后经 IFileOpenDialog 选择数据目录，选择期间选择窗
保持打开，取消则留在选择窗，选定后以 :class:`SkipSelection` 作为选择结果。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path

from .discovery import Candidate, print_candidates
from .translation_store import CACHE_DIR_NAME

logger = logging.getLogger("renpy_overlay.picker")

_CJK_FONT = ("Microsoft YaHei UI", 9)


class SkipSelection:
    """选择窗「跳过注入」的结果：``path`` 为用户选择的数据目录（原始路径）。"""

    def __init__(self, path: str) -> None:
        self.path = path


def resolve_cache_dir(selected: Path) -> Path:
    """跳过注入模式：把用户选择的目录解析为数据缓存目录（纯函数，便于离线单测）。

    需求三规则：
    1. 目录名即为 ``renpy_overlay_cache`` → 使用当前目录；
    2. 目录名不同但其下已包含 ``renpy_overlay_cache`` → 使用该子目录；
    3. 其下不包含 → 使用（随后由存储管线自动创建的）``renpy_overlay_cache``。

    情况 1/2 目录与数据库已存在，各存储以 ``CREATE TABLE IF NOT EXISTS`` 打开，
    不覆盖既有数据库；情况 3 由存储管线自动建目录并新建数据库。既有存储管线
    约定 ``game_dir`` 下固定挂 ``renpy_overlay_cache/``，故调用方以
    ``resolve_cache_dir(selected).parent`` 作为 game_dir 传入即可命中全部三种
    情况的正确目录。
    """
    if selected.name == CACHE_DIR_NAME:
        return selected
    return selected / CACHE_DIR_NAME


def choose_target(
    candidates: list[Candidate],
    refresh: Callable[[], list[Candidate]] | None = None,
    use_gui: bool = True,
) -> Candidate | SkipSelection | None:
    """返回用户选中的候选或「跳过注入」结果，取消或没有候选时返回 None。"""
    if not candidates:
        print("未发现候选进程。请先启动 Ren'Py 游戏，或使用 --all 列出全部进程。")
        return None
    if use_gui:
        try:
            return _choose_gui(candidates, refresh)
        except Exception as exc:  # TclError / 无显示环境 / pywin32 缺失 等
            logger.warning("图形选择界面不可用（%s），回退到命令行菜单", exc)
    return _choose_console(candidates, refresh)


# ---------------------------------------------------------------- 图形界面


def _choose_gui(
    candidates: list[Candidate], refresh: Callable[[], list[Candidate]] | None
) -> Candidate | SkipSelection | None:
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk

    root = tk.Tk()
    root.title("renpy-overlay - 选择要注入的 Ren'Py 进程")
    root.geometry("1000x480")
    root.minsize(760, 380)
    root.attributes("-topmost", True)
    try:
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family=_CJK_FONT[0], size=_CJK_FONT[1])
    except Exception:  # pragma: no cover - 字体不可用时保持系统默认
        pass

    state: dict[str, object] = {"result": None, "rows": list(candidates)}

    # 三块区域（选择框 / 信息框 / 按钮区）各占一个独立的弹性容器，用 grid
    # 行权重分配空间：表格区 weight=3、详情区 weight=1 随窗口拉伸按比例伸缩，
    # 各自的 minsize 保证缩到最小窗口（760x380）时每块仍完整可见；按钮行权重 0
    # 固定高度贴底，不会被上方内容挤掉。
    frame = ttk.Frame(root, padding=8)
    frame.grid(row=0, column=0, sticky="nsew")
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    # 表格 : 详情 = 1 : 1 弹性分配拉伸空间；两者的"请求高度"故意设小
    # （Treeview height=3 行、Text height=2 行），否则 ttk 默认请求高度
    # （Treeview 10 行 = 600px）会超过窗口空间，grid 退化为按请求比例压缩、
    # 权重失效；minsize 兜底最小窗口（760x380）下的可见性。
    frame.rowconfigure(1, weight=1, minsize=120)
    # 详情行的 minsize 含上下间距（8+6）：保证 Text 本体的最小高度不低于 ~64px
    frame.rowconfigure(2, weight=1, minsize=80)

    hint = ttk.Label(
        frame,
        text="选择目标进程后点击「注入」。分数越高越可能是 Ren'Py 游戏，双击表格行可快速注入。",
    )
    hint.grid(row=0, column=0, sticky="ew", pady=(0, 6))

    columns = ("pid", "name", "arch", "score", "title")
    headings = {
        "pid": ("PID", 80),
        "name": ("进程名", 220),
        "arch": ("架构", 70),
        "score": ("分数", 60),
        "title": ("窗口标题", 420),
    }
    table_area = ttk.Frame(frame)
    table_area.grid(row=1, column=0, sticky="nsew")

    # 行高放大到当前实际值的 3 倍：Windows 高 DPI 缩放下 ttk 主题的默认行高
    # （约 20px）不会随字号一起放大，9pt 中文字体会被上下裁切；只改行高不动
    # 字体是最小侵入的修复。数值在运行时从主题读取，兼容不同主题与缩放。
    style = ttk.Style(root)
    try:
        current_row_height = int(style.lookup("Treeview", "rowheight") or 0)
    except (TypeError, ValueError):  # 主题返回了非数值
        current_row_height = 0
    if current_row_height <= 0:
        current_row_height = 20  # 主题未显式定义时的实际默认值
    style.configure("Treeview", rowheight=current_row_height * 3)

    tree = ttk.Treeview(table_area, columns=columns, show="headings", selectmode="browse", height=3)
    for key, (text, width) in headings.items():
        tree.heading(key, text=text)
        tree.column(key, width=width, anchor="w" if key in ("name", "title") else "center")
    scrollbar = ttk.Scrollbar(table_area, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    detail_area = ttk.Frame(frame)
    detail_area.grid(row=2, column=0, sticky="nsew", pady=(8, 6))
    detail = tk.Text(detail_area, height=2, wrap="word", relief="solid", borderwidth=1)
    detail.configure(state="disabled", background="#f7f7f9")
    detail.pack(fill="both", expand=True)

    buttons = ttk.Frame(frame)
    buttons.grid(row=3, column=0, sticky="ew")
    inject_button = ttk.Button(buttons, text="注入选中项")
    cancel_button = ttk.Button(buttons, text="取消")
    refresh_button = ttk.Button(buttons, text="重新扫描")
    skip_button = ttk.Button(buttons, text="跳过注入")
    inject_button.pack(side="right", padx=(6, 0))
    cancel_button.pack(side="right")
    refresh_button.pack(side="left")
    skip_button.pack(side="left", padx=(6, 0))

    def fill(rows: list[Candidate]) -> None:
        tree.delete(*tree.get_children())
        for index, item in enumerate(rows):
            tree.insert("", "end", iid=str(index), values=item.row())
        if rows:
            tree.selection_set("0")
            tree.focus("0")
            show_detail(rows[0])

    def show_detail(item: Candidate) -> None:
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", item.detail())
        detail.configure(state="disabled")

    def selected() -> Candidate | None:
        selection = tree.selection()
        if not selection:
            return None
        rows = state["rows"]
        assert isinstance(rows, list)
        return rows[int(selection[0])]

    def on_select(_event=None) -> None:
        item = selected()
        if item is not None:
            show_detail(item)

    def on_inject() -> None:
        item = selected()
        if item is None:
            return
        state["result"] = item
        root.destroy()

    def on_cancel() -> None:
        state["result"] = None
        root.destroy()

    def on_refresh() -> None:
        if refresh is None:
            return
        try:
            rows = refresh()
        except Exception as exc:  # pragma: no cover
            logger.error("重新扫描失败：%s", exc)
            return
        state["rows"] = rows
        fill(rows)

    def on_skip() -> None:
        """跳过注入：选数据目录后关闭选择窗；取消则留在选择窗（需求）。"""
        from .folder_dialog import pick_folder

        path = pick_folder(_root_hwnd(root))
        if not path:
            return  # 取消/失败：退回选择进程窗口（保持打开）
        state["result"] = SkipSelection(path)
        root.destroy()

    tree.bind("<<TreeviewSelect>>", on_select)
    tree.bind("<Double-1>", lambda _event: on_inject())
    tree.bind("<Return>", lambda _event: on_inject())
    inject_button.configure(command=on_inject)
    cancel_button.configure(command=on_cancel)
    refresh_button.configure(command=on_refresh, state=("normal" if refresh else "disabled"))
    skip_button.configure(command=on_skip)

    fill(list(candidates))
    root.bind("<Escape>", lambda _event: on_cancel())
    root.mainloop()

    result = state["result"]
    if isinstance(result, SkipSelection):
        return result
    return result if isinstance(result, Candidate) else None


def _root_hwnd(root) -> int:
    """Tk 根窗口的原生句柄（作 IFileOpenDialog 的属主）；取不到回退 0。

    Windows 上 ``winfo_id()`` 返回的是 Tk 子窗口，真正的顶层窗口是它的父窗口。
    """
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        return int(hwnd) if hwnd else int(root.winfo_id())
    except Exception:  # pragma: no cover - 非 Windows / 句柄不可得
        return 0


# ---------------------------------------------------------------- 命令行菜单


def _choose_console(
    candidates: list[Candidate], refresh: Callable[[], list[Candidate]] | None
) -> Candidate | None:
    rows = list(candidates)
    while True:
        print()
        print_candidates(rows)
        prompt = "输入 PID 回车确认（直接回车 = 最高分候选"
        prompt += "，r = 重新扫描" if refresh else ""
        prompt += "，q = 取消）: "
        try:
            answer = input(prompt).strip()
        except EOFError:  # pragma: no cover - 管道输入
            return None
        if not answer:
            return rows[0] if rows else None
        if answer.lower() == "q":
            return None
        if answer.lower() == "r":
            if refresh is not None:
                rows = refresh()
            continue
        if answer.isdigit():
            pid = int(answer)
            for item in rows:
                if item.pid == pid:
                    return item
            print(f"PID {pid} 不在候选列表中。")
            continue
        print("无效输入。")


def confirm(prompt: str) -> bool:
    """简单的 y/N 确认，默认取 No。"""
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:  # pragma: no cover
        return False
    return answer in ("y", "yes")


def is_interactive() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:  # pragma: no cover
        return False
