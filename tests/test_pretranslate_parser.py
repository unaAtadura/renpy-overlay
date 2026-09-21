"""脚本扫描与解析的离线验证：say/menu/translate 提取、tl 过滤与 .rpa 解包。

全部基于 tmp_path 构造的最小样例（.rpy 文本与手工组装的 RPA-3.0 归档），
不依赖真实游戏目录。
"""

from __future__ import annotations

import pickle
import zlib

import pytest

from renpy_overlay.pretranslate.parser import (
    RPA_SEPARATOR,
    collect_texts,
    extract_blocks,
    extract_from_rpy,
    scan_rpy_files,
    unpack_rpa_entry,
)

# ------------------------------------------------------------------ 行解析


def test_say_three_forms():
    script = (
        'e "Hello!"\n'
        '"Narrator line."\n'
        'e "With modifier." with dissolve\n'
        'narrator "Prefixed narrator."\n'
    )
    assert extract_from_rpy(script) == [
        "Hello!",
        "Narrator line.",
        "With modifier.",
        "Prefixed narrator.",
    ]


def test_say_escapes_and_triple_quotes():
    script = 'e "Line \\"quoted\\" and \\n newline"\n'
    assert extract_from_rpy(script) == ['Line "quoted" and \n newline']
    script3 = "e \"\"\"first\nsecond\"\"\"\n"
    assert extract_from_rpy(script3) == ["first\nsecond"]


def test_non_say_lines_skipped():
    script = (
        "# 注释 e \"not a say\"\n"
        "define e = Character(\"Eileen\")\n"
        "default points = 0\n"
        "image bg room = \"room.png\"\n"
        "show eileen happy\n"
        "play music \"song.ogg\"\n"
        "jump start_label\n"
        "$ x = \"not a say\"\n"
        "if points > 1:\n"
        '    e "Inside if still collected."\n'
    )
    assert extract_from_rpy(script) == ["Inside if still collected."]


def test_menu_group_collected_in_order():
    script = (
        "menu:\n"
        '    "Open the door":\n'
        "        jump door\n"
        '    "Leave" if has_key:\n'
        "        jump leave\n"
    )
    assert extract_blocks(script) == [("menu", ["Open the door", "Leave"])]


def test_menu_set_and_caption_lines_do_not_break_group():
    script = (
        "menu chapter_choices:\n"
        "    set chosen\n"
        '    "A":\n'
        "        pass\n"
        '    "B":\n'
        "        pass\n"
    )
    assert extract_from_rpy(script) == ["A", "B"]


def test_translate_strings_block_collects_new_only():
    script = (
        "translate english strings:\n"
        '    old "Hello"\n'
        '    new "Bonjour"\n'
        '    old "Bye"\n'
        '    new "Au revoir"\n'
    )
    assert extract_blocks(script) == [("tl_str", ["Bonjour"]), ("tl_str", ["Au revoir"])]


def test_translate_label_block_say_lines_are_new_text():
    script = (
        "translate english lab_a3b2:\n"
        '    # e "Hello"\n'
        '    e "Bonjour"\n'
    )
    assert extract_from_rpy(script) == ["Bonjour"]


def test_python_block_body_skipped():
    script = (
        "init python:\n"
        '    x = "not a say"\n'
        "    def helper():\n"
        '        return "nested"\n'
        'e "after python"\n'
    )
    assert extract_from_rpy(script) == ["after python"]


# ------------------------------------------------------------------ 扫描


def _make_game(tmp_path):
    game = tmp_path / "game"
    (game / "tl" / "english").mkdir(parents=True)
    (game / "tl" / "japanese").mkdir(parents=True)
    (game / "script.rpy").write_text('e "hi"\n', encoding="utf-8")
    (game / "tl" / "english" / "a.rpy").write_text('e "bon"\n', encoding="utf-8")
    (game / "tl" / "japanese" / "b.rpy").write_text('e "こんにちは"\n', encoding="utf-8")
    (game / "compiled.rpyc").write_bytes(b"\x00\x01")
    return game


def test_scan_lists_relative_paths_sorted(tmp_path):
    _make_game(tmp_path)
    assert scan_rpy_files(tmp_path) == [
        "game/script.rpy",
        "game/tl/english/a.rpy",
        "game/tl/japanese/b.rpy",
    ]


