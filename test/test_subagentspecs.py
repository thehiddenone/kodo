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

from kodo.subagents import (
    PRODUCES_REMAINDER,
    RESPONSIBILITY_CODE_KEY,
    ROLE_CODE,
    ROLE_TECH_STACK,
    SCOPE_DEPENDENCIES,
    SCOPE_UNDER_REVIEW,
    AgentRegistry,
    shared_token,
)
from kodo.subagents.specs import ALL_SUBAGENTS, SubAgentSpec
from kodo.toolspecs import ENGINE_OWNED_TASK_FIELDS, ToolSpec, normalize_output

_AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "kodo" / "subagents"
# Entry agents the user talks to directly; they have no caller and no spec.
_ENTRY_AGENTS = {"guide", "problem_solver"}


def _pipeline_author_names() -> set[str]:
    """Every non-critic spec that writes files — read off the live registry.

    An agent is an author here when some other agent declares it as its
    ``critic:``'s partner, or when it declares a critic itself; the standalone
    specialists (`developer`, `investigator`, `planner`, …) are out of scope.
    """
    registry = AgentRegistry(_AGENTS_DIR)
    return {
        agent.name
        for agent in registry.all_agents()
        if agent.critic and agent.name in _SPECS_BY_NAME
    } | {"narrative_author"}


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
    # A spec carries schemas and nothing else. The caller-facing prose lives in
    # the agent's own `## Purpose`, which becomes its run_subagent tool's
    # description — one text, one destination.
    assert spec.name
    assert not hasattr(spec, "description")
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


def test_every_critic_declares_the_shared_findings_shape() -> None:
    """Every critic returns one shape: the reviewed path, this round's findings,
    and a summary. There is deliberately no ``accept``: the verdict is derived
    from the resulting backlog (doc/FINDINGS.md §3), so a critic cannot report a
    pass while leaving problems outstanding, and the two can never disagree."""
    critics = _critic_names()
    assert critics, "expected the pipeline to have critics; the fixture is wrong"
    for name in critics:
        out = _SPECS_BY_NAME[name].output_schema
        props = out["properties"]  # type: ignore[index]
        assert set(props) == {"findings", "summary"}, name
        assert set(out["required"]) == {"findings"}, name  # type: ignore[index]


def test_a_finding_update_needs_only_an_id_and_the_changed_fields() -> None:
    """The update half of the protocol: ``{"id": "F1", "state": "fixed"}`` is a
    complete, compliant finding. Nothing on a finding is schema-required —
    requiring ``kind``/``description`` would make every close non-compliant,
    since ``normalize_output`` backfills a missing required field and flags the
    whole result."""
    for name in _critic_names():
        item = _SPECS_BY_NAME[name].output_schema["properties"]["findings"]["items"]  # type: ignore[index]
        assert item["required"] == [], name
        assert set(item["properties"]) == {  # type: ignore[index]
            "id",
            "kind",
            "description",
            "locations",
            "state",
        }, name
        # A finding points at a LIST of places, so one problem spanning two
        # files is one finding — the defect a work-product review exists to
        # catch (doc/FINDINGS.md).
        location = item["properties"]["locations"]["items"]  # type: ignore[index]
        assert location["required"] == ["path"], name
        assert set(location["properties"]) == {  # type: ignore[index]
            "path",
            "first_line",
            "last_line",
            "excerpt",
        }, name
        assert item["properties"]["state"]["enum"] == ["outstanding", "fixed"], name  # type: ignore[index]


def _escalation_capable_names() -> set[str]:
    """Agents that may escalate, read off the live registry.

    The marker is a ``{SHARED:escalation}`` inclusion in the agent's own body —
    an explicit per-agent declaration, never inferred from the agent's name or
    role. Read off the raw ``.md`` (the registry's rendered prompt has already
    expanded the token away), so this file cannot drift from which agents
    actually carry the contract.
    """
    token = shared_token("escalation")
    return {
        name
        for name in _agent_names() - _ENTRY_AGENTS
        if token in (_AGENTS_DIR / f"subagent_{name}.md").read_text(encoding="utf-8")
    }


