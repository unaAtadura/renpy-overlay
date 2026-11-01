"""预构建 GUI 文案格式化与目录归一纯函数（无 Qt 依赖，离线单测）。

控件行为不做业务逻辑：文案格式化、目录校验归一、进度时间估算等抽为
本模块纯函数，``tests/test_prebuild_formatting.py`` 直接覆盖。
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

#: 终态 status → 用户可见文案（与预构建主窗口一致，措辞统一为"处理"）
_FINISHED_TEXT = {
    "completed": "本轮处理完成。",
    "cancelled": "已取消（已入库条目保留，可重新处理继续增量补译）。",
    "circuit_break": "服务不可达已熔断，请检查设置中的 API 地址后重试。",
    "error": "处理异常终止，详见日志。",
}


def normalize_game_dir(selected: str | Path) -> Path | None:
    """目录校验与归一（纯函数）：返回"含 game/ 子目录"的游戏根目录。

    - 选中目录已含 ``game/`` 子目录 → 直接使用；
    - 选中目录本身像 game 目录（名为 ``game`` 且直接含 ``.rpy``）→
      上溯取父目录；
    - 其余（不存在 / 结构不符）→ None（调用方提示无效）。
    """
    path = Path(selected)
    if not path.is_dir():
        return None
    if (path / "game").is_dir():
        return path
    if path.name.lower() == "game" and any(path.glob("*.rpy")):
        return path.parent
    return None


def format_dir_invalid(selected: str) -> str:
    return f"目录无效：{selected} 下未找到 game/ 子目录（请选择 Ren'Py 游戏根目录）。"


def format_dir_ok(game_dir: Path, db_path: Path) -> str:
    return f"游戏目录：{game_dir}\n数据库：{db_path}"


def format_scan_result(count: int) -> str:
    if count:
        return f"发现 {count} 个脚本文件（默认不勾选，请勾选需要处理的文件）"
    return "未发现任何 .rpy 脚本（可检查 game/ 目录与 tl 语言过滤设置）。"


def format_tool_scan_result(count: int, suffix: str) -> str:
    """工具页（反编译 / 解包）扫描结果文案（纯函数）。"""
    if count:
        return f"发现 {count} 个 {suffix} 文件（默认不勾选，请勾选需要处理的文件）"
    return f"未发现任何 {suffix} 文件。"


def format_counting(done: int, total: int, discovered: int) -> str:
    """统计阶段文案（纯函数）。"""
    return f"统计中…（{done}/{total} 文件）已发现 {discovered} 条原文"


def format_progress(done: int, total: int, failed: int, concurrency: int) -> str:
    """翻译阶段文案：``done/total`` 为主，附实时并发档位与失败数（纯函数）。"""
    text = f"{done}/{total}（并发 {concurrency}"
    if failed:
        text += f"，失败 {failed}"
    return text + "）"


def format_checked(count: int) -> str:
    return f"已勾选 {count} 个文件"


def format_tool_progress(done: int, total: int, failed: int, skipped: int) -> str:
    """工具任务（反编译 / 解包）进度文案（纯函数）。

    ``done`` 为已处理文件数（成功 / 失败 / 跳过均计入，进度条走满）；失败
    与跳过作为注记细分：``3/17（失败 1，跳过 2）``。
    """
    text = f"{done}/{total}"
    notes = []
    if failed:
        notes.append(f"失败 {failed}")
    if skipped:
        notes.append(f"跳过 {skipped}")
    if notes:
        text += "（" + "，".join(notes) + "）"
    return text


def finished_text(status: str) -> str:
    """终态文案（未知状态回退 error 文案；纯函数）。"""
    return _FINISHED_TEXT.get(status, _FINISHED_TEXT["error"])


def format_db_count(count: int) -> str:
    if count < 0:
        return "数据库不可读"
    return f"翻译库共 {count} 条"


# ------------------------------------------------------------------ 进度时间


def format_duration(seconds: float | None) -> str:
    """秒 → ``mm:ss``（满 1 小时为 ``h:mm:ss``）；None / 负数 / 非有限 → "--"。

    向下取整到秒：``59.9`` → ``00:59``（显示粒度即秒，不四舍五入）。
    """
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def estimate_remaining_seconds(
    done: int, total: int, elapsed_seconds: float | None
) -> float | None:
    """按已完成条目的线性速率估算剩余时长（秒）；估算依据不足返回 None。

    - ``done <= 0`` / ``total <= 0`` / ``elapsed`` 缺失或非正 → None
      （任务尚未产生可外推的速率，绝不硬给一个错值）；
    - ``done >= total`` → 0.0（已完成，无剩余）。
    """
    if elapsed_seconds is None or not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        return None
    if done <= 0 or total <= 0:
        return None
    if done >= total:
        return 0.0
    rate = done / elapsed_seconds
    if rate <= 0:  # pragma: no cover - done>0 且 elapsed>0 时恒为正，防御兜底
        return None
    return (total - done) / rate


def format_progress_time(
    done: int,
    total: int,
    elapsed_seconds: float | None,
    now_epoch: float | None = None,
) -> str:
    """进度区时间信息行文案：已运行时间 + 预计剩余（纯函数）。

    - ``elapsed_seconds`` 为 None（任务未开始 / 已收束）→ 整行 "--"；
    - 无估算依据（阶段 1 统计中 / 首条完成前）→ 剩余显示 "--"；
    - 可估算时给出剩余时长，``now_epoch`` 非空时附预计完成时刻
      （本地 ``HH:MM``，跨日由 datetime 自然进位）。
    """
    if elapsed_seconds is None:
        return "--"
    elapsed_text = format_duration(elapsed_seconds)
    remaining = estimate_remaining_seconds(done, total, elapsed_seconds)
    if remaining is None:
        return f"已用 {elapsed_text} · 剩余 --"
    text = f"已用 {elapsed_text} · 剩余 {format_duration(remaining)}"
    if now_epoch is not None:
        finish = datetime.fromtimestamp(now_epoch + remaining)
        text += f"（预计 {finish:%H:%M} 完成）"
    return text
