"""截图历史删除回位与原图导出的离线验证。

删除回位索引与导出核心（目录推导 / 扩展名推断 / 独占写入与重名跳过）均为
纯函数，直接离线验证；窗口层导出方法用 ``__new__`` 跳过 QWidget 初始化、
鸭子 stub 注入按钮/计时器/条带（项目约定：不构造真实 QWidget），配合
open_store 的临时 sqlite 连接验证（ScreenshotStore 全程只在主线程使用）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from renpy_overlay.screenshot.history_window import (
    EXPORT_DIR_NAME,
    EXPORT_EXPORTED,
    EXPORT_FAILED,
    EXPORT_SKIPPED,
    ScreenshotHistoryWindow,
    _index_after_delete,
    blob_extension,
    export_original_bytes,
    export_target_dir,
)
from renpy_overlay.screenshot.store import open_store


def test_delete_middle_keeps_same_position():
    # 删除中间条目：下一条顺位前移到原位置（索引不变）
    assert _index_after_delete(1, 4) == 1


def test_delete_last_falls_back_to_previous():
    # 删除末尾条目：原索引在新列表中越界，回退选中前一条
    assert _index_after_delete(4, 4) == 3
    assert _index_after_delete(2, 2) == 1  # 删的是最后一条（新列表只剩 2 条）


def test_delete_only_record_enters_empty_state():
    assert _index_after_delete(0, 0) == -1


def test_delete_first_of_many():
    assert _index_after_delete(0, 3) == 0


@pytest.mark.parametrize("deleted,new_count,expected", [(0, 2, 0), (1, 2, 1), (5, 3, 2)])
def test_index_never_out_of_range(deleted, new_count, expected):
    # 任意输入下结果都在新列表范围内（防御越界）
    assert _index_after_delete(deleted, new_count) == expected
    assert expected < new_count


# ---- 导出纯函数：目录推导 / 扩展名 / 独占写入与重名跳过 ------------------------------


def test_export_target_dir_is_sibling_of_db(tmp_path):
    """导出目录与 screenshot.db 同级（renpy_overlay_cache\\screenshot），非其子目录。"""
    db_path = tmp_path / "renpy_overlay_cache" / "screenshot.db"
    target = export_target_dir(db_path)
    assert target == db_path.parent / EXPORT_DIR_NAME
    assert target.parent == db_path.parent
    assert target.name == "screenshot"


def test_blob_extension_by_magic():
    """扩展名按 BLOB 魔数推断：JPEG → .jpg，PNG → .png，未知兜底 .jpg。"""
    assert blob_extension(b"\xff\xd8\xff\xe0rest") == ".jpg"
    assert blob_extension(b"\x89PNG\r\n\x1a\n") == ".png"
    assert blob_extension(b"") == ".jpg"  # 空由调用方先拦截，此处仅兜底


def test_export_creates_dir_and_writes_original_bytes(tmp_path):
    """目录不存在自动创建（含父目录）；写入字节与 BLOB 逐位一致，不重编码。"""
    store = open_store(tmp_path)
    blob = b"\xff\xd8\xff" + "原始字节".encode() + b"\x00\x01\xff"
    store.insert(ts=1000, ocr_text="译文", img_original=blob, img_thumbnail=b"t")
    target_dir = export_target_dir(store.path)
    assert not target_dir.exists()
    assert export_original_bytes(blob, target_dir, 1) == EXPORT_EXPORTED
    out = target_dir / "1.jpg"
    assert out.read_bytes() == blob  # 数据库原始字节，逐位一致


def test_export_filename_uses_decimal_primary_key(tmp_path):
    """文件名 = 主键十进制字符串（无前导零）+ 魔数扩展名；PNG 按魔数命名。"""
    png = b"\x89PNG\r\n\x1a\npayload"
    assert export_original_bytes(png, tmp_path, 123) == EXPORT_EXPORTED
    assert (tmp_path / "123.png").read_bytes() == png


def test_export_skips_existing_file_and_directory_never_overwrites(tmp_path):
    """重名（文件或目录同名）一律跳过，绝不覆盖/截断；日志含 id 与目标路径。"""
    blob = b"\xff\xd8\xffnew"
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    (target_dir / "7.jpg").write_bytes(b"stale")
    assert export_original_bytes(blob, target_dir, 7) == EXPORT_SKIPPED
    assert (target_dir / "7.jpg").read_bytes() == b"stale"  # 原内容未被覆盖
    (target_dir / "8.jpg").mkdir()  # 同名目录同样跳过
    assert export_original_bytes(blob, target_dir, 8) == EXPORT_SKIPPED
    assert (target_dir / "8.jpg").is_dir()


def test_export_empty_blob_skipped_without_file(tmp_path):
    assert export_original_bytes(b"", tmp_path, 3) == EXPORT_SKIPPED
    assert not (tmp_path / "3.jpg").exists()


def test_export_failed_when_dir_creation_fails(tmp_path):
    """目录创建失败（父路径被文件占位）→ failed；不抛出。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("file", encoding="ascii")  # 目录名被文件占用
    assert export_original_bytes(b"\xff\xd8x", blocker / "sub", 3) == EXPORT_FAILED
    assert not (blocker / "sub").exists()


# ---- 窗口层导出：__new__ 跳过 QWidget 初始化 + 鸭子 stub ----------------------------


class _StubButton:
    """QPushButton 替身：记录启用状态变化。"""

    def __init__(self) -> None:
        self.enabled = False
        self.enabled_history: list[bool] = []

    def setEnabled(self, value: bool) -> None:  # noqa: N802 - Qt 约定
        self.enabled = value
        self.enabled_history.append(value)