def test_escalation_is_declared_in_the_prompt_and_the_schema_together() -> None:
    """A blocked author escalates through ``return_result`` (there is no
    ``escalate_blocker`` tool), so the two halves must ship together: the
    ``{SHARED:escalation}`` prompt block explaining when and how, and the
    ``reason``/``options`` fields on the output schema it tells the agent to
    set. Either half alone is inert."""
    escalators = _escalation_capable_names()
    assert escalators, "no agent declares the escalation base; the contract has no holders"
    for name in escalators:
        props = _SPECS_BY_NAME[name].output_schema["properties"]  # type: ignore[index]
        assert "reason" in props, f"{name} includes the escalation block but has no reason field"
        assert "options" in props, f"{name} includes the escalation block but has no options field"
    # The reverse. Keyed on the ``reason`` + ``options`` *pair*, because
    # ``reason`` alone is not the marker: ``planner`` has an unrelated one
    # ("why a plan is or isn't warranted") that has nothing to do with blockers.
    for name, spec in _SPECS_BY_NAME.items():
        props = spec.output_schema["properties"]
        if {"reason", "options"} <= set(props):  # type: ignore[arg-type]
            assert name in escalators, f"{name} declares the escalation fields but no block"


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
        item = _SPECS_BY_NAME[name].output_schema["properties"]["findings"]["items"]  # type: ignore[index]
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
    assert "paths" in spec.output_schema["properties"]  # type: ignore[index]


def test_test_coder_normalizes_author_output() -> None:
    """normalize_output accepts the solo author payload for test_coder."""
    schema = _SPECS_BY_NAME["test_coder"].output_schema
    _, author_ok = normalize_output(schema, {"paths": ["src/a.py"], "summary": "s"})
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


# The one schema-bearing agent the engine does *not* seed through
# `_render_task_input`: `_generate_compaction_summary` hands it a bare
# "Conversation transcript to compact: …" user message, so the Input Parameters
# note would describe a first message it never receives. It documents its real
# input itself, in its own `## Your Input` section.
_NO_TASK_INPUT_NOTE = {"compactor"}


def test_registry_auto_grants_return_result_and_input_parameters_note() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    for name in _SPECS_BY_NAME:
        agent = registry.get(name)
        assert "return_result" in agent.tools, name
        expected = name not in _NO_TASK_INPUT_NOTE
        assert ("Input Parameters" in agent.system_prompt) is expected, name
        assert "{PLACEHOLDER:" not in agent.system_prompt, name
        assert "## Your Task Contract" not in agent.system_prompt, name
        assert "input_schema" not in agent.system_prompt, name


def test_registry_leaves_entry_agents_without_return_result() -> None:
    registry = AgentRegistry(_AGENTS_DIR)
    for name in _ENTRY_AGENTS:
        agent = registry.get(name)
        assert "return_result" not in agent.tools, name
        assert "Input Parameters" not in agent.system_prompt, name
        assert "## Your Task Contract" not in agent.system_prompt, name


def test_guide_prompt_does_not_describe_its_subagents() -> None:
    """Everything about a sub-agent reaches the guide on the tool that invokes
    it — never in the prompt. The ``{PLACEHOLDER:SUBAGENTS}`` roster that used to
    restate it there is gone; this pins that it stays gone."""
    registry = AgentRegistry(_AGENTS_DIR)
    prompt = registry.get("guide").system_prompt
    assert "PLACEHOLDER" not in prompt
    assert "| Sub-agent |" not in prompt  # the roster table's header
    # The real property: no callee's own description is restated in the prompt.
    # It reaches the guide on the tool, and only there.
    for name in registry.allowed_subagents("guide"):
        purpose = registry.get(name).purpose
        if purpose:
            assert purpose.splitlines()[0] not in prompt, name


