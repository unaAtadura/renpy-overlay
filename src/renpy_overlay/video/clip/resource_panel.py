"""资源窗口：导入视频/导入文件夹按钮 + 视频列表 + 文件名排序切换。

导入逻辑（future_*.txt 资源导入逻辑）：
- 选择文件夹：扫描当前文件夹下全部支持格式（不递归子目录），按扫描
  到的顺序直接追加到末尾；
- 选择视频：仅导入单个视频；
- 排序按钮：按文件名升序 → 再点降序 → 循环切换（仅影响当前列表显示
  顺序，不触发重新扫描）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("renpy_overlay.video.clip.resource_panel")

#: 支持的媒体格式（future_*.txt：MP4 mkv wmv avi）
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".wmv", ".avi"}
MEDIA_FILTER = "视频文件 (*.mp4 *.mkv *.wmv *.avi);;所有文件 (*.*)"


class ResourcePanel(QWidget):
    """资源窗口面板：导入/排序/双击载入。

    ``video_dir``：文件对话框的起始目录（宿主注入的默认视频目录，
    str 或 Path，内部归一化为 str——QFileDialog 不接受 Path 对象；
    为空时走系统记忆目录）。
    """

    load_requested = pyqtSignal(str)  # 双击条目 → 请求载入播放

    def __init__(
        self, video_dir: str | Path | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        # 归一化为 str：QFileDialog 的 directory 参数不接受 Path 对象
        self._video_dir = str(video_dir) if video_dir else ""
        self._videos: list[Path] = []
        self._sort_order: str = "none"  # none → asc → desc 循环
        self._sort_button: QPushButton
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        import_button = QPushButton("导入视频")
        import_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        import_button.clicked.connect(self._import_videos)
        folder_button = QPushButton("导入文件夹")
        folder_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        folder_button.clicked.connect(self._import_folder)
        self._sort_button = QPushButton("排序: 文件名")
        self._sort_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._sort_button.clicked.connect(self._toggle_sort)
        buttons.addWidget(import_button)
        buttons.addWidget(folder_button)
        buttons.addWidget(self._sort_button)
        layout.addLayout(buttons)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(
            lambda item: self.load_requested.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        layout.addWidget(self._list, stretch=1)

    # ---- 导入 ----------------------------------------------------------------

    def _import_videos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "导入视频", self._video_dir, MEDIA_FILTER
        )
        added = self.add_paths(paths)
        logger.info("导入视频 %d 个（新增 %d，重复跳过）", len(paths), added)

    def _import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "导入文件夹", self._video_dir)
        if not folder:
            return
        # 扫描当前文件夹（os.scandir 顺序 = 扫描到的顺序），不递归子目录
        found = [
            entry.path
            for entry in os.scandir(folder)
            if entry.is_file()
            and Path(entry.path).suffix.lower() in VIDEO_EXTENSIONS
        ]
        added = self.add_paths(found)
        logger.info(
            "文件夹扫描 %d 个视频（新增 %d，不扫描子目录）：%s", len(found), added, folder
        )

    def add_paths(self, paths) -> int:
        """追加导入（同路径已存在跳过）；返回新增数量。"""
        added = 0
        for raw in paths:
            path = Path(raw)
            if not path.is_file() or path in self._videos:
                continue
            self._videos.append(path)
            self._append_item(path)
            added += 1
        if added:
            self._refresh_labels()
        return added

    def _append_item(self, path: Path) -> None:
        item = QListWidgetItem(path.name)
        item.setToolTip(str(path))
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        self._list.addItem(item)

    # ---- 排序 ----------------------------------------------------------------

    def _toggle_sort(self) -> None:
        """升序 → 降序 → （原导入顺序）循环切换。"""
        order = {"none": "asc", "asc": "desc", "desc": "none"}[self._sort_order]
        self._sort_order = order
        if order == "none":
            logger.info("排序已还原为导入顺序")
        else:
            logger.info("按文件名%s排列", "升序" if order == "asc" else "降序")
        self._refresh_labels()

    def _refresh_labels(self) -> None:
        """按当前排序重建列表项（排序仅影响显示顺序）。"""
        label = {"none": "排序: 文件名", "asc": "排序: 升序", "desc": "排序: 降序"}[
            self._sort_order
        ]
        self._sort_button.setText(label)
        videos = list(self._videos)
        if self._sort_order == "asc":
            videos.sort(key=lambda p: p.name.lower())
        elif self._sort_order == "desc":
            videos.sort(key=lambda p: p.name.lower(), reverse=True)
        self._list.clear()
        for path in videos:
            self._append_item(path)
