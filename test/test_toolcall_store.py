"""Rendering tests for kodo.state._toolcall_store.

The tool-call documents under ``toolcalls/<tool_use_id>.md`` are opened with
VS Code's ``markdown.showPreview`` (a CommonMark renderer), so what matters is
not that the Markdown *contains* the value but that a CommonMark parser puts
the whole value inside one code block. These tests therefore scan the rendered
document with the real fence rule rather than matching substrings.
"""

import re
from typing import NamedTuple

from kodo.state import json_to_markdown, render_tool_call_markdown

# A field value that is itself Markdown carrying its own fenced code block —
# the shape that broke rendering in the wild (a planner's ``codebase_context``).
NESTED = """\
## Target layout

```
src/kodo_snake/__init__.py
src/kodo_snake/game.py
```

`pyproject.toml` must make it importable."""


class Block(NamedTuple):
    """One fenced code block found by :func:`fenced_blocks`."""

    fence: str
    content: str


_OPENER = re.compile(r"^ {0,3}(`{3,})\s*$")


def fenced_blocks(doc: str) -> list[Block]:
    """Extract fenced code blocks from `doc` the way CommonMark would.

    Deliberately a hand-rolled scanner rather than a Markdown library: it keeps
    the test dependency-free, and the one rule under test — a fenced block ends
    at the first line whose backtick run is *at least as long* as the opener —
    is exactly the rule spelled out here, so a regression cannot hide behind a
    parser's leniency.
    """
    blocks: list[Block] = []
    lines = doc.split("\n")
    i = 0
    while i < len(lines):
        opener = _OPENER.match(lines[i])
        if opener is None:
            i += 1
            continue
        fence = opener.group(1)
        body: list[str] = []
        i += 1
        while i < len(lines):
            closer = _OPENER.match(lines[i])
            if closer is not None and len(closer.group(1)) >= len(fence):
                break
            body.append(lines[i])
            i += 1
        blocks.append(Block(fence, "\n".join(body)))
        i += 1
    return blocks


# ---------------------------------------------------------------------------
# A value that is itself fenced Markdown
# ---------------------------------------------------------------------------


def test_nested_fence_stays_in_one_block() -> None:
    blocks = fenced_blocks(json_to_markdown({"codebase_context": NESTED}, level=4))
    assert len(blocks) == 1
    assert blocks[0].content == NESTED


def test_nested_fence_widens_the_opener() -> None:
    blocks = fenced_blocks(json_to_markdown({"codebase_context": NESTED}, level=4))
    assert blocks[0].fence == "````"


def test_fence_widens_past_the_longest_inner_run() -> None:
    text = "a\n`````\nb\n`````\nc"
    blocks = fenced_blocks(json_to_markdown({"k": text}, level=4))
    assert len(blocks) == 1
    assert blocks[0].fence == "``````"
    assert blocks[0].content == text


def test_value_without_backticks_keeps_a_three_backtick_fence() -> None:
    text = "line one\nline two"
    blocks = fenced_blocks(json_to_markdown({"k": text}, level=4))
    assert [(b.fence, b.content) for b in blocks] == [("```", text)]


def test_content_after_a_nested_fence_value_is_not_swallowed() -> None:
    doc = json_to_markdown({"codebase_context": NESTED, "after": "tail"}, level=4)
    assert doc.split("\n")[-1] == "tail"
    assert len(fenced_blocks(doc)) == 1


# ---------------------------------------------------------------------------
# Multi-line elements of an all-scalar list
# ---------------------------------------------------------------------------


def _undent_bullet(doc: str) -> str:
    """Strip the ``- ``/``  `` list indentation off the first bullet in `doc`.

    What a CommonMark renderer hands the code block is the bullet's content
    with the item's indentation removed, so undoing it here lets the fence
    scanner above check the element exactly as it will be displayed.
    """
    lines = doc.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("- "))
    out = [lines[start][2:]]
    for ln in lines[start + 1 :]:
        if ln.startswith("  "):
            out.append(ln[2:])
        elif ln == "":
            out.append("")
        else:
            break
    return "\n".join(out)


def test_multiline_list_element_is_indented_into_its_bullet() -> None:
    doc = json_to_markdown({"notes": ["short", NESTED]}, level=4)
    bullets = [ln for ln in doc.split("\n") if ln.startswith("- ")]
    assert bullets == ["- short", "- ````"]
    # Every continuation line of the fenced element belongs to the bullet.
    tail = doc.split("- ````\n", 1)[1].split("\n")
    assert all(ln == "" or ln.startswith("  ") for ln in tail), tail


def test_multiline_list_element_round_trips() -> None:
    doc = json_to_markdown({"notes": [NESTED]}, level=4)
    blocks = fenced_blocks(_undent_bullet(doc))
    assert len(blocks) == 1
    assert blocks[0].content == NESTED


def test_all_scalar_list_of_short_strings_is_unchanged() -> None:
    doc = json_to_markdown({"notes": ["a", "b"]}, level=4)
    assert doc.endswith("- a\n- b")


# ---------------------------------------------------------------------------
# Whole document
# ---------------------------------------------------------------------------


def test_document_headings_survive_a_nested_fence_value() -> None:
    doc = render_tool_call_markdown(
        name="return_result",
        external_name="Return Result",
        user_description="Return the sub-agent's result",
        security_label="None",
        compliant=True,
        tool_input={"result": {"codebase_context": NESTED, "plan_warranted": True}},
        output={"ok": True},
    )
    # "## Output" must be a real heading, i.e. outside every code block.
    assert all("## Output" not in b.content for b in fenced_blocks(doc))
    assert "\n## Output\n" in doc