def test_scan_tl_lang_filter(tmp_path):
    _make_game(tmp_path)
    assert scan_rpy_files(tmp_path, tl_langs=["english"]) == [
        "game/script.rpy",
        "game/tl/english/a.rpy",
    ]
    assert scan_rpy_files(tmp_path, tl_langs=[]) == ["game/script.rpy"]


def test_scan_empty_dir(tmp_path):
    (tmp_path / "game").mkdir()
    assert scan_rpy_files(tmp_path) == []


# ------------------------------------------------------------------ .rpa 归档


def _make_rpa(path, entries: dict[str, bytes], key: int = 0xDEADBEEF):
    """组装最小 RPA-3.0：头行 + 0x40 处索引区（固定预留至 0x400）+ 数据区。"""
    index_start, data_start = 0x40, 0x400
    body = bytearray()
    index = {}
    for name, content in entries.items():
        offset = data_start + len(body)
        payload = zlib.compress(content)
        # 混淆形态（unRPA 语义）：首字节 N = 从头丢弃 N 字节（含首字节自身），
        # 此处 2 字节前缀后紧跟 zlib 流
        blob = b"\x02\x00" + payload
        body += blob
        index[name] = [(offset ^ key, len(blob))]
    header = f"RPA-3.0 {index_start:x} {key:x}\n".encode("ascii")
    padding = b"\x00" * (index_start - len(header))
    index_blob = zlib.compress(pickle.dumps(index))
    assert index_start + len(index_blob) <= data_start, "索引区超出预留"
    gap = b"\x00" * (data_start - index_start - len(index_blob))
    path.write_bytes(header + padding + index_blob + gap + bytes(body))


def test_rpa_roundtrip(tmp_path):
    archive = tmp_path / "data.rpa"
    _make_rpa(archive, {"script/a.rpy": b'e "from archive"\n'})
    assert unpack_rpa_entry(archive, "script/a.rpy") == b'e "from archive"\n'


def test_rpa_missing_entry_raises(tmp_path):
    archive = tmp_path / "data.rpa"
    _make_rpa(archive, {"a.rpy": b"x"})
    with pytest.raises(KeyError):
        unpack_rpa_entry(archive, "missing.rpy")


def test_scan_includes_rpa_entries(tmp_path):
    _make_game(tmp_path)
    archive = tmp_path / "game" / "data.rpa"
    _make_rpa(
        archive,
        {
            "script/deep.rpy": b'e "archived"\n',
            "tl/english/deep.rpy": b'e "archived tl"\n',
            "images/bg.png": b"\x89PNG",
        },
    )
    rels = scan_rpy_files(tmp_path)
    assert f"game/data.rpa{RPA_SEPARATOR}script/deep.rpy" in rels
    assert f"game/data.rpa{RPA_SEPARATOR}tl/english/deep.rpy" in rels
    assert not any("bg.png" in rel for rel in rels)
    filtered = scan_rpy_files(tmp_path, tl_langs=["japanese"])
    assert f"game/data.rpa{RPA_SEPARATOR}tl/english/deep.rpy" not in filtered


# ------------------------------------------------------------------ 收集与清洗


def test_collect_texts_cleans_dedupes_and_filters(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "a.rpy").write_text(
        'e "  {w}Hello  "\n'
        'e "{i}only tags{/i}"\n'
        '""\n'
        "menu:\n"
        '    " A ":\n'
        "        pass\n",
        encoding="utf-8",
    )
    (game / "broken.rpy").write_bytes(b"\xff\xfe\xfa")  # 非法内容也不致命
    texts = collect_texts(["game/a.rpy", "game/missing.rpy"], tmp_path)
    by_text = {item.text: item.kind for item in texts}
    # 清洗：去标签 + strip；纯标签与空串被过滤
    assert by_text == {"Hello": "say", "only tags": "say", "1. A": "menu"}
    assert all(item.source == "game/a.rpy" for item in texts)


def test_collect_texts_dedupes_across_files(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "a.rpy").write_text('e "Same"\n', encoding="utf-8")
    (game / "b.rpy").write_text('e "Same"\n', encoding="utf-8")
    texts = collect_texts(["game/a.rpy", "game/b.rpy"], tmp_path)
    assert len(texts) == 1
    assert texts[0].source == "game/a.rpy"  # 首个来源保留
