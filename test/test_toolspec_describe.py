"""Tests for :mod:`kodo.toolspecs._describe` — the model-facing tool description.

An LLM tool definition carries only ``name``/``description``/``input_schema``, so
a tool's ``output_schema`` reaches the model exclusively as the dense sketch
:func:`~kodo.toolspecs.tool_description` appends to the description. These tests
pin the dense form (description-only, no JSON-Schema scaffolding), the
optional-field note that replaces ``required``, and the catalog-wide invariant
that every spec's description carries its when-to-use guidance now that the
separate ``when_to_use`` field is gone.
"""

from __future__ import annotations

import json

import pytest

from kodo.toolspecs import (
    ALL_TOOLS,
    RUN_COMMAND,
    SCHEMA_COMPLIANCE_KEY,
    ToolSpec,
    dense_output_schema,
    optional_output_paths,
    tool_description,
)

# ---------------------------------------------------------------------------
# dense_output_schema
# ---------------------------------------------------------------------------


def test_dense_output_schema_collapses_properties_to_descriptions() -> None:
    """The canonical case: every property becomes its description string."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "root": {"type": "string", "description": "The resolved absolute search root."},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Matching paths, relative to `root`.",
            },
            "count": {"type": "integer", "description": "Number of paths returned."},
            "truncated": {
                "type": "boolean",
                "description": "True if results were capped at `max_results`.",
            },
        },
        "required": ["root", "files", "count", "truncated"],
    }
    assert dense_output_schema(schema) == (
        '{"root": "The resolved absolute search root.", '
        '"files": ["Matching paths, relative to `root`."], '
        '"count": "Number of paths returned.", '
        '"truncated": "True if results were capped at `max_results`."}'
    )


def test_dense_output_schema_drops_schema_scaffolding() -> None:
    """No `type`, `properties`, or `required` keys survive into the sketch."""
    rendered = dense_output_schema(RUN_COMMAND.output_schema)
    for scaffolding in ('"type"', '"properties"', '"required"', '"description"'):
        assert scaffolding not in rendered
    # It is still valid, single-line JSON.
    assert "\n" not in rendered
    assert isinstance(json.loads(rendered), dict)


def test_dense_output_schema_recurses_into_array_of_objects() -> None:
    """An array of objects renders its *item* shape, not the array's prose."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "description": "One entry per matching line.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path."},
                        "line": {"type": "integer", "description": "Line number."},
                    },
                    "required": ["path", "line"],
                },
            },
        },
        "required": ["matches"],
    }
    assert dense_output_schema(schema) == (
        '{"matches": [{"path": "File path.", "line": "Line number."}]}'
    )


def test_dense_output_schema_recurses_into_nested_objects() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "object",
                "properties": {"detail": {"type": "string", "description": "The detail."}},
                "required": ["detail"],
            },
        },
    }
    assert dense_output_schema(schema) == '{"summary": {"detail": "The detail."}}'


