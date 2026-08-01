"""Guard rails for the sub-agent specs and their wiring into the registry.

Mirrors the role of ``test_tools_compliance.py`` for tools: every sub-agent
(except the user-facing entry agents) declares a typed ``SubAgentSpec``, the
specs and the ``subagent_*.md`` files correspond one-to-one, the schemas are
well-formed, and the registry auto-grants ``return_result`` + the short
Input Parameters pointer note to schema-bearing agents while leaving entry
agents untouched. Neither the input nor the output schema is ever restated as
prose in a system prompt (see ``_registry.py``'s module docstring) — the input
schema reaches a caller as real JSON Schema on ``run_subagent_<name>``, and the
sub-agent itself sees concrete values (not the schema) under ``## Input
Parameters`` in its first user turn (``_render_task_input``), not here.
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
    if isinstance(branches, list):  # dual-role agent (top-level oneOf of shapes)
        assert all(b.get("type") == "object" for b in branches)
    else:
        assert out.get("type") == "object"


def test_toolchain_builder_requires_project_path() -> None:
    """``project_path`` must be required so the agent never falls back to caller cwd."""
    spec = _SPECS_BY_NAME["toolchain_builder"]
    assert "project_path" in spec.input_schema["properties"]
    assert "project_path" in spec.input_schema["required"]


def _critic_names() -> list[str]:
    """Names of every agent that declares ``role: critic``, read off the registry.

    Derived rather than pattern-matched on ``_critic`` so this tracks the
    frontmatter that actually drives the engine's behaviour.
    """
    registry = AgentRegistry(_AGENTS_DIR)
    return sorted(a.name for a in registry.all_agents() if a.is_critic)


def test_every_critic_declares_the_shared_verdict_shape() -> None:
    """Every critic returns one shape: the reviewed path, the verdict, and the
    concerns — the schema the retired ``document_feedback`` tool declared,
    promoted to the critic's own ``return_result`` payload so a critic reports
    once instead of twice."""
    critics = _critic_names()
    assert critics, "expected the pipeline to have critics; the fixture is wrong"
    for name in critics:
        out = _SPECS_BY_NAME[name].output_schema
        props = out["properties"]  # type: ignore[index]
        assert set(props) == {"path", "accept", "concerns", "summary"}, name
        assert set(out["required"]) == {"path", "accept", "concerns"}, name  # type: ignore[index]


def _escalation_capable_names() -> set[str]:
    """Agents that may escalate, read off the live registry.

    The marker is the shared ``base_escalation.md`` snippet in an agent's
    ``bases:`` frontmatter — an explicit per-agent declaration, never inferred
    from the agent's name or role. Derived here rather than hardcoded so this
    file cannot drift from which agents actually carry the contract.
    """
    registry = AgentRegistry(_AGENTS_DIR)
    return {
        name for name in _agent_names() - _ENTRY_AGENTS if "escalation" in registry.get(name).bases
    }


def test_escalation_is_declared_in_the_prompt_and_the_schema_together() -> None:
    """A blocked author escalates through ``return_result`` (there is no
    ``escalate_blocker`` tool), so the two halves must ship together: the
    ``bases: escalation`` prompt section explaining when and how, and the
    ``reason``/``options`` fields on the output schema it tells the agent to
    set. Either half alone is inert."""
    escalators = _escalation_capable_names()
    assert escalators, "no agent declares the escalation base; the contract has no holders"
    for name in escalators:
        props = _SPECS_BY_NAME[name].output_schema["properties"]  # type: ignore[index]
        assert "reason" in props, f"{name} loads the escalation base but declares no reason field"
        assert "options" in props, f"{name} loads the escalation base but declares no options field"
    # The reverse. Keyed on the ``reason`` + ``options`` *pair*, because
    # ``reason`` alone is not the marker: ``planner`` has an unrelated one
    # ("why a plan is or isn't warranted") that has nothing to do with blockers.
    for name, spec in _SPECS_BY_NAME.items():
        props = spec.output_schema["properties"]
        if {"reason", "options"} <= set(props):  # type: ignore[arg-type]
            assert name in escalators, f"{name} declares the escalation fields but no base"


def test_an_escalating_author_can_produce_a_compliant_result() -> None:
    """Only ``summary`` is required of an escalation-capable author: one blocked
    before it wrote anything has no ``primary_path`` to report, and a backfilled
    required field would mark the whole escalation non-compliant — the engine's
    "this sub-agent failed" signal, which an escalation is not."""
    for name in _escalation_capable_names():
        schema = _SPECS_BY_NAME[name].output_schema
        assert set(schema["required"]) == {"summary"}, name  # type: ignore[index]
        _, compliant = normalize_output(
            schema,
            {
                "summary": "Blocked: the Tech Stack names no database.",
                "reason": "missing_tech_stack_field",
                "options": ["Postgres", "SQLite"],
            },
        )
        assert compliant, f"{name} cannot return a compliant escalation"


def test_concern_kind_is_free_form_with_a_pointer_to_the_prompt() -> None:
    """The concern catalogue is prose in each critic's ``### Concern vocabulary``
    section, not a schema ``enum``: the catalogue carries per-kind explanations
    and routing rules a bare enum cannot, and nothing ever enforced the enum
    (``normalize_output`` checks declared keys and required fields, never value
    constraints). The schema must therefore point at the prompt instead of
    duplicating a list that would silently drift."""
    for name in _critic_names():
        item = _SPECS_BY_NAME[name].output_schema["properties"]["concerns"]["items"]  # type: ignore[index]
        kind = item["properties"]["kind"]  # type: ignore[index]
        assert "enum" not in kind, f"{name} reintroduced a concern-kind enum"
        assert "Concern vocabulary" in kind["description"], name


def test_every_critic_prompt_carries_its_concern_vocabulary() -> None:
    """The other half of the contract above: since the kinds left the schema,
    each critic's prompt must actually contain the section the schema points at."""
    for name in _critic_names():
        body = (_AGENTS_DIR / f"subagent_{name}.md").read_text(encoding="utf-8")
        assert "### Concern vocabulary" in body, f"{name} has no concern catalogue in its prompt"


