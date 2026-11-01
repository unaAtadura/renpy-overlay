"""unrpa 扫描根判定与 game↔unrpa 路径映射纯函数验证（无 Qt 依赖）。"""

from __future__ import annotations

from pathlib import Path

from renpy_overlay.pretranslate.overlay_paths import (
    decompile_target,
    extract_target_dir,
    list_relative,
    resolve_scan_root,
    unrpa_root,
)


def make_unrpa(tmp_path: Path, *files: str) -> Path:
    unrpa = unrpa_root(tmp_path)
    for rel in files:
        target = unrpa / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    return unrpa


# ------------------------------------------------------------------ 扫描根判定


def test_resolve_scan_root_game_when_unrpa_missing(tmp_path):
    (tmp_path / "game").mkdir()
    assert resolve_scan_root(tmp_path, ".rpy") == tmp_path / "game"


def test_resolve_scan_root_unrpa_when_has_suffix(tmp_path):
    make_unrpa(tmp_path, "ep2.rpy", "nested/deep.ep3.rpy")
    assert resolve_scan_root(tmp_path, ".rpy") == unrpa_root(tmp_path)


def test_resolve_scan_root_game_when_unrpa_empty(tmp_path):
    unrpa_root(tmp_path).mkdir(parents=True)  # 存在但为空
    assert resolve_scan_root(tmp_path, ".rpy") == tmp_path / "game"


def test_resolve_scan_root_game_when_unrpa_has_other_suffix(tmp_path):
    make_unrpa(tmp_path, "a.rpyc")  # 只看本页后缀
    assert resolve_scan_root(tmp_path, ".rpy") == tmp_path / "game"


def test_resolve_scan_root_independent_per_page(tmp_path):
    """页 0 看 .rpy、页 1 看 .rpyc：互不影响，各自独立判定。"""
    make_unrpa(tmp_path, "b.rpyc")
    assert resolve_scan_root(tmp_path, ".rpy") == tmp_path / "game"
    assert resolve_scan_root(tmp_path, ".rpyc") == unrpa_root(tmp_path)


def test_resolve_scan_root_ignores_directory_named_like_suffix(tmp_path):
    decoy = unrpa_root(tmp_path) / "fake.rpy"
    decoy.mkdir(parents=True)  # 目录而非文件：不计数
    assert resolve_scan_root(tmp_path, ".rpy") == tmp_path / "game"


# ------------------------------------------------------------------ 递归列举


def test_list_relative_sorted_and_recursive(tmp_path):
    make_unrpa(tmp_path, "b.rpy", "a/c.rpy", "a.rpy", "ignore.txt")
    assert list_relative(unrpa_root(tmp_path), ".rpy") == [
        "a.rpy",
        "a/c.rpy",
        "b.rpy",
    ]


def test_list_relative_suffix_not_prefix_matched(tmp_path):
    make_unrpa(tmp_path, "x.rpyc")  # "*.rpy" 不得匹配 "x.rpyc"
    assert list_relative(unrpa_root(tmp_path), ".rpy") == []


def test_list_relative_missing_root_returns_empty(tmp_path):
    assert list_relative(tmp_path / "nowhere", ".rpa") == []


# ------------------------------------------------------------------ 反编译映射


def test_decompile_target_from_game_root(tmp_path):
    target = decompile_target(tmp_path, tmp_path / "game", "ep2.rpyc")
    assert target == unrpa_root(tmp_path) / "ep2.rpy"


def test_decompile_target_from_game_subdir(tmp_path):
    target = decompile_target(tmp_path, tmp_path / "game", "folder/ep3.rpyc")
    assert target == unrpa_root(tmp_path) / "folder" / "ep3.rpy"


def test_decompile_target_from_unrpa_same_dir(tmp_path):
    unrpa = unrpa_root(tmp_path)
    target = decompile_target(tmp_path, unrpa, "nested/x.rpyc")
    assert target == unrpa / "nested" / "x.rpy"  # 同级同名


def test_decompile_target_multi_dot_name_keeps_stem(tmp_path):
    target = decompile_target(tmp_path, tmp_path / "game", "ep.1.0.rpyc")
    assert target.name == "ep.1.0.rpy"


# ------------------------------------------------------------------ 解包映射


def test_extract_target_dir_root_archive(tmp_path):
    assert extract_target_dir(tmp_path, "achieve.rpa") == unrpa_root(tmp_path) / "achieve"


def test_extract_target_dir_subdir_archive(tmp_path):
    assert extract_target_dir(tmp_path, "folder/ep1_0.rpa") == (
        unrpa_root(tmp_path) / "folder" / "ep1_0"
    )


def test_extract_target_dir_different_archives_isolated(tmp_path):
    """同父目录两个归档 → 各自专属目录，互不覆盖。"""
    unrpa = unrpa_root(tmp_path)
    assert extract_target_dir(tmp_path, "ep1.rpa") == unrpa / "ep1"
    assert extract_target_dir(tmp_path, "ep2.rpa") == unrpa / "ep2"


def test_extract_target_dir_multi_dot_name(tmp_path):
    assert extract_target_dir(tmp_path, "ep.1.0.rpa") == unrpa_root(tmp_path) / "ep.1.0"
