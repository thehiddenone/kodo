"""Behavior tests for kodo.llms._sanitize.strip_kodo_callouts."""

from __future__ import annotations

from kodo.llms._sanitize import strip_kodo_callouts


def test_strip_kodo_callouts_no_tags_returns_unchanged() -> None:
    """
    Given text with no callout tags,
    when strip_kodo_callouts is called,
    then the text is returned unchanged.
    """
    assert strip_kodo_callouts("plain text, nothing special") == "plain text, nothing special"


def test_strip_kodo_callouts_removes_info_tag_and_content() -> None:
    """
    Given text containing a <kodo_info> callout,
    when strip_kodo_callouts is called,
    then the tag and its content are removed entirely.
    """
    text = "before <kodo_info>progress note</kodo_info> after"
    assert strip_kodo_callouts(text) == "before  after"


def test_strip_kodo_callouts_removes_all_four_variants() -> None:
    """
    Given text containing one of each of the four callout tags,
    when strip_kodo_callouts is called,
    then every one of them is removed.
    """
    text = (
        "<kodo_info>info</kodo_info>"
        "<kodo_warn>warn</kodo_warn>"
        "<kodo_crit>crit</kodo_crit>"
        "<kodo>good</kodo>"
    )
    assert strip_kodo_callouts(text) == ""


def test_strip_kodo_callouts_handles_multiline_content() -> None:
    """
    Given a callout whose content spans multiple lines,
    when strip_kodo_callouts is called,
    then the whole block — including the embedded newline — is removed.
    """
    text = "x<kodo_warn>line one\nline two</kodo_warn>y"
    assert strip_kodo_callouts(text) == "xy"


def test_strip_kodo_callouts_does_not_confuse_kodo_with_kodo_info() -> None:
    """
    Given a bare <kodo> tag followed later by an unrelated <kodo_info> tag,
    when strip_kodo_callouts is called,
    then each is matched against its own closing tag, not the other's.
    """
    text = "<kodo>good news</kodo> and <kodo_info>fyi</kodo_info>"
    assert strip_kodo_callouts(text) == " and "
