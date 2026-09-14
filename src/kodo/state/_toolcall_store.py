"""Render a tool call's input + output to a user-facing Markdown document.

Each dispatched tool call is persisted as one Markdown file under the session's
``toolcalls/`` directory (``<tool_use_id>.md``). These files exist for the
*user* — the WebView's tool-call detail table links to them so a click opens the
full input and output in an editor. They are written with a generic
JSON→Markdown renderer (JSON field names become headings) rather than as raw
JSON, so they read cleanly.

The renderer is intentionally dumb and total: any JSON-shaped value renders to
*something* readable; it never raises on unexpected shapes.
"""

from __future__ import annotations

import re

__all__ = ["json_to_markdown", "render_tool_call_markdown"]

_MULTILINE_THRESHOLD = 120

_BACKTICK_RUN = re.compile(r"`+")


def _fence_for(text: str) -> str:
    """Return a backtick fence long enough to wrap `text` verbatim.

    Field values are routinely Markdown in their own right — a plan's
    ``codebase_context``, an agent's report, a file excerpt — and therefore
    contain their own ``` fences. CommonMark ends a fenced block at the first
    line whose backtick run is *at least as long* as the opener, so a fixed
    three-backtick fence is closed by the value's first inner fence and the
    remainder of the document renders as garbage (headings, bullets and the
    following fields all leak out, and the inner closing fence re-opens a
    block that swallows whatever comes next).

    Opening with one backtick more than the longest run anywhere in the text
    keeps the value in a single block with its content intact. Runs are
    counted anywhere, not just at line starts: that can pick a fence a
    backtick longer than strictly necessary, which is harmless, and it avoids
    having to reason about CommonMark's indentation rules for a closing fence.
    """
    longest = max((len(m.group(0)) for m in _BACKTICK_RUN.finditer(text)), default=0)
    return "`" * max(3, longest + 1)


def _scalar_to_markdown(value: object) -> str:
    """Render a scalar (str/number/bool/None) as inline or fenced Markdown."""
    if value is None:
        return "_(none)_"
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    text = str(value)
    if "\n" in text or len(text) > _MULTILINE_THRESHOLD:
        fence = _fence_for(text)
        return f"{fence}\n{text}\n{fence}"
    return text


def _bullet(value: object) -> str:
    """Render one element of an all-scalar list as a Markdown bullet.

    A multi-line element becomes a fenced block, and a fenced block only
    belongs to the list item if its continuation lines are indented past the
    ``- `` marker — otherwise the opener sits alone in the bullet and the
    content lines escape the list. So every line after the first is indented
    by two spaces (blank lines are left blank rather than padded, which keeps
    them blank inside the code block and still continues the item).
    """
    first, *rest = _scalar_to_markdown(value).split("\n")
    return "\n".join([f"- {first}", *(f"  {line}" if line else "" for line in rest)])


def _list_to_markdown(value: list[object], level: int, label: str | None) -> str:
    """Render a list whose elements include at least one dict/list, or is empty.

    Each element gets its own heading at `level` (the same depth its dict key,
    if any, would have used — this is the one place headings don't nest one
    level deeper than their container). The heading is ``{label}[{index}]``,
    0-based; `label` is the dict key this list was found under, or `None` for
    an unnamed list (top-level, or nested inside another list), which uses
    ``item`` instead.
    """
    if not value:
        return "_(empty list)_"
    if all(not isinstance(item, (dict, list)) for item in value):
        return "\n".join(_bullet(item) for item in value)
    heading = "#" * min(level, 6)
    effective_label = label if label is not None else "item"
    parts = [
        f"{heading} {effective_label}[{i}]\n\n{json_to_markdown(item, level + 1)}"
        for i, item in enumerate(value)
    ]
    return "\n\n".join(parts)


def json_to_markdown(value: object, level: int = 2) -> str:
    """Render an arbitrary JSON-shaped value as Markdown.

    Dict keys become headings (clamped to ``######``); a dict value that is a
    list of dicts/lists skips the key heading and instead gives each element
    its own ``key[index]`` heading at that same level (0-based); an all-scalar
    or empty list still gets the key heading, rendered as a bullet list or
    ``_(empty list)_`` beneath it. A list found outside any dict key (at the
    top level, or nested inside another list) uses ``item[index]`` instead of
    ``key[index]``. Scalars render inline or as a fenced block when
    multi-line/long.

    Args:
        value: The value to render (dict, list, or scalar).
        level: Current Markdown heading level for nested dict keys.

    Returns:
        str: Markdown text (no trailing newline guarantee).
    """
    heading = "#" * min(level, 6)
    if isinstance(value, dict):
        if not value:
            return "_(empty)_"
        parts: list[str] = []
        for key, val in value.items():
            if isinstance(val, list) and any(isinstance(item, (dict, list)) for item in val):
                parts.append(_list_to_markdown(val, level, key))
            elif isinstance(val, (dict, list)):
                parts.append(f"{heading} {key}\n\n{json_to_markdown(val, level + 1)}")
            else:
                parts.append(f"{heading} {key}\n\n{_scalar_to_markdown(val)}")
        return "\n\n".join(parts)
    if isinstance(value, list):
        return _list_to_markdown(value, level, None)
    return _scalar_to_markdown(value)


def render_tool_call_markdown(
    *,
    name: str,
    external_name: str,
    user_description: str,
    security_label: str,
    compliant: bool,
    tool_input: dict[str, object],
    output: dict[str, object],
) -> str:
    """Render a full tool-call document (header + Input + Output).

    Args:
        name: Internal tool name.
        external_name: Human-readable tool name.
        user_description: Short human-readable description.
        security_label: Friendly security-impact level name.
        compliant: Whether the output matched its declared schema.
        tool_input: The parsed tool input.
        output: The (normalized) tool output.

    Returns:
        str: The Markdown document.
    """
    compliance = "✅ compliant" if compliant else "⚠️ repaired (output did not match schema)"
    header = (
        f"# {external_name} (`{name}`)\n\n"
        f"{user_description}\n\n"
        f"- **Security impact:** {security_label}\n"
        f"- **Schema compliance:** {compliance}\n"
    )
    return (
        f"{header}\n"
        f"## Input\n\n{json_to_markdown(tool_input, level=3)}\n\n"
        f"## Output\n\n{json_to_markdown(output, level=3)}\n"
    )
