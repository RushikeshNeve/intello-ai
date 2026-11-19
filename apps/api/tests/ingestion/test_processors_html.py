"""HTML processor: junk removal, structure, no network, hostile inputs."""

from __future__ import annotations

import time

import pytest

from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError
from app.modules.ingestion.processors import Limits
from tests.ingestion.test_processors_support import HTML, block_network
from tests.ingestion.test_processors_support import run as run_blocked


def html(text: str, limits: Limits | None = None) -> ExtractedDocument:
    return run_blocked(text.encode(), HTML, limits)


def test_scripts_styles_and_friends_are_removed() -> None:
    doc = html(
        "<html><head><title>T</title><style>p{}</style><script>evil()</script></head><body>"
        "<p>keep</p><script>x()</script><style>y{}</style><noscript>ns</noscript>"
        "<template><p>tpl</p></template><iframe src='http://a.invalid'>frame</iframe>"
        "<object data='http://b.invalid'>obj</object><embed src='http://c.invalid'>"
        "<svg><text>svgtext</text></svg><canvas>cv</canvas><video>vid</video>"
        "<button>btn</button><input value='in'><textarea>ta</textarea>"
        "<select><option>opt</option></select><!-- a comment --></body></html>"
    )
    assert doc.stream() == "keep"


def test_hidden_elements_are_removed() -> None:
    doc = html(
        "<p>shown</p><div hidden>h1</div><div aria-hidden='true'>h2</div>"
        "<span style='color:red; display : none'>h3</span>"
        "<p style='VISIBILITY:hidden'>h4</p><p aria-hidden='false'>visible two</p>"
    )
    assert doc.stream() == "shown\n\nvisible two"


def test_structure_headings_lists_tables_pre_quote() -> None:
    doc = html(
        "<h1>Top</h1><p>intro</p><h2>Sub</h2><p>text<br>after break</p>"
        "<ul><li>a</li><li>b<ul><li>b1</li></ul></li></ul><ol><li>x</li><li>y</li></ol>"
        "<table><caption>Cap</caption><tr><th>H1</th><th>H2</th></tr>"
        "<tr><td>1</td><td>2 &amp; 3</td></tr><tr><td></td><td></td></tr></table>"
        "<pre>  code\n    indented</pre><blockquote>quote <b>text</b></blockquote>"
    )
    kinds = [(b.kind, b.text) for b in doc.blocks]
    assert kinds == [
        ("heading", "Top"),
        ("paragraph", "intro"),
        ("heading", "Sub"),
        ("paragraph", "text\nafter break"),
        ("list", "- a\n- b\n- b1"),
        ("list", "1. x\n2. y"),
        ("table", "Cap\nH1 | H2\n1 | 2 & 3"),
        ("code", "  code\n    indented"),
        ("quote", "quote text"),
    ]
    assert doc.blocks[3].locator.section_path == ["Top", "Sub"]
    assert all(b.locator.kind == "offset" for b in doc.blocks)


def test_inline_elements_do_not_split_paragraphs() -> None:
    doc = html("<p>one <b>two</b> <i>three</i> <a href='http://x.invalid'>four</a>.</p>")
    assert [b.text for b in doc.blocks] == ["one two three four."]


def test_entities_and_nbsp() -> None:
    doc = html("<p>Fish &amp; chips &lt;3&nbsp;&eacute;t&#233; &#x41; &bogus; &copy</p>")
    assert doc.blocks[0].text.startswith("Fish & chips <3 été A")


def test_unclosed_and_misnested_tags_do_not_crash() -> None:
    doc = html("<p>one<p>two<div>three<b>bold<i>x</b>y</i></div><li>stray item<td>stray cell")
    assert "one" in doc.stream()
    assert "three" in doc.stream()


def test_urls_in_attributes_are_never_fetched() -> None:
    page = (
        "<html><body><img src='http://tracker.invalid/p.gif'><link href='http://x.invalid/s.css'>"
        "<script src='http://y.invalid/a.js'></script><iframe src='http://z.invalid/'></iframe>"
        "<a href='http://w.invalid/'>link text</a><p style=\"background:url('http://v.invalid/b.png')\">"
        "styled</p><video src='http://u.invalid/v.mp4' poster='http://u.invalid/p.png'></video>"
        "<form action='http://t.invalid/'><input value='x'></form></body></html>"
    )
    with block_network() as attempts:
        doc = run_blocked(page.encode(), HTML)
    assert attempts == []
    assert doc.stream() == "link text\n\nstyled"
    assert "invalid" not in doc.stream()


def test_deep_nesting_10000_levels_is_handled_iteratively() -> None:
    depth = 10_000
    doc = html("<div>" * depth + "deep text" + "</div>" * depth)
    assert doc.stream() == "deep text"
    doc2 = html("<b>" * depth + "unclosed inline text")
    assert "unclosed inline text" in doc2.stream()
    nested_lists = html("<ul><li>" * 3000 + "leaf" + "</li></ul>" * 3000)
    assert "leaf" in nested_lists.stream()


def test_huge_single_line_and_many_blocks() -> None:
    doc = html("<p>" + "word " * 500_000 + "</p>")
    assert len(doc.blocks) == 1
    many = html("".join(f"<p>paragraph {i}</p>" for i in range(20_000)))
    assert len(many.blocks) == 20_000


def test_element_count_limit_is_too_large() -> None:
    with pytest.raises(ProcessingError) as info:
        html("<b>x</b>" * 11, Limits(max_html_elements=10))
    assert info.value.code == "too_large"


def test_charset_from_meta_and_bom() -> None:
    data = (
        b"<html><head><meta charset='windows-1252'></head>"
        b"<body><p>caf\xe9 cr\xe8me</p></body></html>"
    )
    doc = run_blocked(data, HTML)
    assert doc.stream() == "café crème"
    bom = b"\xef\xbb\xbf<p>caf\xc3\xa9</p>"
    assert run_blocked(bom, HTML).stream() == "café"


def test_only_junk_or_empty_is_no_text() -> None:
    for page in (
        "",
        "<html><head><title>x</title></head><body><script>1</script></body></html>",
        "<p> </p>",
        "<!-- c -->",
    ):
        with pytest.raises(ProcessingError) as info:
            html(page)
        assert info.value.code == "no_text_extracted"


def test_offsets_follow_the_stream_for_every_block() -> None:
    doc = html("<h1>T</h1><p>a</p><ul><li>b</li></ul><pre>c</pre>")
    stream = doc.stream()
    for block in doc.blocks:
        assert stream[block.locator.char_start : block.locator.char_end] == block.text


def test_parsing_is_roughly_linear() -> None:
    small = "<p>some paragraph text here</p>" * 2_000
    large = small * 8
    t0 = time.perf_counter()
    html(small)
    t1 = time.perf_counter()
    html(large)
    t2 = time.perf_counter()
    assert (t2 - t1) < max(10 * (t1 - t0), 0.5) * 3
