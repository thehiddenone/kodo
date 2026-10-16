"""Agent Builder's service — doc/AGENT_BUILDER.md §6.2.

Every test runs the real :class:`~kodo.agents.AgentWorkshop` over the real
packaged agents and a temporary user root, so "the agent loads" means what it
means in production: the registry, reloaded after the write, accepts it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.agents import (
    KIND_AGENT,
    KIND_SUBAGENT,
    PACKAGED_AGENTS_DIR,
    RULE_SECTIONS,
    SOURCE_BUILTIN,
    SOURCE_USER,
    AgentRegistry,
    AgentWorkshop,
    AgentWorkshopError,
)

_SUBAGENT = """---
name: acme_summarizer
display_name: Summarizer
tools:
  - read_file
---
# Summarizer

You are **Summarizer**: you summarize one file into three bullet points.

## Purpose

Summarizer turns one file into three bullet points.

## Inputs

- **`instructions`** — the file to summarize.

## Workflow

1. Read the file with `read_file`.

## Reporting

Call `return_result` once, last.

## What to Avoid

- Free-form output in place of `return_result`.

{SHARED:working_rules}

{SHARED:security}
"""

_SPEC: dict[str, object] = {
    "input_schema": {
        "shape": "raw",
        "schema": {"type": "object", "properties": {"instructions": {"type": "string"}}},
    },
    "output_schema": {
        "shape": "raw",
        "schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
    },
}

_TOP = """---
name: acme_helper
display_name: Helper
tools:
  - run_subagent
subagents:
  - acme_summarizer
---
# Helper

You are **Helper**: you summarize the files the user names.

## Workflow

1. Call `run_subagent_acme_summarizer` once per file.

## What to Avoid

- Free-form output in place of a tool call.

{SHARED:working_rules}