def test_callee_schemas_reach_the_guide_through_its_tools() -> None:
    """The other half: what left the roster must be on the tools themselves."""
    registry = AgentRegistry(_AGENTS_DIR)
    specs = {s.name: s for s in registry.run_subagent_specs("guide")}

    architect = specs["run_subagent_architect"]
    # The caller says what to do; the engine works out which files that means.
    for engine_owned in ENGINE_OWNED_TASK_FIELDS:
        assert engine_owned not in architect.input_schema["properties"]  # type: ignore[index]
        assert engine_owned not in architect.input_schema["required"]  # type: ignore[index]
    assert "instructions" in architect.input_schema["properties"]  # type: ignore[index]
    # architect's own extra output field survives into the tool's declared output.
    assert "end_to_end_testable" in architect.output_schema["properties"]  # type: ignore[index]
    # A reviewed sub-agent's tool takes the round budget and reports the loop.
    assert "max_rounds" in architect.input_schema["properties"]  # type: ignore[index]
    assert "review" in architect.output_schema["properties"]  # type: ignore[index]

    # A sub-agent with neither a critic nor a user gate gets neither field. Read
    # off the live registry rather than naming an agent: which agents are
    # reviewed is a frontmatter decision that moves, and hardcoding one here
    # turns an intentional flag change into a spurious test failure.
    unreviewed = [
        name
        for name in registry.allowed_subagents("guide")
        if f"run_subagent_{name}" in specs
        and not registry.get(name).critic
        and not registry.get(name).user_review
    ]
    assert unreviewed, "expected at least one sub-agent the guide calls with no review at all"
    for name in unreviewed:
        spec = specs[f"run_subagent_{name}"]
        assert "max_rounds" not in spec.input_schema["properties"], name  # type: ignore[index]
        assert "review" not in spec.output_schema["properties"], name  # type: ignore[index]

    # And the third shape: a user gate with no critic is still a loop, so it
    # reports through the same `review` block and takes the same budget.
    gate_only = [
        name
        for name in registry.allowed_subagents("guide")
        if f"run_subagent_{name}" in specs
        and registry.get(name).user_review
        and not registry.get(name).critic
    ]
    for name in gate_only:
        spec = specs[f"run_subagent_{name}"]
        assert "max_rounds" in spec.input_schema["properties"], name  # type: ignore[index]
        assert "review" in spec.output_schema["properties"], name  # type: ignore[index]


def test_return_result_is_bound_to_each_agents_own_output_schema() -> None:
    """A sub-agent reads the shape it must produce off ``return_result``'s
    ``result`` parameter — the authoritative copy — rather than from prose."""
    registry = AgentRegistry(_AGENTS_DIR)
    (spec,) = registry.return_result_specs("architect")
    result = spec.input_schema["properties"]["result"]  # type: ignore[index]
    assert "paths" in result["properties"]
    assert "end_to_end_testable" in result["properties"]
    # The engine-owned compliance field is declared where the agent will read it.
    assert "schema_compliance" in result["properties"]

    # Entry agents never return a result to anybody, so they get no such tool.
    assert registry.return_result_specs("guide") == []


# ---------------------------------------------------------------------------
# Artifact roles — what each agent produces and consumes
# ---------------------------------------------------------------------------


def test_every_pipeline_author_declares_what_it_produces() -> None:
    """An author whose output fills no declared role is invisible to every later
    stage: nothing can resolve against what it wrote. Read off the live
    registry rather than a hardcoded roster."""
    silent = sorted(
        name
        for name, spec in _SPECS_BY_NAME.items()
        if name in _pipeline_author_names() and not spec.produces
    )
    assert silent == []


def test_every_critic_consumes_the_thing_it_reviews() -> None:
    """A critic with no `under_review` need would be spawned against a work
    product it is never actually handed — the shape of the traced failure."""
    for name in _critic_names():
        scopes = {need.scope for need in _SPECS_BY_NAME[name].consumes}
        assert SCOPE_UNDER_REVIEW in scopes, name


def test_no_critic_declares_that_it_produces_an_artifact() -> None:
    """Critics write findings, not artifacts. One that claimed a role would
    overwrite its author's entry in the ledger."""
    for name in _critic_names():
        assert _SPECS_BY_NAME[name].produces == {}, name