class _StubTimer:
    """QTimer 替身：记录启动次数，分片由测试手动步进（替代事件循环）。"""

    def __init__(self) -> None:
        self.started = 0

    def start(self) -> None:
        self.started += 1


def _make_host(store, entries, selected_index: int = -1) -> ScreenshotHistoryWindow:
    """构造窗口层测试替身：跳过 QWidget 初始化，鸭子注入导出方法触碰的成员。"""
    host = ScreenshotHistoryWindow.__new__(ScreenshotHistoryWindow)
    host._store = store
    host._entries = list(entries)
    host._strip = SimpleNamespace(selected_index=selected_index)
    host._export_btn = _StubButton()
    host._export_all_btn = _StubButton()
    host._export_timer = _StubTimer()
    host._export_ids = []
    host._export_stats = [0, 0, 0]
    return host


def test_window_export_selected_writes_selected_record(tmp_path):
    """单条导出：按选中记录主键写 <id>.jpg；不触碰条带/详情等任何展示状态。"""
    store = open_store(tmp_path)
    blob = b"\xff\xd8\xffselected"
    store.insert(ts=1000, ocr_text="译文", img_original=blob, img_thumbnail=b"t")
    host = _make_host(store, [(1, 1000)], selected_index=0)
    host._export_selected()
    assert (export_target_dir(store.path) / "1.jpg").read_bytes() == blob
    assert host._export_timer.started == 0  # 单条导出同步完成，不走分片


def test_window_export_selected_without_selection_or_store_is_safe(tmp_path):
    """无选中 / store 不可用：只记日志安全返回，不建目录、不抛出。"""
    store = open_store(tmp_path)
    host = _make_host(store, [(1, 1000)], selected_index=-1)
    host._export_selected()  # 无选中
    assert not export_target_dir(store.path).exists()
    host_no_store = _make_host(None, [(1, 1000)], selected_index=0)
    host_no_store._export_selected()  # store None：安全返回
    assert host_no_store._export_timer.started == 0


def test_window_export_all_guards_empty_db_reentry_and_missing_store(tmp_path):
    """空库 / store 不可用不启动；进行中重复点击被忽略（防重入）。"""
    _make_host(None, [], selected_index=-1)._export_all()  # store None：安全返回
    store = open_store(tmp_path)
    empty = _make_host(store, [], selected_index=-1)
    empty._export_all()  # 空库
    assert empty._export_timer.started == 0

    store.insert(ts=1000, ocr_text="a", img_original=b"\xff\xd8a", img_thumbnail=b"t")
    store.insert(ts=2000, ocr_text="b", img_original=b"\xff\xd8b", img_thumbnail=b"t")
    host = _make_host(store, [(1, 1000), (2, 2000)])
    host._export_all()
    assert host._export_timer.started == 1
    assert host._export_all_btn.enabled is False  # 进行中禁用防重入
    assert host._export_ids == [1, 2]
    host._export_all()  # 重复点击：忽略
    assert host._export_timer.started == 1
    assert host._export_ids == [1, 2]


def test_window_export_all_step_exports_all_and_skips_existing(tmp_path):
    """全部导出不受选中影响，逐条写库中原始字节；已存在跳过且不覆盖。"""
    store = open_store(tmp_path)
    blob_a, blob_b = b"\xff\xd8\xffA", b"\xff\xd8\xffB"
    store.insert(ts=1000, ocr_text="a", img_original=blob_a, img_thumbnail=b"t")
    store.insert(ts=2000, ocr_text="b", img_original=blob_b, img_thumbnail=b"t")
    host = _make_host(store, [(1, 1000), (2, 2000)])
    target_dir = export_target_dir(store.path)
    target_dir.mkdir(parents=True)
    (target_dir / "1.jpg").write_bytes(b"existing")  # 预置重名文件
    host._export_all()
    host._export_all_step()  # 手动泵分片（两条在同一批内完成）
    assert host._export_ids == []
    assert (target_dir / "1.jpg").read_bytes() == b"existing"  # 未覆盖
    assert (target_dir / "2.jpg").read_bytes() == blob_b
    assert host._export_stats == [1, 1, 0]  # 成功 1 / 跳过 1 / 失败 0
    assert host._export_all_btn.enabled is True  # 完成后恢复


def test_window_export_all_step_tolerates_deleted_record(tmp_path):
    """导出进行中条目被删除（record 返回 None）：计入失败，不中断其余记录。"""
    store = open_store(tmp_path)
    store.insert(ts=1000, ocr_text="a", img_original=b"\xff\xd8A", img_thumbnail=b"t")
    host = _make_host(store, [(1, 1000), (99, 2000)])  # id=99 已不在库中
    host._export_all()
    host._export_all_step()
    assert host._export_stats == [1, 0, 1]
    assert host._export_all_btn.enabled is True


def test_window_export_all_abort_when_dir_creation_fails(tmp_path):
    """导出目录创建失败（路径被同名文件占位）：立即中止，不启动分片。"""
    store = open_store(tmp_path)
    store.insert(ts=1000, ocr_text="a", img_original=b"\xff\xd8A", img_thumbnail=b"t")
    occupied = export_target_dir(store.path)
    occupied.write_text("occupied", encoding="ascii")  # 导出目录路径被同名文件占用
    host = _make_host(store, [(1, 1000)])
    host._export_all()
    assert host._export_timer.started == 0
    assert host._export_ids == []  # 未进入在途
    assert host._export_all_btn.enabled_history == []  # 中止前未触碰按钮状态
