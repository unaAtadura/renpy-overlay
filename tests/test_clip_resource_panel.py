"""资源面板离线验证：video_dir 归一化（回归）与导入去重/排序纯逻辑。

回归背景：宿主注入的 ``default_video_dir`` 返回 Path 对象，QFileDialog
的 directory 参数不接受 Path（TypeError），构造入口必须归一化为 str。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from renpy_overlay.video.clip.resource_panel import ResourcePanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_video_dir_path_normalized_to_str(qapp):
    """Path 注入 → 内部归一化为 str（回归：QFileDialog directory 参数）。"""
    panel = ResourcePanel(video_dir=Path("D:/vids/video"))
    assert panel._video_dir == str(Path("D:/vids/video"))
    assert isinstance(panel._video_dir, str)


def test_video_dir_str_passthrough_and_none(qapp):
    """str 注入原样保留；None/空值回退空串（走系统记忆目录）。"""
    assert ResourcePanel(video_dir="D:/vids")._video_dir == "D:/vids"
    assert ResourcePanel()._video_dir == ""
    assert ResourcePanel(video_dir=None)._video_dir == ""


def test_add_paths_dedupes_and_accepts_strings(qapp, tmp_path):
    """导入去重：同路径跳过；字符串/Path 混合均可加入。"""
    panel = ResourcePanel()
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mkv"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    assert panel.add_paths([str(a), b]) == 2  # 首次：2 个新增
    assert panel.add_paths([str(a)]) == 0  # 重复：跳过
    assert panel.add_paths([str(tmp_path / "missing.mp4")]) == 0  # 不存在：跳过