def test_every_consumed_role_is_produced_by_somebody() -> None:
    """The registry enforces this at load time; asserting it here names the
    offending role directly instead of failing every test at import."""
    produced = {role for spec in _SPECS_BY_NAME.values() for role in spec.produces}
    for name, spec in _SPECS_BY_NAME.items():
        for need in spec.consumes:
            if need.scope == SCOPE_UNDER_REVIEW:
                continue
            assert need.role in produced, f"{name} consumes unproduced role {need.role}"


def test_produces_only_names_output_fields_the_schema_declares() -> None:
    """A role mapped to a field that does not exist silently resolves to
    nothing, which is indistinguishable from the agent not having run."""
    for name, spec in _SPECS_BY_NAME.items():
        properties = spec.output_schema.get("properties")
        declared = set(properties) if isinstance(properties, dict) else set()
        for one_of in spec.output_schema.get("oneOf") or []:
            branch = one_of.get("properties") if isinstance(one_of, dict) else None
            if isinstance(branch, dict):
                declared.update(branch)
        for role, field_name in spec.produces.items():
            if field_name == PRODUCES_REMAINDER:
                continue
            assert field_name in declared, f"{name}: role {role} -> unknown field {field_name}"


def test_at_most_one_role_takes_the_remainder() -> None:
    """Two roles both claiming "everything unclaimed" would each get the same
    files, making the split meaningless."""
    for name, spec in _SPECS_BY_NAME.items():
        remainder = [r for r, f in spec.produces.items() if f == PRODUCES_REMAINDER]
        assert len(remainder) <= 1, f"{name}: {remainder}"


def test_code_critic_asks_for_nothing_its_contract_excludes() -> None:
    """It judges code AS code — the design, requirements and test plan its coder
    was handed are explicitly out of scope, and a declaration that does not ask
    for them needs no per-agent opt-out flag."""
    roles = {need.role for need in _SPECS_BY_NAME["code_critic"].consumes}
    assert roles == {ROLE_CODE, ROLE_TECH_STACK}


def test_dependencies_scope_is_only_ever_declared_as_optional() -> None:
    """A component with no neighbours legitimately has no dependency designs,
    and a required need that nothing fills refuses the spawn — so declaring
    this scope as required would block every leaf component in the graph."""
    declared = [
        (name, need)
        for name, spec in _SPECS_BY_NAME.items()
        for need in spec.consumes
        if need.scope == SCOPE_DEPENDENCIES
    ]
    assert declared, "expected at least one agent to consume its neighbours' designs"
    for name, need in declared:
        assert need.required is False, name


def _tools_by_agent() -> dict[str, ToolSpec]:
    """Every `run_subagent_<name>` tool an entry agent can see, keyed by callee."""
    registry = AgentRegistry(_AGENTS_DIR)
    return {
        name: tool
        for caller in ("guide", "problem_solver")
        for name in registry.allowed_subagents(caller)
        for tool in registry.run_subagent_specs(caller)
        if tool.name == f"run_subagent_{name}"
    }


def test_no_resolved_tool_lets_a_caller_write_an_engine_owned_field() -> None:
    """`input_paths` and `for_revision_paths` are resolved by the engine from
    declared artifact roles and the work-product ledger. Leaving either
    caller-writable is how a model came to be the source of a file path at all
    — the failure that started this whole line of work (doc/FINDINGS.md).

    Which agents this covers is read off the live specs, not listed here: the
    rule is "declares roles", and a new agent joins it by declaring them."""
    resolved = [
        (name, tool) for name, tool in _tools_by_agent().items() if _SPECS_BY_NAME[name].consumes
    ]
    assert resolved, "expected at least one role-declaring agent to be callable"
    for _name, tool in resolved:
        properties = tool.input_schema["properties"]
        required = tool.input_schema["required"]
        for field_name in ENGINE_OWNED_TASK_FIELDS:
            assert field_name not in properties, f"{tool.name}: {field_name}"  # type: ignore[operator]
            assert field_name not in required, f"{tool.name}: {field_name}"  # type: ignore[operator]


