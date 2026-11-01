"""unrpa 目录判定与 game↔unrpa 路径映射纯函数（无 Qt / 子进程依赖）。

三页签扫描与处理共用的路径规则：

- unrpa 工作目录：``<游戏根>/renpy_overlay_cache/unrpa``（与 translations.db
  同一缓存目录）；
- 扫描根判定（两页各自独立判定）：unrpa 目录存在且递归含至少一个指定后缀
  文件 → 本页扫描根取 unrpa，否则取 ``game``；
- 反编译映射：扫描根为 unrpa → 同级同名 .rpy；扫描根为 game → 以 unrpa
  替代 game 段保持相对路径。两分支统一为 ``unrpa/<相对扫描根路径去后缀>.rpy``；
- 解包映射（归档专属目录）：``game/x.rpa → unrpa/x/``、
  ``game/folder/y.rpa → unrpa/folder/y/``——不同归档写入各自目录，同名
  条目互不覆盖；归档内自带 ``game/`` 前缀的条目在解包后剥离（见
  ``external_tools``）。

相对路径统一为 posix 风格（``as_posix()``），跨 Windows 分隔符稳定。
"""

from __future__ import annotations

from pathlib import Path

from ..translation_store import CACHE_DIR_NAME

#: unrpa 工作目录名（位于 renpy_overlay_cache 下）
UNRPA_DIR_NAME = "unrpa"


def unrpa_root(game_dir: Path) -> Path:
    """游戏根目录对应的 unrpa 工作目录。"""
    return Path(game_dir) / CACHE_DIR_NAME / UNRPA_DIR_NAME


def resolve_scan_root(game_dir: Path, suffix: str) -> Path:
    """扫描根判定（纯函数）：unrpa 优先，回退 ``game``。

    - unrpa 目录存在且递归含至少一个 ``suffix`` 文件 → 返回 unrpa；
    - 否则返回 ``<游戏根>/game``（含不存在 unrpa / unrpa 为空 / unrpa 中
      无该类型文件三种情形）。
    ``suffix`` 形如 ``".rpy"`` / ``".rpyc"``（各页独立调用）。
    """
    unrpa = unrpa_root(game_dir)
    if unrpa.is_dir() and any(path.is_file() for path in unrpa.rglob(f"*{suffix}")):
        return unrpa
    return Path(game_dir) / "game"


def list_relative(root: Path, suffix: str) -> list[str]:
    """递归列出 ``root`` 下指定后缀文件的相对路径（升序稳定；根不存在为空）。

    与 :func:`resolve_scan_root` 配套：返回值即"相对扫描根路径"，直接作为
    勾选列表条目与任务输入。不含归档内条目（.rpa 内的 .rpyc 由解包页负责
    解出后再进入本列表）。
    """
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob(f"*{suffix}")
        if path.is_file()
    )


def decompile_target(game_dir: Path, scan_root: Path, rel: str) -> Path:
    """反编译输出目标路径（纯函数）。

    ``rel`` 为相对扫描根的 .rpyc 路径；目标统一为
    ``unrpa/<rel 去后缀>.rpy``——扫描根为 unrpa 时即同级同名（已存在则
    跳过），扫描根为 game 时即 unrpa 替代 game 段的同构路径。
    ``scan_root`` 仅用于表达映射语义（两分支规则一致），不参与计算。
    """
    return (unrpa_root(game_dir) / rel).with_suffix(".rpy")


def extract_target_dir(game_dir: Path, rel_archive: str) -> Path:
    """解包输出目录（纯函数）：归档专属目录，隔离不同归档的同名条目。

    ``rel_archive`` 为相对 ``game`` 的归档路径，输出为去掉 .rpa 后缀的
    同构目录：``x.rpa → unrpa/x/``、``folder/y.rpa → unrpa/folder/y/``。
    "输出目录已存在且非空 → 整档跳过"落在专属目录上，即"该归档已解包过"
    的幂等防护，不再因其他归档的产物而误跳。（目录本身由调用方创建。）
    """
    return unrpa_root(game_dir) / Path(rel_archive).with_suffix("")
