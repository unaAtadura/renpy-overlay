"""离线批量预构建翻译缓存（纯逻辑层：扫描 / 解析 / 清洗 / 可中止任务流水）。

包内模块只依赖标准库与既有 translator / translation_store，不依赖
PyQt6 / win32api，全部可离线单测；GUI（预构建主窗口 ``prebuild_window``）
在包外的顶层模块。

导出：``scan_rpy_files``、``collect_texts``、``SourceText`` 及清洗口径
``clean_text`` / ``build_choice_key``；批量任务（``BatchTask``）与自适应
并发（``AdaptiveLimiter``）由 ``runner`` / ``concurrency`` 子模块提供。
"""

from __future__ import annotations

from .cleaning import build_choice_key, clean_text
from .parser import SourceText, collect_texts, extract_blocks, extract_from_rpy, scan_rpy_files

__all__ = [
    "SourceText",
    "build_choice_key",
    "clean_text",
    "collect_texts",
    "extract_blocks",
    "extract_from_rpy",
    "scan_rpy_files",
]
