"""Guard rails for the sub-agent specs and their wiring into the registry.

Mirrors the role of ``test_tools_compliance.py`` for tools: every sub-agent
(except the user-facing entry agents) declares a typed ``SubAgentSpec``, the
specs and the ``subagent_*.md`` files correspond one-to-one, the schemas are
well-formed, and the registry auto-grants ``return_result`` + a ``## Your Task
Contract`` to schema-bearing agents while leaving entry agents untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.subagents import AgentRegistry
from kodo.subagents.specs import ALL_SUBAGENTS, SubAgentSpec
from kodo.toolspecs import normalize_output

_AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "kodo" / "subagents"
# Entry agents the user talks to directly; they have no caller and no spec.
_ENTRY_AGENTS = {"guide", "problem_solver"}

_SPECS_BY_NAME = {s.name: s for s in ALL_SUBAGENTS}


def _agent_names() -> set[str]:
    return {p.stem[len("subagent_") :] for p in _AGENTS_DIR.glob("subagent_*.md")}


def test_every_non_entry_agent_has_a_spec() -> None:
    missing = _agent_names() - _ENTRY_AGENTS - set(_SPECS_BY_NAME)
    assert not missing, f"sub-agents without a SubAgentSpec: {sorted(missing)}"


def test_every_spec_has_a_subagent_file() -> None:
    orphans = set(_SPECS_BY_NAME) - _agent_names()
    assert not orphans, f"SubAgentSpecs with no subagent_<name>.md: {sorted(orphans)}"


def test_entry_agents_have_no_spec() -> None:
    assert not (_ENTRY_AGENTS & set(_SPECS_BY_NAME))


@pytest.mark.parametrize("spec", ALL_SUBAGENTS, ids=lambda s: s.name)
def test_schemas_are_well_formed(spec: SubAgentSpec) -> None:
    assert isinstance(spec, SubAgentSpec)
    assert spec.name and spec.description
    assert spec.input_schema.get("type") == "object"
    out = spec.output_schema
    branches = out.get("oneOf")
    if isinstance(branches, list):  # dual-role agent (test_coder)
        assert all(b.get("type") == "object" for b in branches)
    else:
        assert out.get("type") == "object"


def test_critic_specs_constrain_concern_kinds() -> None:
    """Every critic's output declares a non-empty concern-kind enum."""
    for spec in ALL_SUBAGENTS:
        if not spec.name.endswith("_critic"):
            continue
        item = spec.output_schema["properties"]["concerns"]["items"]  # type: ignore[index]
        kinds = item["properties"]["kind"]["enum"]  # type: ignore[index]
        assert kinds, f"{spec.name} declares no concern kinds"


def test_test_coder_output_is_oneof() -> None:
    """The dual-role agent's output is a oneOf of author + critic shapes."""
    spec = _SPECS_BY_NAME["test_coder"]
    branches = spec.output_schema.get("oneOf")
    assert isinstance(branches, list) and len(branches) == 2
    # One branch is the author shape (primary_path), the other the critic shape.
    has_author = any("primary_path" in b.get("properties", {}) for b in branches)
    has_critic = any("verdict" in b.get("properties", {}) for b in branches)
    assert has_author and has_critic


def test_test_coder_normalizes_either_branch() -> None:
    """normalize_output accepts either oneOf branch for the dual-role agent."""
    schema = _SPECS_BY_NAME["test_coder"].output_schema
    _, author_ok = normalize_output(
        schema, {"primary_path": "src/a.py", "paths": ["src/a.py"], "summary": "s"}
    )
    _, critic_ok = normalize_output(schema, {"verdict": "rejected", "concerns": []})
    assert author_ok and critic_ok


def test_return_result_with_engine_owned_compliance_key_stays_compliant() -> None:
    """A result that includes the engine-owned ``schema_compliance`` key is compliant.

    Regression: an agent is shown the *augmented* output schema (via its ``##
    Your Task Contract``), which lists ``schema_compliance`` as required, so an
    obedient agent includes it in its ``return_result`` payload. Validation,
    however, runs against the *raw* ``spec.output_schema`` that omits the key.
    Before the fix, normalize_output treated the supplied key as an undeclared
    extra, dropped it, and wrongly marked the otherwise-perfect result
    non-compliant — flagging the whole sub-agent run as failed. This mirrors the
    real toolchain_python payload that exhibited the bug.
    """
    spec = _SPECS_BY_NAME["toolchain_python"]
    payload = {
        "scripts_created": ["scripts/build.sh"],
        "development_md_path": "DEVELOPMENT.md",
        "pyproject_path": "pyproject.toml",
        "summary": "done",
        "schema_compliance": True,  # included exactly as the augmented contract asks
    }
    normalized, compliant = normalize_output(spec.output_schema, payload)
    assert compliant, f"normalized -> {normalized!r}"
    # The engine owns the value: it is re-injected, never trusted from the input.
    assert normalized["schema_compliance"] is True


def test_engine_owned_compliance_key_does_not_mask_a_real_violation() -> None:
    """Including ``schema_compliance`` must not whitewash an actually bad payload.

    A genuinely undeclared field is still dropped and still marks the result
    non-compliant even when ``schema_compliance`` rides along in the input.
    """
    spec = _SPECS_BY_NAME["toolchain_python"]
    payload = {
        "scripts_created": ["scripts/build.sh"],
        "development_md_path": "DEVELOPMENT.md",
        "summary": "done",
        "schema_compliance": True,
        "stray": 1,  # genuinely undeclared
    }
    normalized, compliant = normalize_output(spec.output_schema, payload)
    assert not compliant
    assert "stray" not in normalized
    assert normalized["schema_compliance"] is False


def test_registry_auto_grants_return_result_and_contract() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    for name in _SPECS_BY_NAME:
        agent = registry.get(name)
        assert "return_result" in agent.tools, name
        assert "## Your Task Contract" in agent.system_prompt, name


def test_registry_leaves_entry_agents_without_return_result() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    for name in _ENTRY_AGENTS:
        agent = registry.get(name)
        assert "return_result" not in agent.tools, name
        assert "## Your Task Contract" not in agent.system_prompt, name


def test_guide_roster_embeds_callee_schemas() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    section = registry.render_subagents_section("guide")
    # The roster now carries each callee's input + output schema blocks.
    assert "Input schema" in section
    assert "Output schema" in section
    # A concrete callee field shows the schemas are really rendered.
    assert "for_revision_path" in section
    assert "end_to_end_testable" in section  # architect's extra output field
