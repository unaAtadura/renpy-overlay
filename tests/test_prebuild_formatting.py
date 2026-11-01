"""预构建 GUI 文案格式化与目录归一纯函数验证（无 Qt 依赖）。"""

from __future__ import annotations

from datetime import datetime

from renpy_overlay.pretranslate.formatting import (
    estimate_remaining_seconds,
    finished_text,
    format_checked,
    format_counting,
    format_db_count,
    format_dir_invalid,
    format_dir_ok,
    format_duration,
    format_progress,
    format_progress_time,
    format_scan_result,
    format_tool_progress,
    format_tool_scan_result,
    normalize_game_dir,
)

# ------------------------------------------------------------------ 目录归一


def test_normalize_returns_dir_with_game_subdir(tmp_path):
    game_root = tmp_path / "SomeGame"
    (game_root / "game").mkdir(parents=True)
    assert normalize_game_dir(game_root) == game_root


def test_normalize_ascends_from_game_dir(tmp_path):
    game_root = tmp_path / "SomeGame"
    game = game_root / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text('e "x"\n', encoding="utf-8")
    assert normalize_game_dir(game) == game_root


def test_normalize_game_named_dir_without_rpy_is_invalid(tmp_path):
    game = tmp_path / "SomeGame" / "game"
    game.mkdir(parents=True)  # 名为 game 但无 .rpy：不认为可选
    assert normalize_game_dir(game) is None


def test_normalize_missing_or_flat_dir_invalid(tmp_path):
    assert normalize_game_dir(tmp_path / "不存在") is None
    flat = tmp_path / "flat"
    flat.mkdir()
    assert normalize_game_dir(flat) is None


# ------------------------------------------------------------------ 文案


def test_format_counting():
    assert format_counting(3, 10, 42) == "统计中…（3/10 文件）已发现 42 条原文"


def test_format_progress():
    assert format_progress(0, 100, 0, 1) == "0/100（并发 1）"
    assert format_progress(97, 100, 3, 8) == "97/100（并发 8，失败 3）"


def test_format_scan_result():
    assert "5 个脚本" in format_scan_result(5)
    assert "未发现" in format_scan_result(0)


def test_format_tool_scan_result():
    assert "3 个 .rpyc 文件" in format_tool_scan_result(3, ".rpyc")
    assert "未发现任何 .rpa 文件" in format_tool_scan_result(0, ".rpa")


def test_finished_text_known_and_unknown():
    assert finished_text("completed") == "本轮处理完成。"
    assert "已取消" in finished_text("cancelled")
    assert "可重新处理" in finished_text("cancelled")
    assert "熔断" in finished_text("circuit_break")
    assert finished_text("whatever") == finished_text("error")


def test_format_tool_progress():
    """工具任务进度：done=已处理数，失败与跳过为注记细分。"""
    assert format_tool_progress(3, 17, 0, 0) == "3/17"
    assert format_tool_progress(3, 17, 1, 0) == "3/17（失败 1）"
    assert format_tool_progress(3, 17, 0, 2) == "3/17（跳过 2）"
    assert format_tool_progress(3, 17, 1, 2) == "3/17（失败 1，跳过 2）"


def test_format_checked_and_db_count():
    assert format_checked(3) == "已勾选 3 个文件"
    assert format_db_count(12) == "翻译库共 12 条"
    assert format_db_count(-1) == "数据库不可读"


def test_format_dir_messages(tmp_path):
    game_root = tmp_path / "G"
    (game_root / "game").mkdir(parents=True)
    text = format_dir_ok(game_root, game_root / "renpy_overlay_cache" / "translations.db")
    assert "游戏目录" in text and "数据库" in text
    assert "无效" in format_dir_invalid("X:/nowhere")


# ------------------------------------------------------------------ 进度时间


def test_format_duration_basic_and_hours():
    assert format_duration(0) == "00:00"
    assert format_duration(59.9) == "00:59"  # 向下取整到秒
    assert format_duration(60) == "01:00"
    assert format_duration(3599) == "59:59"
    assert format_duration(3600) == "1:00:00"
    assert format_duration(3661.7) == "1:01:01"


def test_format_duration_invalid_inputs():
    assert format_duration(None) == "--"
    assert format_duration(-1) == "--"
    assert format_duration(float("inf")) == "--"
    assert format_duration(float("-inf")) == "--"
    assert format_duration(float("nan")) == "--"


def test_estimate_remaining_linear_rate():
    assert estimate_remaining_seconds(10, 100, 20.0) == 180.0  # 0.5 条/秒
    assert estimate_remaining_seconds(50, 100, 10.0) == 10.0


def test_estimate_remaining_insufficient_basis_returns_none():
    """依据不足绝不硬给错值：未开始 / 无条目 / 无时长。"""
    assert estimate_remaining_seconds(0, 100, 10.0) is None  # 尚无完成条目
    assert estimate_remaining_seconds(10, 0, 10.0) is None  # 分母为 0
    assert estimate_remaining_seconds(10, 100, None) is None  # 未计时
    assert estimate_remaining_seconds(10, 100, 0.0) is None
    assert estimate_remaining_seconds(10, 100, -5.0) is None
    assert estimate_remaining_seconds(10, 100, float("nan")) is None


def test_estimate_remaining_completed_returns_zero():
    assert estimate_remaining_seconds(100, 100, 20.0) == 0.0
    assert estimate_remaining_seconds(150, 100, 20.0) == 0.0  # 超额完成同样归零


def test_format_progress_time_not_started_or_finished():
    """未开始 / 已收束（elapsed=None）→ 整行占位符。"""
    assert format_progress_time(0, 0, None) == "--"
    assert format_progress_time(50, 100, None) == "--"


def test_format_progress_time_no_estimate_basis():
    """统计阶段 / 首条完成前：已用时间照常显示，剩余为占位符。"""
    assert format_progress_time(0, 0, 12.0) == "已用 00:12 · 剩余 --"
    assert format_progress_time(0, 100, 12.0) == "已用 00:12 · 剩余 --"


def test_format_progress_time_with_estimate():
    assert format_progress_time(10, 100, 20.0) == "已用 00:20 · 剩余 03:00"
    # now_epoch 省略时不附完成时刻
    assert "预计" not in format_progress_time(10, 100, 20.0)


def test_format_progress_time_includes_finish_clock():
    """提供 now_epoch 时附本地完成时刻（HH:MM，与 datetime 同一公式）。"""
    now = datetime.now().timestamp()
    text = format_progress_time(10, 100, 20.0, now_epoch=now)
    expected = datetime.fromtimestamp(now + 180).strftime("%H:%M")
    assert text == f"已用 00:20 · 剩余 03:00（预计 {expected} 完成）"


def test_format_progress_time_completed_ratio():
    assert format_progress_time(100, 100, 20.0) == "已用 00:20 · 剩余 00:00"
    assert format_progress_time(120, 100, 20.0) == "已用 00:20 · 剩余 00:00"