{SHARED:security}
"""

_CONFIG: dict[str, object] = {"label": "Helper", "description": "Summarizes files.", "rank": 50}


@pytest.fixture
def registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        PACKAGED_AGENTS_DIR, skills_dir=tmp_path / "skills", user_dir=tmp_path / "agents"
    )


@pytest.fixture
def workshop(registry: AgentRegistry) -> AgentWorkshop:
    return AgentWorkshop(registry)


async def _write_pipeline(workshop: AgentWorkshop) -> None:
    await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, _SPEC)
    await workshop.write_agent(KIND_AGENT, "acme_helper", _TOP, _CONFIG)


# ---------------------------------------------------------------------------
# write_agent
# ---------------------------------------------------------------------------


async def test_written_pipeline_loads_and_reaches_the_picker(
    workshop: AgentWorkshop, registry: AgentRegistry
) -> None:
    await _write_pipeline(workshop)

    assert {"acme_summarizer", "acme_helper"} <= registry.user_agent_names
    assert registry.knows_top_agent("acme_helper")
    assert registry.spec_for("acme_summarizer") is not None


async def test_write_reports_a_cross_agent_load_error_until_the_callee_exists(
    workshop: AgentWorkshop,
) -> None:
    first = await workshop.write_agent(KIND_AGENT, "acme_helper", _TOP, _CONFIG)
    assert first["report"]["load"] == "error"  # type: ignore[index]
    assert "acme_summarizer" in str(first["report"]["error"])  # type: ignore[index]

    await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, _SPEC)

    report = await workshop.validate_agent("acme_helper")
    assert report["load"] == "ok"


async def test_write_fills_in_the_config_name(workshop: AgentWorkshop) -> None:
    result = await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, _SPEC)

    spec_path = next(Path(p) for p in result["files"] if str(p).endswith(".json"))  # type: ignore[attr-defined]
    assert json.loads(spec_path.read_text(encoding="utf-8"))["name"] == "acme_summarizer"


async def test_overwrite_replaces_an_existing_user_agent(
    workshop: AgentWorkshop, registry: AgentRegistry
) -> None:
    await _write_pipeline(workshop)

    await workshop.write_agent(
        KIND_AGENT,
        "acme_helper",
        _TOP.replace("display_name: Helper", "display_name: Aide"),
        _CONFIG,
    )

    assert registry.get("acme_helper").display_name == "Aide"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("kodo_helper", "reserved prefix"),
        ("subagents", "reserved"),
        ("Acme", "not a valid agent name"),
        ("acme/../x", "not a valid agent name"),
        ("a" + "b" * 60, "too long"),
    ],
)
async def test_write_refuses_a_name_kodo_would_not_write(
    workshop: AgentWorkshop, tmp_path: Path, name: str, reason: str
) -> None:
    with pytest.raises(AgentWorkshopError, match=reason):
        await workshop.write_agent(KIND_AGENT, name, _TOP, _CONFIG)
    assert not (tmp_path / "agents").exists()


async def test_write_refuses_a_name_the_other_kind_uses(workshop: AgentWorkshop) -> None:
    await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, _SPEC)

    with pytest.raises(AgentWorkshopError, match="one namespace"):
        await workshop.write_agent(
            KIND_AGENT, "acme_summarizer", _TOP.replace("acme_helper", "acme_summarizer"), _CONFIG
        )


async def test_write_refuses_a_prompt_its_loader_rejects(
    workshop: AgentWorkshop, tmp_path: Path
) -> None:
    mismatched = _SUBAGENT.replace("name: acme_summarizer", "name: acme_other")

    with pytest.raises(AgentWorkshopError, match="does not load"):
        await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", mismatched, _SPEC)
    assert not (tmp_path / "agents" / "subagents" / "acme_summarizer").exists()


async def test_write_refuses_a_spec_with_an_unknown_key(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError, match="does not load"):
        await workshop.write_agent(
            KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, {**_SPEC, "produce": {}}
        )


async def test_write_refuses_a_config_name_that_disagrees(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError, match="must agree"):
        await workshop.write_agent(
            KIND_AGENT, "acme_helper", _TOP, {**_CONFIG, "name": "acme_other"}
        )


@pytest.mark.parametrize("claim", [{"default": True}, {"interactive": False, "selectable": False}])
async def test_write_refuses_what_only_a_builtin_can_declare(
    workshop: AgentWorkshop, claim: dict[str, object]
) -> None:
    with pytest.raises(AgentWorkshopError, match="built-in|user agent cannot"):
        await workshop.write_agent(KIND_AGENT, "acme_helper", _TOP, {**_CONFIG, **claim})


async def test_write_refuses_an_unknown_kind(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError, match="kind must be"):
        await workshop.write_agent("critic", "acme_helper", _TOP, _CONFIG)


async def test_a_registry_without_a_user_root_refuses_writes() -> None:
    workshop = AgentWorkshop(AgentRegistry(PACKAGED_AGENTS_DIR))

    with pytest.raises(AgentWorkshopError, match="no user agents directory"):
        await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, _SPEC)


# ---------------------------------------------------------------------------
# edit_agent
# ---------------------------------------------------------------------------


async def test_edit_changes_one_file_and_reports(
    workshop: AgentWorkshop, registry: AgentRegistry
) -> None:
    await _write_pipeline(workshop)

    result = await workshop.edit_agent(
        "acme_helper", "config", '"label": "Helper"', '"label": "Files Helper"'
    )

    assert result["report"]["load"] == "ok"  # type: ignore[index]
    assert next(t.label for t in registry.top_agents() if t.name == "acme_helper") == "Files Helper"


async def test_edit_refuses_an_ambiguous_or_absent_old_string(workshop: AgentWorkshop) -> None:
    await _write_pipeline(workshop)

    with pytest.raises(AgentWorkshopError, match="occurs 4 times"):
        await workshop.edit_agent("acme_summarizer", "prompt", "Summarizer", "Digest")
    with pytest.raises(AgentWorkshopError, match="does not occur"):
        await workshop.edit_agent("acme_summarizer", "prompt", "nowhere in the file", "x")
    with pytest.raises(AgentWorkshopError, match="empty"):
        await workshop.edit_agent("acme_summarizer", "prompt", "", "x")


async def test_edit_refuses_a_result_its_loader_rejects(workshop: AgentWorkshop) -> None:
    await _write_pipeline(workshop)
    before = (await workshop.read_agent("acme_summarizer"))["json"]

    with pytest.raises(AgentWorkshopError, match="does not load"):
        await workshop.edit_agent("acme_summarizer", "spec", '"output_schema"', '"outputs"')

    assert (await workshop.read_agent("acme_summarizer"))["json"] == before


async def test_edit_refuses_the_wrong_json_for_the_kind(workshop: AgentWorkshop) -> None:
    await _write_pipeline(workshop)

    with pytest.raises(AgentWorkshopError, match="'spec'"):
        await workshop.edit_agent("acme_summarizer", "config", "x", "y")
    with pytest.raises(AgentWorkshopError, match="'config'"):
        await workshop.edit_agent("acme_helper", "spec", "x", "y")


async def test_edit_refuses_builtins_and_unknown_agents(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError, match="read-only"):
        await workshop.edit_agent("kodo_guide", "prompt", "Guide", "Guidance")
    with pytest.raises(AgentWorkshopError, match="no user agent"):
        await workshop.edit_agent("acme_missing", "prompt", "x", "y")


async def test_edit_can_repair_a_broken_agent(
    workshop: AgentWorkshop, registry: AgentRegistry
) -> None:
    await workshop.write_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT, _SPEC)
    await workshop.write_agent(
        KIND_AGENT, "acme_helper", _TOP.replace("acme_summarizer", "acme_sumariser"), _CONFIG
    )
    assert "acme_helper" in {b.name for b in registry.broken_agents}

    result = await workshop.edit_agent(
        "acme_helper", "prompt", "  - acme_sumariser", "  - acme_summarizer"
    )

    # The body still names the misspelled tool, which the lint reports; the
    # frontmatter fix alone is what makes it load.
    assert result["report"]["load"] == "ok"  # type: ignore[index]


# ---------------------------------------------------------------------------
# list_agents / read_agent / validate_agent / lint_agent
# ---------------------------------------------------------------------------


async def test_listing_has_builtins_users_and_broken_rows(
    workshop: AgentWorkshop, tmp_path: Path
) -> None:
    await _write_pipeline(workshop)
    broken = tmp_path / "agents" / "acme_broken"
    broken.mkdir()
    await workshop.write_agent(
        KIND_SUBAGENT, "acme_other", _SUBAGENT.replace("acme_summarizer", "acme_other"), _SPEC
    )

    listed = await workshop.list_agents()
    rows = {row["name"]: row for row in listed["agents"]}  # type: ignore[union-attr]

    assert rows["kodo_problem_solver"]["source"] == SOURCE_BUILTIN
    assert rows["kodo_problem_solver"]["copyable"] is True
    assert rows["kodo_compactor"]["copyable"] is False
    assert rows["acme_helper"]["source"] == SOURCE_USER
    assert rows["acme_helper"]["kind"] == KIND_AGENT
    assert rows["acme_helper"]["subagents"] == ["acme_summarizer"]
    assert rows["acme_helper"]["selectable"] is True
    assert (
        rows["acme_summarizer"]["description"]
        == "Summarizer turns one file into three bullet points."
    )
    assert rows["acme_broken"]["error"]
    assert listed["root"] == str(tmp_path / "agents")


async def test_read_returns_files_as_written(workshop: AgentWorkshop) -> None:
    await _write_pipeline(workshop)

    builtin = await workshop.read_agent("kodo_investigator")
    user = await workshop.read_agent("acme_helper")

    assert builtin["source"] == SOURCE_BUILTIN
    assert "{SHARED:task_input}" in str(builtin["prompt"])
    assert json.loads(str(builtin["json"]))["name"] == "kodo_investigator"
    assert user["prompt"] == _TOP
    assert user["frontmatter"]["subagents"] == ["acme_summarizer"]  # type: ignore[index]


async def test_read_works_on_a_broken_agent(workshop: AgentWorkshop) -> None:
    await workshop.write_agent(KIND_AGENT, "acme_helper", _TOP, _CONFIG)

    read = await workshop.read_agent("acme_helper")

    assert read["error"]
    assert read["prompt"] == _TOP


async def test_read_and_validate_refuse_an_unknown_agent(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError, match="list_agents"):
        await workshop.read_agent("acme_missing")
    with pytest.raises(AgentWorkshopError, match="list_agents"):
        await workshop.validate_agent("acme_missing")


async def test_lint_checks_a_draft_without_writing_it(
    workshop: AgentWorkshop, tmp_path: Path
) -> None:
    clean = await workshop.lint_agent(KIND_SUBAGENT, "acme_summarizer", _SUBAGENT)
    missing = await workshop.lint_agent(
        KIND_SUBAGENT, "acme_summarizer", _SUBAGENT.replace("## Reporting", "## Output")
    )

    assert clean["findings"] == []
    assert [f["rule"] for f in missing["findings"]] == [RULE_SECTIONS]  # type: ignore[index, union-attr]
    assert not (tmp_path / "agents").exists()


async def test_lint_refuses_a_draft_that_does_not_parse(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError, match="does not parse"):
        await workshop.lint_agent(KIND_SUBAGENT, "acme_summarizer", "no frontmatter")


# ---------------------------------------------------------------------------
# copy_builtin
# ---------------------------------------------------------------------------


async def test_copy_asks_before_overwriting_and_then_obeys(
    workshop: AgentWorkshop, registry: AgentRegistry
) -> None:
    first = await workshop.copy_builtin("kodo_problem_solver", "acme_", None)
    assert first["done"] is True
    assert "acme_problem_solver" in first["copied"]  # type: ignore[operator]
    assert registry.knows_top_agent("acme_problem_solver")

    unanswered = await workshop.copy_builtin("kodo_problem_solver", "acme_", None)
    assert unanswered["done"] is False
    assert "acme_problem_solver" in str(unanswered["conflicts"])
    assert unanswered["copied"] == []

    kept = await workshop.copy_builtin("kodo_problem_solver", "acme_", False)
    assert kept["copied"] == []
    assert "acme_problem_solver" in kept["kept"]  # type: ignore[operator]


async def test_copy_refuses_what_cannot_be_copied(workshop: AgentWorkshop) -> None:
    with pytest.raises(AgentWorkshopError):
        await workshop.copy_builtin("kodo_compactor", "acme_", None)
    with pytest.raises(AgentWorkshopError, match="reserved"):
        await workshop.copy_builtin("kodo_guide", "kodo_x_", None)
