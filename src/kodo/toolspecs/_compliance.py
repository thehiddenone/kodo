"""Engine-owned output-schema compliance: augmentation + normalization.

A tool's :attr:`~kodo.toolspecs.ToolSpec.output_schema` declares the *success*
shape only. The engine owns one extra field, ``schema_compliance`` (a boolean),
which it injects in-flight — both into the schema it shows agents
(:func:`augment_output_schema`) and into every result an agent receives
(:func:`normalize_output`). Specs therefore never declare ``schema_compliance``;
if one somehow does, the engine's definition/value replaces it.

:func:`normalize_output` makes a best effort to hand the agent something usable:
it drops undeclared fields, backfills missing required fields with empty
strings, and reports whether the raw output was already compliant. An
``{"error": ...}`` envelope is treated as a valid (compliant) failure result and
passes through untouched apart from the injected ``schema_compliance: True``.
"""

from __future__ import annotations

from copy import deepcopy

__all__ = [
    "SCHEMA_COMPLIANCE_KEY",
    "augment_output_schema",
    "normalize_output",
    "tool_result_succeeded",
]

SCHEMA_COMPLIANCE_KEY = "schema_compliance"

# The engine-owned schema fragment for the injected field.
_SCHEMA_COMPLIANCE_PROPERTY: dict[str, object] = {
    "type": "boolean",
    "description": (
        "Engine-owned. True when the tool's raw output matched its declared "
        "output schema; False when the engine had to repair it (missing "
        "required fields were backfilled with empty strings and/or undeclared "
        "fields were dropped). Treat a False value as a signal that some data "
        "may be missing or imprecise."
    ),
}


def augment_output_schema(output_schema: dict[str, object]) -> dict[str, object]:
    """Return *output_schema* with the engine-owned ``schema_compliance`` field.

    The input is not mutated. Any pre-existing ``schema_compliance`` property is
    replaced by the engine's definition and the key is ensured present in
    ``required``.

    Args:
        output_schema: The spec's declared output schema (an ``object`` schema).

    Returns:
        dict[str, object]: A new schema with ``schema_compliance`` added to
        ``properties`` and ``required``.
    """
    schema = deepcopy(output_schema)
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
        schema["properties"] = props
    props[SCHEMA_COMPLIANCE_KEY] = dict(_SCHEMA_COMPLIANCE_PROPERTY)

    required = schema.get("required")
    if not isinstance(required, list):
        required = []
    if SCHEMA_COMPLIANCE_KEY not in required:
        required = [*required, SCHEMA_COMPLIANCE_KEY]
    schema["required"] = required
    return schema


def normalize_output(
    output_schema: dict[str, object], raw: object
) -> tuple[dict[str, object], bool]:
    """Coerce a tool's raw result to its declared schema; report compliance.

    Behaviour:

    - A non-object result is non-compliant; it is wrapped as
      ``{"result": <stringified raw>}`` so the data still reaches the agent.
    - An ``{"error": ...}`` object is a valid failure envelope: compliant,
      passed through unchanged (apart from the injected ``schema_compliance``).
    - Otherwise: undeclared properties are dropped (non-compliant), missing
      required properties are backfilled with ``""`` (non-compliant). The
      ``schema_compliance`` boolean is then injected (always overwriting any
      value the tool supplied, since the engine owns the field).

    Args:
        output_schema: The spec's declared output schema (without
            ``schema_compliance``).
        raw: The parsed tool result (typically a ``dict``).

    Returns:
        tuple[dict[str, object], bool]: ``(normalized_object, compliant)``.
        ``normalized_object`` always contains a ``schema_compliance`` key equal
        to ``compliant``.
    """
    if not isinstance(raw, dict):
        result = {"result": "" if raw is None else str(raw), SCHEMA_COMPLIANCE_KEY: False}
        return result, False

    # An error envelope is a sanctioned, compliant failure result.
    if "error" in raw:
        out = dict(raw)
        out[SCHEMA_COMPLIANCE_KEY] = True
        return out, True

    props = output_schema.get("properties")
    declared: set[str] = set(props) if isinstance(props, dict) else set()
    required_raw = output_schema.get("required")
    required: list[str] = [str(r) for r in required_raw] if isinstance(required_raw, list) else []

    compliant = True
    # Drop undeclared fields (only meaningful when the schema declares any).
    if declared:
        kept = {k: v for k, v in raw.items() if k in declared}
        if len(kept) != len(raw):
            compliant = False
    else:
        kept = dict(raw)

    # Backfill missing required fields with empty strings.
    for key in required:
        if key not in kept:
            kept[key] = ""
            compliant = False

    kept[SCHEMA_COMPLIANCE_KEY] = compliant
    return kept, compliant


def tool_result_succeeded(output: dict[str, object] | None) -> bool | None:
    """Classify a normalized tool result as success / failure / not-yet-known.

    Used to drive the VSIX success-✓ / failure-✗ badge next to a tool name.
    The convention mirrors how tools report outcomes (see the toolspec output
    schemas and :func:`normalize_output`):

    - ``None`` input → ``None`` (the result has not arrived; show neither badge).
    - An ``{"error": ...}`` envelope → ``False`` (sanctioned failure result).
    - An ``exit_code`` field (``run_command``) → ``True`` only when it is the
      integer ``0``; a non-zero code, or ``null`` (e.g. a timed-out command),
      is a failure.
    - A boolean ``success`` field (``toolchain_*``) → that value.
    - Anything else (a compliant ``{"status": ...}`` envelope) → ``True``.

    ``schema_compliance`` is deliberately ignored here: a repaired-but-otherwise
    successful result is still a success; the separate schema-compliance warning
    already surfaces repairs.

    Args:
        output: The normalized tool output dict (with ``schema_compliance``
            injected), or ``None`` if the result is not yet known.

    Returns:
        bool | None: ``True`` on success, ``False`` on failure, ``None`` when
        unknown.
    """
    if output is None:
        return None
    if "error" in output:
        return False
    if "exit_code" in output:
        return output["exit_code"] == 0
    success = output.get("success")
    if isinstance(success, bool):
        return success
    return True
