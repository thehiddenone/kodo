"""Tool-description rendering — what the model actually reads about a tool.

An LLM tool definition carries only three things: ``name``, ``description``, and
``input_schema`` (see :mod:`kodo.llms.anthropic._claude` and
:mod:`kodo.llms.llamacpp._llama`). There is no field for a tool's *output*
shape, so a tool's declared :attr:`~kodo.toolspecs.ToolSpec.output_schema` would
never reach the model at all unless it is folded into the description.

:func:`tool_description` does exactly that: it appends a **dense** rendering of
the output schema to the spec's prose description. Dense means
*description-only* — every property collapses to its ``description`` string,
dropping the ``"type"``/``"properties"``/``"required"`` scaffolding, which is
pure overhead for a reader who will see the concrete types in the actual result::

    {"type": "object",                              {"root": "The resolved absolute
     "properties": {                                        search root.",
       "root": {"type": "string",          ===>      "files": ["Matching paths,
                "description": "The ..."},                    relative to `root`."],
       "files": {"type": "array",                    "count": "Number of paths
                 "items": {"type": "string"},                 returned."}
                 "description": "Matching ..."},
       ...},
     "required": [...]}

Types are deliberately omitted — they are self-evident in the returned data.
Optionality is not, so what ``required`` would have said is preserved as a
one-line prose note listing the fields that may be absent.

The engine-owned ``schema_compliance`` field (see :mod:`._compliance`) is **not**
included: it is injected into every tool result identically, so repeating its
long explanation under all ~30 tools would cost far more than it teaches. Agents
that need it are told about it once, by their own ``## Your Task Contract``
section (rendered by :class:`~kodo.subagents._registry.AgentRegistry` from the
*augmented* schema).
"""

from __future__ import annotations

import json

from ._spec import ToolSpec

__all__ = ["dense_output_schema", "optional_output_paths", "tool_description"]

_RETURNS_PREFIX = "Returns: "
_OPTIONAL_PREFIX = "These fields may be absent from the result: "


def dense_output_schema(output_schema: dict[str, object]) -> str:
    """Render *output_schema* as a compact, description-only JSON sketch.

    Every declared property is replaced by its ``description`` string, so the
    result reads as an example-shaped object rather than a JSON Schema. Nesting
    is preserved: an object property recurses into a nested ``{...}``, and an
    array property becomes a one-element ``[...]`` holding either its item
    object's sketch or, for an array of scalars, the array's own description.

    A property with no ``description`` falls back to its parenthesized type
    (``"(string)"``), so a key never maps to an empty string.

    Args:
        output_schema: A spec's declared ``output_schema`` — an ``object``
            schema, or a ``{"oneOf": [...]}`` of them.

    Returns:
        str: A single-line JSON object literal. For a top-level ``oneOf``, the
        branches are rendered separately and joined with ``" | "``.
    """
    branches = output_schema.get("oneOf") or output_schema.get("anyOf")
    if isinstance(branches, list):
        rendered = [_dumps(_dense(b)) for b in branches if isinstance(b, dict)]
        if rendered:
            return " | ".join(rendered)
    return _dumps(_dense(output_schema))


def optional_output_paths(output_schema: dict[str, object]) -> tuple[str, ...]:
    """Return the paths of declared output properties that are not required.

    Nesting is expressed with dots for objects and ``[]`` for array items —
    ``"checkpoint_sha"``, ``"summary.detail"``, ``"matches[].line"`` — so a
    conditionally present nested field is still identifiable.

    Args:
        output_schema: A spec's declared ``output_schema``.

    Returns:
        tuple[str, ...]: Optional-property paths, in declaration order.
    """
    paths: list[str] = []
    _collect_optional(output_schema, "", paths)
    return tuple(paths)


def tool_description(spec: ToolSpec) -> str:
    """Return the full description sent to the model for *spec*.

    The spec's prose :attr:`~kodo.toolspecs.ToolSpec.description` (which already
    carries the tool's purpose, rules, and when-to-use guidance), followed by the
    dense output-schema sketch and — when any output property is optional — the
    one-line note naming the fields that may be absent.

    A spec whose ``output_schema`` declares no properties (a tool that returns
    nothing meaningful) gets no ``Returns:`` block at all.

    Args:
        spec: The tool specification to describe.

    Returns:
        str: The description string for this tool's LLM tool definition.
    """
    props = spec.output_schema.get("properties")
    branches = spec.output_schema.get("oneOf") or spec.output_schema.get("anyOf")
    if not (isinstance(props, dict) and props) and not isinstance(branches, list):
        return spec.description

    parts = [spec.description, f"{_RETURNS_PREFIX}{dense_output_schema(spec.output_schema)}"]
    optional = optional_output_paths(spec.output_schema)
    if optional:
        parts.append(f"{_OPTIONAL_PREFIX}{', '.join(f'`{p}`' for p in optional)}.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dumps(value: object) -> str:
    """Serialize *value* as compact single-line JSON."""
    return json.dumps(value, separators=(", ", ": "), ensure_ascii=False)


def _type_name(node: dict[str, object]) -> str:
    """Parenthesized type of *node* (``"(string)"``, ``"(integer|null)"``)."""
    raw = node.get("type")
    if isinstance(raw, list):
        return f"({'|'.join(str(t) for t in raw)})"
    return f"({raw})" if raw else "(any)"


def _dense(node: object) -> object:
    """Return the dense stand-in for one JSON-Schema *node*.

    Objects become dicts, arrays become one-element lists, and everything else
    becomes its description string (or its parenthesized type when it has none).
    """
    if not isinstance(node, dict):
        return ""

    branches = node.get("oneOf") or node.get("anyOf")
    if isinstance(branches, list):
        for branch in branches:
            if isinstance(branch, dict):
                return _dense(branch)

    props = node.get("properties")
    if isinstance(props, dict) and props:
        return {str(key): _dense(value) for key, value in props.items()}

    items = node.get("items")
    if isinstance(items, dict):
        item_props = items.get("properties")
        if (isinstance(item_props, dict) and item_props) or "items" in items:
            # Array of objects (or of arrays): the item's own shape is more
            # informative than the array's prose, so render the item.
            return [_dense(items)]
        # Array of scalars: the array property's description describes the
        # elements ("Matching paths, relative to `root`."), so it goes inside.
        return [_describe_leaf(items) or _describe_leaf(node) or _type_name(items)]

    return _describe_leaf(node) or _type_name(node)


def _describe_leaf(node: dict[str, object]) -> str:
    """Return *node*'s ``description``, or ``""`` when it has none."""
    description = node.get("description")
    return description.strip() if isinstance(description, str) else ""


def _collect_optional(node: object, prefix: str, out: list[str]) -> None:
    """Append the optional-property paths under *node* to *out*."""
    if not isinstance(node, dict):
        return

    branches = node.get("oneOf") or node.get("anyOf")
    if isinstance(branches, list):
        for branch in branches:
            _collect_optional(branch, prefix, out)
        return

    props = node.get("properties")
    if isinstance(props, dict) and props:
        required_raw = node.get("required")
        required = {str(r) for r in required_raw} if isinstance(required_raw, list) else set()
        for key, value in props.items():
            path = f"{prefix}{key}"
            if key not in required and path not in out:
                out.append(path)
            _collect_optional(value, f"{path}.", out)
        return

    items = node.get("items")
    if isinstance(items, dict):
        _collect_optional(items, f"{prefix[:-1]}[]." if prefix.endswith(".") else prefix, out)