def test_test_coder_output_is_solo_author_shape() -> None:
    """test_coder is now a plain solo author (no dual-role oneOf).

    Behavioral review of the Test Plan moved to ``test_design_critic``, so
    ``test_coder`` returns only the author shape (the test code + stubs it wrote).
    """
    spec = _SPECS_BY_NAME["test_coder"]
    assert spec.output_schema.get("oneOf") is None
    assert spec.output_schema.get("type") == "object"
    assert "primary_path" in spec.output_schema["properties"]  # type: ignore[index]


def test_test_coder_normalizes_author_output() -> None:
    """normalize_output accepts the solo author payload for test_coder."""
    schema = _SPECS_BY_NAME["test_coder"].output_schema
    _, author_ok = normalize_output(
        schema, {"primary_path": "src/a.py", "paths": ["src/a.py"], "summary": "s"}
    )
    assert author_ok


def test_test_design_critic_vocabulary_leads_with_behavioral_kinds() -> None:
    """The behavioral kinds are this critic's whole reason to exist, so they must
    survive the move of the catalogue from schema enum to prompt prose."""
    body = (_AGENTS_DIR / "subagent_test_design_critic.md").read_text(encoding="utf-8")
    vocabulary = body.split("### Concern vocabulary", 1)[1]
    assert "non_behavioral_test" in vocabulary
    assert "over_specified_test" in vocabulary


