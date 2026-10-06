"""Tests for :mod:`kodo.websearch.htmlextract` — the curl backend's static extraction.

Covers the wall detector and the HTML-to-Markdown walk behind
``content_filter: "text"`` (inline links/breaks, headings, tables, nested
lists, block content inside list items, code blocks, quotes and rules) using
inline HTML fixtures only.
"""

from __future__ import annotations

import pytest

from kodo.websearch import htmlextract

_BASE = "https://example.com/docs/"


def _md(body: str) -> str:
    """Markdown for an ``<article>`` wrapping *body*."""
    _, markdown = htmlextract.extract_text(
        f"<html><body><article>{body}</article></body></html>", _BASE
    )
    return markdown


# ---------------------------------------------------------------------------
# is_blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<iframe src="https://www.google.com/recaptcha/api2"></iframe>',
        '<div id="cf-challenge-running"></div>',
        '<div class="px-captcha-container"></div>',
    ],
)
def test_is_blocked_detects_wall_markup(html: str) -> None:
    assert htmlextract.is_blocked(f"<html><body>{html}</body></html>")


def test_is_blocked_detects_wall_title() -> None:
    html = "<html><head><title>Just a moment...</title></head><body></body></html>"
    assert htmlextract.is_blocked(html)


def test_is_blocked_ignores_ordinary_page() -> None:
    assert not htmlextract.is_blocked("<html><head><title>Docs</title></head><body>Hi</body>")


# ---------------------------------------------------------------------------
# extract_text — content root and title
# ---------------------------------------------------------------------------


def test_extract_text_prefers_main_when_no_article() -> None:
    html = "<html><body><div>Outside</div><main><p>Inside main</p></main></body></html>"
    title, markdown = htmlextract.extract_text(html, _BASE)
    assert title == ""
    assert markdown == "Inside main"


def test_extract_text_falls_back_to_role_main_then_body() -> None:
    role_main = '<html><body><p>Chrome</p><div role="main"><p>Role main</p></div></body></html>'
    assert htmlextract.extract_text(role_main, _BASE)[1] == "Role main"
    assert htmlextract.extract_text("<p>Just body</p>", _BASE)[1] == "Just body"


# ---------------------------------------------------------------------------
# Inline content
# ---------------------------------------------------------------------------


def test_inline_links_resolved_and_non_navigational_links_flattened() -> None:
    markdown = _md(
        "<p>"
        '<a href="guide.html">relative</a> '
        '<a href="javascript:void(0)">script</a> '
        '<a href="#section">anchor</a> '
        '<a href="https://x.com/"></a>'
        "<a>no href</a>"
        "</p>"
    )
    assert markdown == "[relative](https://example.com/docs/guide.html) script anchor no href"


def test_inline_breaks_and_nested_inline_elements() -> None:
    markdown = _md(
        "<p>line one<br>line two <strong>bold <em>and <a href='/x'>linked</a></em></strong></p>"
    )
    assert markdown == "line one line two bold and [linked](https://example.com/x)"


def test_loose_inline_content_between_blocks_is_its_own_paragraph() -> None:
    markdown = _md("<span>loose <b>text</b></span><p>para</p>trailing")
    assert markdown == "loose text\n\npara\n\ntrailing"


# ---------------------------------------------------------------------------
# Block content
# ---------------------------------------------------------------------------


def test_headings_render_with_level_and_empty_heading_is_dropped() -> None:
    assert _md("<h2>Sub <code>title</code></h2><h3>  </h3><h6>Deep</h6>") == (
        "## Sub title\n\n###### Deep"
    )


def test_pre_blockquote_and_hr() -> None:
    markdown = _md(
        "<pre>  def f():\n      return 1  </pre>"
        "<pre>   </pre>"
        "<blockquote>Quoted <i>text</i></blockquote>"
        "<blockquote> </blockquote>"
        "<hr>"
    )
    assert markdown == "```\ndef f():\n      return 1\n```\n\n> Quoted text\n\n---"


def test_table_pads_short_rows_and_escapes_pipes() -> None:
    markdown = _md(
        "<table><thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>"
        "<tbody><tr><td>a|b</td></tr><tr><td>1</td><td>2</td><td>3</td></tr></tbody></table>"
    )
    assert markdown == "| A | B | C |\n| --- | --- | --- |\n| a\\|b |  |  |\n| 1 | 2 | 3 |"


def test_empty_tables_render_nothing() -> None:
    assert _md("<table></table><table><tr></tr></table><p>after</p>") == "after"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


def test_nested_lists_are_indented() -> None:
    markdown = _md("<ul><li>top<ul><li>child<ol><li>grandchild</li></ol></li></ul></li></ul>")
    assert markdown == "- top\n\n  - child\n\n    - grandchild"


def test_list_skips_non_li_children_and_empty_items() -> None:
    markdown = _md("<ul><div>stray</div><li>  </li><li>kept</li><li><ul></ul></li></ul>")
    assert markdown == "- kept"


def test_list_item_with_block_children() -> None:
    markdown = _md(
        "<ol>"
        "<li>code:<pre>x = 1</pre></li>"
        "<li><table><tr><td>cell</td></tr></table></li>"
        "<li>quote <blockquote>q</blockquote><pre> </pre></li>"
        "</ol>"
    )
    assert markdown == "- code:\n\n```\nx = 1\n```\n\n-\n\n| cell |\n| --- |\n\n- quote\n\n> q"


def test_list_item_wrapper_containing_blocks_is_walked() -> None:
    markdown = _md(
        "<ul><li>intro <div>wrapped <pre>code</pre></div><div><blockquote></blockquote></div>"
        "</li></ul>"
    )
    assert markdown == "- intro\n\nwrapped\n\n```\ncode\n```"


def test_flat_list_items_with_inline_wrappers() -> None:
    markdown = _md("<ul><li><span>plain <b>bold</b></span></li><li>two</li></ul>")
    assert markdown == "- plain bold\n- two"