def test_an_agent_with_no_declared_roles_keeps_its_callers_input_paths() -> None:
    """Hiding a field is only right where something else fills it in.

    An agent that declares no `consumes` has no resolution behind it, so
    stripping `input_paths` would leave nobody able to name a file: not the
    caller (the field is gone from its tool) and not the engine (there are no
    roles to resolve). `developer` is that agent — the Problem Solver has
    already read the tree and points it at what to edit."""
    unresolved = [
        (name, tool)
        for name, tool in _tools_by_agent().items()
        if not _SPECS_BY_NAME[name].consumes
        and "input_paths" in _SPECS_BY_NAME[name].input_schema.get("properties", {})  # type: ignore[union-attr]
    ]
    assert unresolved, (
        "no callable agent declares input_paths without consumes — if that is "
        "deliberate, this test and the engine_resolves_inputs branch it guards "
        "are both dead and should go"
    )
    for name, tool in unresolved:
        assert "input_paths" in tool.input_schema["properties"], name  # type: ignore[operator]


def test_the_sub_agents_own_schema_still_documents_the_engine_owned_fields() -> None:
    """Stripped from the *caller's* tool, kept on the agent's own contract: that
    is what gives each field a description in the rendered task brief, so the
    agent reading it knows what it was handed."""
    for name, spec in _SPECS_BY_NAME.items():
        if not spec.consumes:
            continue
        properties = spec.input_schema.get("properties")
        assert isinstance(properties, dict)
        assert "input_paths" in properties, name
        description = properties["input_paths"]["description"]  # type: ignore[index]
        assert "engine" in str(description).lower(), name


def _per_component_specs() -> dict[str, SubAgentSpec]:
    """The specs that run once per component — read off the live specs.

    Which agents those are is the specs' business, not this test's; what is
    pinned below is the invariants that hold whatever the set turns out to be.
    """
    per_component = {
        name: spec for name, spec in _SPECS_BY_NAME.items() if spec.takes_responsibility_code
    }
    assert per_component, "expected at least one per-component spec to declare a responsibility"
    return per_component


def test_only_a_per_component_spec_declares_a_responsibility_code() -> None:
    """``responsibility_code`` is the third field the engine has the last word
    on. A product-level agent that receives one has its work product split
    across calls — so it is not merely unused there, it is harmful, and the
    schema does not offer it. ``takes_responsibility_code`` is what the engine
    reads, so it must agree with the schema in both directions."""
    per_component = _per_component_specs()
    for name, spec in _SPECS_BY_NAME.items():
        properties = spec.input_schema.get("properties")
        required = spec.input_schema.get("required")
        assert isinstance(properties, dict)
        assert isinstance(required, list)
        declared = RESPONSIBILITY_CODE_KEY in properties
        assert declared is (name in per_component), name
        # Declared and optional would be the same footgun in a smaller shape:
        # a caller free to omit the component of a per-component run.
        assert (RESPONSIBILITY_CODE_KEY in required) is declared, name


def test_no_critic_declares_a_responsibility_code() -> None:
    """A critic's task is built by the engine — ``instructions`` plus resolved
    ``input_paths``, nothing else — so it never receives one. Its ``self``-scoped
    needs are narrowed from the work product under review, whose own
    responsibility the round already knows."""
    registry = AgentRegistry(_AGENTS_DIR)
    critics = [
        agent.name
        for agent in registry.all_agents()
        if agent.name in _SPECS_BY_NAME and agent.is_critic
    ]
    assert critics, "expected the registry to load some critics"
    for name in critics:
        assert not _SPECS_BY_NAME[name].takes_responsibility_code, name


def test_no_generated_tool_offers_a_responsibility_code_to_a_product_level_stage() -> None:
    """The caller-facing half: a Guide that is never shown the field cannot pass
    one to stage 4 and split its record. The prompt used to be the only thing
    standing between it and that mistake."""
    registry = AgentRegistry(_AGENTS_DIR)
    per_component = _per_component_specs()
    seen = set()
    for caller in ("guide", "problem_solver"):
        for tool in registry.run_subagent_specs(caller):
            target = tool.name[len("run_subagent_") :]
            offered = RESPONSIBILITY_CODE_KEY in tool.input_schema["properties"]  # type: ignore[operator]
            assert offered is (target in per_component), tool.name
            if offered:
                seen.add(target)
    assert seen, "expected the guide to be offered the per-component stages"
