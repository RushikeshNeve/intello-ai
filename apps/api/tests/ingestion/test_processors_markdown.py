"""Markdown processor: sections, code fences, lists, quotes, tables, front matter."""

from __future__ import annotations

import pytest

from app.modules.ingestion.contracts import Block, ExtractedDocument, ProcessingError
from tests.ingestion.test_processors_support import MARKDOWN
from tests.ingestion.test_processors_support import run as run_blocked


def md(text: str) -> ExtractedDocument:
    return run_blocked(text.encode(), MARKDOWN)


def by_kind(doc: ExtractedDocument, kind: str) -> list[Block]:
    return [b for b in doc.blocks if b.kind == kind]


def test_atx_headings_build_section_paths() -> None:
    doc = md("# A\n\ntext a\n\n## B\n\ntext b\n\n### C\n\ntext c\n\n## D\n\ntext d\n")
    assert [(b.kind, b.level) for b in doc.blocks if b.kind == "heading"] == [
        ("heading", 1),
        ("heading", 2),
        ("heading", 3),
        ("heading", 2),
    ]
    paths = {b.text: b.locator.section_path for b in doc.blocks}
    assert paths["A"] == ["A"]
    assert paths["text b"] == ["A", "B"]
    assert paths["text c"] == ["A", "B", "C"]
    assert paths["D"] == ["A", "D"]  # a sibling replaces B and C
    assert paths["text d"] == ["A", "D"]


def test_closing_hashes_and_indentation_rules_for_atx() -> None:
    doc = md("## Title ##\n\n    # indented four spaces is not a heading\n\n#NoSpace\n")
    assert doc.blocks[0].text == "Title"
    assert [b.kind for b in doc.blocks] == ["heading", "paragraph", "paragraph"]


def test_setext_headings() -> None:
    doc = md("Big title\n=========\n\nbody one\n\nSmall title\n-----------\n\nbody two\n")
    headings = by_kind(doc, "heading")
    assert [(h.text, h.level) for h in headings] == [("Big title", 1), ("Small title", 2)]
    assert doc.blocks[-1].locator.section_path == ["Big title", "Small title"]
    assert headings[0].locator.line_start == 1
    assert headings[0].locator.line_end == 2


def test_thematic_break_is_not_a_heading_and_is_dropped() -> None:
    doc = md("before\n\n---\n\nafter\n\n***\n\nlast\n")
    assert [b.text for b in doc.blocks] == ["before", "after", "last"]


def test_fenced_code_is_kept_intact_and_not_parsed() -> None:
    src = "intro\n\n```python\n# not a heading\n- not a list\n\n    indented()\n```\n\nafter\n"
    doc = md(src)
    (code,) = by_kind(doc, "code")
    assert code.text == "# not a heading\n- not a list\n\n    indented()"
    assert not by_kind(doc, "heading")
    assert (code.locator.line_start, code.locator.line_end) == (3, 8)
    assert [b.text for b in doc.blocks][-1] == "after"


def test_tilde_fence_longer_fence_and_unclosed_fence() -> None:
    doc = md("~~~\nlen 3 ~~~ inside\n~~~\n\n````\n```\nnested fence text\n```\n````\n")
    codes = by_kind(doc, "code")
    assert [c.text for c in codes] == ["len 3 ~~~ inside", "```\nnested fence text\n```"]
    unclosed = md("```\ncode runs to the end\n\nstill code")
    assert unclosed.blocks[0].kind == "code"
    assert unclosed.blocks[0].text.endswith("still code")


def test_code_block_whitespace_only_is_dropped() -> None:
    doc = md("text\n\n```\n   \n```\n")
    assert [b.kind for b in doc.blocks] == ["paragraph"]


def test_lists_keep_items_together_with_nesting_and_blank_lines() -> None:
    doc = md(
        "- one\n- two\n  - nested\n\n- three after blank\n\nparagraph\n\n1. first\n2. second\n"
    )
    lists = by_kind(doc, "list")
    assert lists[0].text == "- one\n- two\n- nested\n- three after blank"
    assert lists[1].text == "1. first\n2. second"
    assert (lists[0].locator.line_start, lists[0].locator.line_end) == (1, 5)


def test_block_quotes() -> None:
    doc = md("> quoted line one\n> quoted line two\n>\n> > nested\n\nafter\n")
    (quote,) = by_kind(doc, "quote")
    assert "quoted line one" in quote.text
    assert "nested" in quote.text
    assert ">" not in quote.text


def test_tables_become_pipe_joined_rows() -> None:
    doc = md("| A | B |\n|---|:-:|\n| 1 | 2 |\n| 3 | 4 |\n\nafter\n")
    (table,) = by_kind(doc, "table")
    assert table.text == "A | B\n1 | 2\n3 | 4"


def test_front_matter_is_ignored_but_line_numbers_still_count_it() -> None:
    doc = md("---\ntitle: Secret front matter\n---\n\n# Real\n\nbody\n")
    assert "Secret" not in doc.stream()
    assert doc.blocks[0].text == "Real"
    assert doc.blocks[0].locator.line_start == 5


def test_unclosed_front_matter_marker_is_just_a_rule() -> None:
    doc = md("---\nnot front matter, no closing marker\n")
    assert "not front matter" in doc.stream()


def test_inline_markup_is_reduced_and_urls_are_dropped() -> None:
    doc = md(
        'See [the docs](http://evil.invalid/x "t") and ![alt text](http://evil.invalid/i.png), '
        "<b>bold</b> <https://evil.invalid/auto> and [ref][1].\n\n[1]: http://evil.invalid/ref\n"
    )
    text = doc.stream()
    assert "the docs" in text
    assert "alt text" in text
    assert "evil.invalid/x" not in text
    assert "<b>" not in text
    assert "[1]: " not in text  # link reference definition dropped


def test_html_comments_are_removed_even_across_lines() -> None:
    doc = md(
        "before\n\n<!-- one line comment -->\n\n<!--\nmulti\nline\n-->\n\n"
        "after inline <!-- c --> x\n"
    )
    assert "comment" not in doc.stream()
    assert "multi" not in doc.stream()
    assert "after inline  x" not in doc.stream()  # whitespace collapsed by normalization
    assert doc.blocks[-1].text == "after inline x"


def test_raw_html_is_text_never_executed() -> None:
    doc = md("<script>alert(1)</script>\n\nvisible\n")
    assert "<script>" not in doc.stream()
    assert "visible" in doc.stream()


def test_entities_are_decoded() -> None:
    assert md("Fish &amp; chips &copy;\n").blocks[0].text == "Fish & chips ©".replace(" ", " ")


def test_offsets_follow_the_stream_for_every_block() -> None:
    doc = md("# T\n\npara\n\n- a\n- b\n\n```\ncode\n```\n")
    stream = doc.stream()
    for block in doc.blocks:
        assert stream[block.locator.char_start : block.locator.char_end] == block.text


def test_empty_or_only_front_matter_is_no_text() -> None:
    for src in ("", "   \n", "---\ntitle: x\n---\n", "<!-- only a comment -->\n", "***\n"):
        with pytest.raises(ProcessingError) as info:
            md(src)
        assert info.value.code == "no_text_extracted"


def test_pathological_markdown_stays_fast_and_correct() -> None:
    nasty = (
        "[" * 50_000
        + "\n\n"
        + "<" * 50_000
        + "\n\n"
        + "# "
        + " " * 50_000
        + "x\n\n"
        + "![](" * 20_000
        + "\n\n"
        + "> " * 20_000
    )
    doc = md(nasty)  # must terminate quickly (linear regexes)
    assert doc.blocks
