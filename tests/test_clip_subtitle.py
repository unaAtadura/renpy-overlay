"""ASS 字幕读写与字幕↔切片映射单测（离线）。"""

import pytest

from renpy_overlay.video.clip.editing.subtitle import (
    ASS_HEADER,
    ASS_STYLE_NAME,
    SubtitleEntry,
    build_ass,
    build_ass_dialogue,
    entries_to_segments,
    escape_ass_text,
    format_ass_time,
    parse_ass,
    parse_ass_time,
)


class TestAssTime:
    def test_format(self):
        assert format_ass_time(0) == "0:00:00.00"
        assert format_ass_time(65_430) == "0:01:05.43"
        assert format_ass_time(3_661_000) == "1:01:01.00"
        assert format_ass_time(3_600_000) == "1:00:00.00"

    def test_format_hour_no_pad_multidigit(self):
        assert format_ass_time(36_000_000) == "10:00:00.00"

    def test_format_centisecond_truncates(self):
        assert format_ass_time(65_439) == "0:01:05.43"

    def test_format_negative_clamps(self):
        assert format_ass_time(-5) == "0:00:00.00"

    def test_parse(self):
        assert parse_ass_time("0:00:00.00") == 0
        assert parse_ass_time("0:01:05.43") == 65_430
        assert parse_ass_time("1:01:01.00") == 3_661_000
        assert parse_ass_time("10:00:00.00") == 36_000_000
        assert parse_ass_time("0:00:00,50") == 500  # 逗号分隔厘秒容错

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            None,
            "12:34",
            "0:00:00",
            "aa:bb:cc.00",
            "0:00:00.abc",
            "0:00:00.5",  # 厘秒须两位
            "0:00:00.500",  # 毫秒三位非法（ASS 恒厘秒）
            "-1:00:00.00",
        ],
    )
    def test_parse_invalid_returns_none(self, bad):
        assert parse_ass_time(bad) is None


class TestAssHeader:
    def test_required_sections(self):
        assert "[Script Info]" in ASS_HEADER
        assert "[V4+ Styles]" in ASS_HEADER
        assert "[Events]" in ASS_HEADER

    def test_script_info_fields(self):
        assert "ScriptType: v4.00+" in ASS_HEADER
        assert "PlayResX: 1920" in ASS_HEADER
        assert "PlayResY: 1080" in ASS_HEADER

    def test_style_line_default(self):
        style_lines = [
            ln
            for ln in ASS_HEADER.splitlines()
            if ln.startswith(f"Style: {ASS_STYLE_NAME},")
        ]
        assert len(style_lines) == 1
        fields = style_lines[0].split(": ", 1)[1].split(",")
        assert len(fields) == 23  # V4+ Style 标准字段数
        assert fields[1] == "Microsoft YaHei"  # 字体（中文渲染兜底）
        assert fields[22] == "1"  # Encoding=default charset

    def test_events_format_line(self):
        format_lines = [
            ln for ln in ASS_HEADER.splitlines() if ln.startswith("Format:")
        ]
        events_format = format_lines[-1]
        names = [f.strip() for f in events_format.split(":", 1)[1].split(",")]
        assert names[:3] == ["Layer", "Start", "End"]
        assert names[-1] == "Text"

    def test_header_ends_with_newline(self):
        assert ASS_HEADER.endswith("\n")


class TestBuildAssDialogue:
    def test_bilingual_uses_hard_newline(self):
        line = build_ass_dialogue(SubtitleEntry(1_000, 3_000, "你好世界", "Hello world"))
        assert line == (
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
            "你好世界\\NHello world\n"
        )

    def test_single_language_when_src_empty(self):
        line = build_ass_dialogue(SubtitleEntry(0, 500, "只有中文", ""))
        assert line == (
            "Dialogue: 0,0:00:00.00,0:00:00.50,Default,,0,0,0,,只有中文\n"
        )
        assert "\\N" not in line

    def test_field_count_keeps_text_commas(self):
        line = build_ass_dialogue(SubtitleEntry(0, 500, "a,b", "c"))
        body = line.removeprefix("Dialogue: ").rstrip("\n")
        assert len(body.split(",", 9)) == 10  # Text 段含逗号不被切断

    def test_special_chars_escaped(self):
        line = build_ass_dialogue(SubtitleEntry(0, 500, "他\\说{了}", "x"))
        assert "他\\\\说｛了｝" in line  # 反斜杠与花括号转义，渲染器不吞字


