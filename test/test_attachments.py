"""Unit tests for kodo.runtime._attachments.

Covers the prompt-attachment control-tag parsing, the LLM-injection layout, and
the server-authoritative file validation (text-only, per-file/combined caps).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.runtime._attachments import (
    MAX_ATTACH_BYTES,
    AttachmentError,
    encode_attachment_marker,
    inject_attachments,
    load_attachment,
    parse_attachment_marker,
)

# ---------------------------------------------------------------------------
# parse_attachment_marker
# ---------------------------------------------------------------------------


def test_parse_no_marker_returns_text_unchanged() -> None:
    text, paths = parse_attachment_marker("just a normal prompt\nsecond line")
    assert text == "just a normal prompt\nsecond line"
    assert paths == []


def test_parse_extracts_paths_and_strips_marker() -> None:
    prompt = "do the thing"
    raw = encode_attachment_marker(["/abs/a.py", "/abs/b.md"]) + "\n" + prompt
    text, paths = parse_attachment_marker(raw)
    assert text == prompt
    assert paths == ["/abs/a.py", "/abs/b.md"]


def test_parse_marker_with_no_following_newline() -> None:
    raw = encode_attachment_marker(["/abs/a.py"])
    text, paths = parse_attachment_marker(raw)
    assert text == ""
    assert paths == ["/abs/a.py"]


def test_parse_empty_array_marker() -> None:
    raw = encode_attachment_marker([]) + "\nhi"
    text, paths = parse_attachment_marker(raw)
    assert text == "hi"
    assert paths == []


def test_parse_malformed_marker_is_treated_as_plain_text() -> None:
    raw = "<!--KODO_ATTACHMENTS:not json-->\nhi"
    text, paths = parse_attachment_marker(raw)
    assert text == raw  # untouched
    assert paths == []


def test_parse_marker_must_be_first_line() -> None:
    raw = "line one\n" + encode_attachment_marker(["/abs/a.py"])
    text, paths = parse_attachment_marker(raw)
    assert paths == []
    assert text == raw


def test_parse_tolerates_path_containing_marker_suffix() -> None:
    # A filename containing "-->" must still round-trip (greedy suffix match).
    raw = encode_attachment_marker(["/abs/weird-->name.py"]) + "\nprompt"
    text, paths = parse_attachment_marker(raw)
    assert paths == ["/abs/weird-->name.py"]
    assert text == "prompt"


# ---------------------------------------------------------------------------
# inject_attachments
# ---------------------------------------------------------------------------


def test_inject_no_items_returns_clean_text() -> None:
    assert inject_attachments("hello", []) == "hello"


def test_inject_appends_tags_after_prompt() -> None:
    out = inject_attachments("my prompt", [("id-a", "a.py"), ("id-b", "b.md")])
    assert out == (
        'my prompt\n\n<ATTACHMENT ID="id-a" filename="a.py"/>\n'
        '<ATTACHMENT ID="id-b" filename="b.md"/>'
    )


def test_inject_escapes_filename_xml_specials() -> None:
    out = inject_attachments("hi", [("id-a", 'weird "name" & <tag>.txt')])
    assert out == (
        'hi\n\n<ATTACHMENT ID="id-a" filename="weird &quot;name&quot; '
        '&amp; &lt;tag&gt;.txt"/>'
    )


# ---------------------------------------------------------------------------
# load_attachment
# ---------------------------------------------------------------------------


def test_load_reads_utf8_text(tmp_path: Path) -> None:
    p = tmp_path / "f.py"
    p.write_text("héllo", encoding="utf-8")
    loaded = load_attachment(str(p), running_total=0)
    assert loaded.name == "f.py"
    assert loaded.content == "héllo"
    assert loaded.size == len("héllo".encode())


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AttachmentError):
        load_attachment(str(tmp_path / "nope.txt"), running_total=0)


def test_load_binary_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "bin"
    p.write_bytes(b"abc\x00def")
    with pytest.raises(AttachmentError):
        load_attachment(str(p), running_total=0)


def test_load_invalid_utf8_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad"
    p.write_bytes(b"\xff\xfe\xfa")  # invalid UTF-8, no NUL
    with pytest.raises(AttachmentError):
        load_attachment(str(p), running_total=0)


def test_load_oversized_file_raises(tmp_path: Path) -> None:
    p = tmp_path / "big"
    p.write_text("x" * (MAX_ATTACH_BYTES + 1), encoding="utf-8")
    with pytest.raises(AttachmentError):
        load_attachment(str(p), running_total=0)


def test_load_combined_cap_raises(tmp_path: Path) -> None:
    p = tmp_path / "ok"
    p.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(AttachmentError):
        load_attachment(str(p), running_total=MAX_ATTACH_BYTES - 50)