def test_dense_output_schema_falls_back_to_type_when_description_absent() -> None:
    """A key never maps to an empty string — the type stands in."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "exit_code": {"type": ["integer", "null"]},
            "opaque": {},
        },
    }
    assert dense_output_schema(schema) == (
        '{"name": "(string)", "exit_code": "(integer|null)", "opaque": "(any)"}'
    )


def test_dense_output_schema_renders_oneof_branches() -> None:
    """A dual-shape schema renders every branch, joined by ` | `."""
    schema: dict[str, object] = {
        "oneOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "string", "description": "A."}},
            },
            {
                "type": "object",
                "properties": {"b": {"type": "string", "description": "B."}},
            },
        ]
    }
    assert dense_output_schema(schema) == '{"a": "A."} | {"b": "B."}'


# ---------------------------------------------------------------------------
# optional_output_paths
# ---------------------------------------------------------------------------


def test_optional_output_paths_reports_non_required_properties() -> None:
    assert optional_output_paths(RUN_COMMAND.output_schema) == (
        "checkpoint_sha",
        "checkpoint_root",
    )


def test_optional_output_paths_empty_when_everything_is_required() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
    }
    assert optional_output_paths(schema) == ()


def test_optional_output_paths_uses_dotted_and_bracket_nesting() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "object",
                "properties": {
                    "detail": {"type": "string"},
                    "extra": {"type": "string"},
                },
                "required": ["detail"],
            },
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["summary", "matches"],
    }
    assert optional_output_paths(schema) == ("summary.extra", "matches[].line")


# ---------------------------------------------------------------------------
# tool_description
# ---------------------------------------------------------------------------


def test_tool_description_appends_returns_and_optional_note() -> None:
    described = tool_description(RUN_COMMAND)
    assert described.startswith(RUN_COMMAND.description)
    assert "\n\nReturns: {" in described
    assert (
        "These fields may be absent from the result: `checkpoint_sha`, `checkpoint_root`."
        in described
    )


def test_tool_description_omits_optional_note_when_all_required() -> None:
    spec = _spec_with_output(
        {
            "type": "object",
            "properties": {"a": {"type": "string", "description": "A."}},
            "required": ["a"],
        }
    )
    described = tool_description(spec)
    assert 'Returns: {"a": "A."}' in described
    assert "may be absent" not in described


def test_tool_description_omits_returns_block_for_empty_output_schema() -> None:
    """A tool that returns nothing meaningful gets no Returns block at all."""
    spec = _spec_with_output({"type": "object", "properties": {}})
    assert tool_description(spec) == spec.description


def test_dense_form_excludes_engine_owned_schema_compliance() -> None:
    """`schema_compliance` is injected into every result identically; repeating
    its long explanation under all ~30 tools would cost far more than it
    teaches, so the dense sketch renders the *raw* schema without it.

    Only the sketch is constrained — a spec whose prose deliberately discusses
    the flag (``return_result`` tells sub-agents the engine reports
    ``schema_compliance: false`` on a repair) is free to keep doing so.
    """
    for spec in ALL_TOOLS:
        assert SCHEMA_COMPLIANCE_KEY not in dense_output_schema(spec.output_schema), spec.name


# ---------------------------------------------------------------------------
# Catalog-wide invariants
# ---------------------------------------------------------------------------


def test_every_spec_description_carries_when_to_use_guidance() -> None:
    """`when_to_use` was merged into `description`, the only prose channel to the
    model. A spec whose guidance is fully implied by its prose is exempt only if
    it is listed here, so a new spec cannot silently ship without routing help."""
    # Tools whose description already states the trigger unambiguously ("call
    # this exactly once, last") and so need no separate when-to-use sentence.
    exempt = {"return_result", "submit_evaluation"}
    for spec in ALL_TOOLS:
        if spec.name in exempt:
            continue
        assert "When to use" in spec.description, spec.name


def test_no_spec_declares_a_when_to_use_field() -> None:
    """The field is gone from ToolSpec; nothing may resurrect it per-spec."""
    assert not hasattr(ToolSpec, "when_to_use")
    for spec in ALL_TOOLS:
        assert not hasattr(spec, "when_to_use"), spec.name


@pytest.mark.parametrize("spec", ALL_TOOLS, ids=lambda s: s.name)
def test_tool_description_is_renderable_for_every_spec(spec: ToolSpec) -> None:
    """Nothing in the catalog trips the renderer, and the prose always leads."""
    described = tool_description(spec)
    assert described.startswith(spec.description)
    assert described.strip() == described


def _spec_with_output(output_schema: dict[str, object]) -> ToolSpec:
    return ToolSpec(
        name="probe",
        external_name="Probe",
        user_description="Probe tool",
        description="Probe description.",
        input_schema={"type": "object", "properties": {}},
        output_schema=output_schema,
        security_impact=RUN_COMMAND.security_impact,
        input_visibility={},
        output_visibility={},
    )