class TestEscapeText:
    def test_newline_variants(self):
        assert escape_ass_text("a\r\nb") == "a\\Nb"
        assert escape_ass_text("a\nb") == "a\\Nb"

    def test_roundtrip(self):
        raw = "行1\n行\\2{3}4"
        from renpy_overlay.video.clip.editing.subtitle import _unescape_ass_text

        assert _unescape_ass_text(escape_ass_text(raw)) == raw


class TestBuildAss:
    def test_header_then_dialogues_in_order(self):
        entries = [
            SubtitleEntry(1_000, 3_000, "你好", "Hello"),
            SubtitleEntry(4_000, 6_000, "再见", "Bye"),
        ]
        text = build_ass(entries)
        assert text.startswith(ASS_HEADER)
        dialogues = [ln for ln in text.splitlines() if ln.startswith("Dialogue:")]
        assert dialogues == [
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,你好\\NHello",
            "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,再见\\NBye",
        ]

    def test_empty_entries_header_only(self):
        assert build_ass([]) == ASS_HEADER


class TestParseAss:
    def test_roundtrip_centisecond_precision(self):
        entries = [
            SubtitleEntry(1_000, 3_000, "你好", "Hello"),
            SubtitleEntry(4_000, 6_500, "再见", "Bye"),
            SubtitleEntry(0, 500, "只有中文", ""),
        ]
        assert parse_ass(build_ass(entries)) == entries

    def test_ignores_header_and_style_lines(self):
        parsed = parse_ass(ASS_HEADER)
        assert parsed == []

    def test_dialogue_only_minimal(self):
        parsed = parse_ass(
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,中文\\N原文\n"
        )
        assert len(parsed) == 1
        assert (parsed[0].start_ms, parsed[0].end_ms) == (1_000, 3_000)
        assert (parsed[0].text_zh, parsed[0].text_src) == ("中文", "原文")

    def test_text_field_keeps_commas(self):
        parsed = parse_ass(
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,好,好吗\n"
        )
        assert parsed[0].text_zh == "好,好吗"

    def test_skips_invalid_time_lines(self):
        parsed = parse_ass(
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,好\\Nok\n"
            "Dialogue: 0,垃圾,不合法,Default,,0,0,0,,坏块\n"
        )
        assert len(parsed) == 1
        assert parsed[0].text_zh == "好"

    def test_custom_format_line_order(self):
        parsed = parse_ass(
            "[Events]\n"
            "Format: Marked, Start, End, Style, Name, MarginL, MarginR,"
            " MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,中\\Nen\n"
        )
        assert len(parsed) == 1
        assert (parsed[0].start_ms, parsed[0].text_src) == (1_000, "en")

    def test_style_format_line_not_confused_with_events(self):
        # Style 段 Format 行（无 Text 字段）不得覆盖 Events Format 映射
        parsed = parse_ass(
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
            " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
            " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
            " Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "[Events]\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,中\n"
        )
        assert len(parsed) == 1
        assert parsed[0].text_zh == "中"

    def test_case_insensitive_keywords(self):
        parsed = parse_ass("dialogue: 0,0:00:01.00,0:00:02.00,default,,0,0,0,,中\n")
        assert len(parsed) == 1

    def test_crlf_and_blank_lines(self):
        parsed = parse_ass(
            "[Events]\r\n\r\n"
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,中\\Nen\r\n"
        )
        assert len(parsed) == 1
        assert parsed[0].text_zh == "中"
        assert parsed[0].text_src == "en"

    def test_empty(self):
        assert parse_ass("") == []
        assert parse_ass(None) == []


class TestEntriesToSegments:
    def test_maps_sorted_with_origin(self):
        from renpy_overlay.video.clip.editing.interfaces import ClipSegment

        entries = [
            SubtitleEntry(4_000, 5_000, "b", "b"),
            SubtitleEntry(1_000, 2_000, "a", "a"),
        ]
        segs = entries_to_segments(entries)
        assert segs == [
            ClipSegment(1_000, 2_000, origin="subtitle"),
            ClipSegment(4_000, 5_000, origin="subtitle"),
        ]
