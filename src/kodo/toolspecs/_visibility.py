"""Project tool input/output to the customer-visible rows shown in the WebView.

Each :class:`~kodo.toolspecs.ToolSpec` declares ``input_visibility`` and
``output_visibility`` maps (property name → ``always`` / ``visible`` /
``hidden``). A property absent from a map defaults to ``hidden``. The WebView's
tool-call detail box renders the resulting rows: ``always`` values in full,
``visible`` values cropped (client-side) to 3 lines / 200 characters, and
``hidden`` values omitted entirely.
"""

from __future__ import annotations

import json

from ._spec import (
    OUTPUT_VISIBILITY_DEFAULT,
    VISIBILITY_HIDDEN,
    ToolSpec,
)

__all__ = ["build_detail_rows", "stringify_value"]


def stringify_value(value: object) -> str:
    """Render a property value as a single string for display.

    Strings pass through; everything else is pretty-printed as JSON (falling
    back to ``str`` for anything not JSON-serialisable).
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _rows_for(
    data: dict[str, object], visibility: dict[str, str], source: str
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name, value in data.items():
        vis = visibility.get(name, OUTPUT_VISIBILITY_DEFAULT)
        if vis == VISIBILITY_HIDDEN:
            continue
        rows.append(
            {
                "name": name,
                "value": stringify_value(value),
                "source": source,
                "visibility": vis,
            }
        )
    return rows


def build_detail_rows(
    spec: ToolSpec,
    tool_input: dict[str, object],
    output: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Build the customer-visible detail rows for one tool call.

    Args:
        spec: The tool's spec (carries the visibility maps).
        tool_input: The parsed tool input.
        output: The (normalized) tool output, or ``None`` if not yet known.

    Returns:
        list[dict[str, object]]: Ordered rows, each
        ``{name, value, source: 'input'|'output', visibility}``; ``hidden``
        properties are excluded. Input rows precede output rows.
    """
    rows = _rows_for(tool_input, spec.input_visibility, "input")
    if output is not None:
        rows.extend(_rows_for(output, spec.output_visibility, "output"))
    return rows