def test_return_result_with_engine_owned_compliance_key_stays_compliant() -> None:
    """A result that includes the engine-owned ``schema_compliance`` key is compliant.

    Regression: an agent is shown the *augmented* output schema (as the real
    JSON Schema bound to its own ``return_result`` tool's ``result`` parameter),
    which lists ``schema_compliance`` as required, so an obedient agent
    includes it in its ``return_result`` payload. Validation,
    however, runs against the *raw* ``spec.output_schema`` that omits the key.
    Before the fix, normalize_output treated the supplied key as an undeclared
    extra, dropped it, and wrongly marked the otherwise-perfect result
    non-compliant — flagging the whole sub-agent run as failed. This mirrors the
    real toolchain-setup payload that exhibited the bug.
    """
    spec = _SPECS_BY_NAME["toolchain_builder"]
    payload = {
        "scripts_created": ["scripts/build.sh"],
        "development_md_path": "DEVELOPMENT.md",
        "ecosystem": "python",
        "manifest_paths": ["pyproject.toml"],
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
    spec = _SPECS_BY_NAME["toolchain_builder"]
    payload = {
        "scripts_created": ["scripts/build.sh"],
        "development_md_path": "DEVELOPMENT.md",
        "ecosystem": "python",
        "summary": "done",
        "schema_compliance": True,
        "stray": 1,  # genuinely undeclared
    }
    normalized, compliant = normalize_output(spec.output_schema, payload)
    assert not compliant
    assert "stray" not in normalized
    assert normalized["schema_compliance"] is False


def test_registry_auto_grants_return_result_and_input_parameters_note() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    for name in _SPECS_BY_NAME:
        agent = registry.get(name)
        assert "return_result" in agent.tools, name
        assert "Input Parameters" in agent.system_prompt, name
        assert "## Your Task Contract" not in agent.system_prompt, name
        assert "input_schema" not in agent.system_prompt, name


def test_registry_leaves_entry_agents_without_return_result() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    for name in _ENTRY_AGENTS:
        agent = registry.get(name)
        assert "return_result" not in agent.tools, name
        assert "Input Parameters" not in agent.system_prompt, name
        assert "## Your Task Contract" not in agent.system_prompt, name


def test_guide_roster_does_not_restate_callee_schemas() -> None:
    """Schemas reach the caller as real JSON Schema on each
    ``run_subagent_<name>`` tool, so the roster must not carry a second, prose
    copy that cannot stay in sync with it."""
    registry = AgentRegistry(_AGENTS_DIR)
    section = registry.render_subagents_section("guide")
    assert "Input schema" not in section
    assert "Output schema" not in section
    assert "for_revision_path" not in section


def test_callee_schemas_reach_the_guide_through_its_tools() -> None:
    """The other half: what left the roster must be on the tools themselves."""
    registry = AgentRegistry(_AGENTS_DIR)
    specs = {s.name: s for s in registry.run_subagent_specs("guide")}

    architect = specs["run_subagent_architect"]
    assert "for_revision_path" in architect.input_schema["properties"]  # type: ignore[index]
    # architect's own extra output field survives into the tool's declared output.
    assert "end_to_end_testable" in architect.output_schema["properties"]  # type: ignore[index]
    # A reviewed sub-agent's tool takes the round budget and reports the loop.
    assert "max_rounds" in architect.input_schema["properties"]  # type: ignore[index]
    assert "review" in architect.output_schema["properties"]  # type: ignore[index]

    # An unreviewed one gets neither.
    narrative = specs["run_subagent_narrative_author"]
    assert "max_rounds" not in narrative.input_schema["properties"]  # type: ignore[index]
    assert "review" not in narrative.output_schema["properties"]  # type: ignore[index]


def test_return_result_is_bound_to_each_agents_own_output_schema() -> None:
    """A sub-agent reads the shape it must produce off ``return_result``'s
    ``result`` parameter — the authoritative copy — rather than from prose."""
    registry = AgentRegistry(_AGENTS_DIR)
    (spec,) = registry.return_result_specs("architect")
    result = spec.input_schema["properties"]["result"]  # type: ignore[index]
    assert "primary_path" in result["properties"]
    assert "end_to_end_testable" in result["properties"]
    # The engine-owned compliance field is declared where the agent will read it.
    assert "schema_compliance" in result["properties"]

    # Entry agents never return a result to anybody, so they get no such tool.
    assert registry.return_result_specs("guide") == []
