"""The deterministic prompt lint and the per-agent report (doc/AGENT_BUILDER.md §5.1, §5.2).

The lint is pure, so most tests here write one prompt file, parse it with the
real loader, and assert on the findings for exactly that input. The last
section drives :class:`~kodo.agents.AgentValidator` over a real registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.agents import (
    ENGINE_DRIVEN_AGENTS,
    KIND_AGENT,
    KIND_SUBAGENT,
    LINT_RULES,
    PACKAGED_AGENTS_DIR,
    RULE_PURPOSE,
    RULE_SCHEMA_RESTATED,
    RULE_SECTIONS,
    RULE_SUBAGENT_NOT_LISTED,
    RULE_TOOL_NOT_GRANTED,
    RULE_UNKNOWN_AGENT,
    RULE_VAGUE_QUALIFIER,
    SEVERITY_WARNING,
    AgentRegistry,
    AgentValidator,
    LintContext,
    LintFinding,
    lint_prompt,
    load_agent,
)
from kodo.toolspecs import ALL_TOOLS

_TOOLS = frozenset(tool.name for tool in ALL_TOOLS)
_CONTEXT = LintContext(
    tool_names=_TOOLS,
    agent_names=frozenset({"kodo_coder", "helper", "worker"}),
    broken_names=frozenset({"wreck"}),
)

_SECTIONS = "## Inputs\n\nx\n\n## Workflow\n\nx\n\n## Reporting\n\nx\n\n## What to Avoid\n\nx\n"
_CLEAN_BODY = f"# Worker\n\nYou are **Worker**, which does one job.\n\n{_SECTIONS}"


def _lint(
    tmp_path: Path,
    body: str,
    *,
    frontmatter: str = "tools:\n  - read_file\n",
    name: str = "worker",
    top_level: bool = False,
    authors: frozenset[str] = frozenset(),
    context: LintContext = _CONTEXT,
) -> list[LintFinding]:
    """Write one prompt, parse it, and lint it with its declared tools as the grant."""
    stem = f"agent_{name}" if top_level else f"subagent_{name}"
    path = tmp_path / f"{stem}.md"
    text = f"---\nname: {name}\n{frontmatter}---\n{body}"
    path.write_text(text, encoding="utf-8")
    agent = load_agent(path)
    return list(lint_prompt(agent, text, agent.tools, context, authors=authors))


def _only(findings: list[LintFinding], rule: str) -> list[LintFinding]:
    return [f for f in findings if f.rule == rule]


def _ungranted_tool() -> str:
    return sorted(_TOOLS - {"read_file"})[0]


def test_a_prompt_that_follows_every_rule_has_no_findings(tmp_path: Path) -> None:
    assert _lint(tmp_path, _CLEAN_BODY) == []


def test_every_finding_is_a_warning(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "Maybe. Two sentences.\n")
    assert findings
    assert {f.severity for f in findings} == {SEVERITY_WARNING}
    assert {f.rule for f in findings} <= set(LINT_RULES)


def test_a_prompt_without_frontmatter_is_not_linted() -> None:
    agent_text = "# Worker\n\nMaybe.\n"
    agent = load_agent(PACKAGED_AGENTS_DIR / "agent_kodo_problem_solver.md")
    assert lint_prompt(agent, agent_text, frozenset(), _CONTEXT) == ()


# ---------------------------------------------------------------------------
# L1 tool-not-granted
# ---------------------------------------------------------------------------


def test_naming_a_tool_the_agent_lacks_is_flagged_with_its_line(tmp_path: Path) -> None:
    tool = _ungranted_tool()
    findings = _only(_lint(tmp_path, f"{_CLEAN_BODY}\nCall `{tool}` now.\n"), RULE_TOOL_NOT_GRANTED)
    assert len(findings) == 1
    assert tool in findings[0].message
    # 5 frontmatter lines (---, name, tools, - read_file, ---) precede the body.
    assert findings[0].line == 5 + _CLEAN_BODY.count("\n") + 2


def test_naming_a_granted_tool_or_a_non_tool_is_not_flagged(tmp_path: Path) -> None:
    body = f"{_CLEAN_BODY}\nCall `read_file`, set `max_depth`.\n"
    assert _only(_lint(tmp_path, body), RULE_TOOL_NOT_GRANTED) == []


def test_a_tool_named_inside_a_fenced_block_is_not_flagged(tmp_path: Path) -> None:
    body = f"{_CLEAN_BODY}\n```\n`{_ungranted_tool()}`\n```\n"
    assert _only(_lint(tmp_path, body), RULE_TOOL_NOT_GRANTED) == []


# ---------------------------------------------------------------------------
# L2 subagent-not-listed
# ---------------------------------------------------------------------------


def test_a_run_subagent_call_outside_the_allow_list_is_flagged(tmp_path: Path) -> None:
    frontmatter = "tools:\n  - run_subagent\nsubagents:\n  - helper\n"
    body = f"{_CLEAN_BODY}\nUse run_subagent_helper, then `run_subagent_stranger`.\n"
    findings = _only(_lint(tmp_path, body, frontmatter=frontmatter), RULE_SUBAGENT_NOT_LISTED)
    assert [f.message for f in findings] == [
        "names run_subagent_stranger, but 'stranger' is not in subagents:"
    ]


def test_an_agent_may_name_its_own_tool(tmp_path: Path) -> None:
    body = f"{_CLEAN_BODY}\nYou run inside `run_subagent_worker`.\n"
    assert _only(_lint(tmp_path, body), RULE_SUBAGENT_NOT_LISTED) == []


def test_a_critic_may_name_the_tool_of_the_author_it_reviews_for(tmp_path: Path) -> None:
    body = "# Worker\n\nYou review.\n\nThe engine spawns you inside `run_subagent_helper`.\n"
    findings = _lint(tmp_path, body, frontmatter="role: critic\n", authors=frozenset({"helper"}))
    assert _only(findings, RULE_SUBAGENT_NOT_LISTED) == []


# ---------------------------------------------------------------------------
# L3 unknown-agent
# ---------------------------------------------------------------------------


def test_a_backticked_builtin_name_that_is_not_loaded_is_flagged(tmp_path: Path) -> None:
    body = f"{_CLEAN_BODY}\nAsk `kodo_ghost`, not `kodo_coder`.\n"
    findings = _only(_lint(tmp_path, body), RULE_UNKNOWN_AGENT)
    assert len(findings) == 1
    assert "kodo_ghost" in findings[0].message


def test_a_backticked_user_agent_that_failed_to_load_is_flagged(tmp_path: Path) -> None:
    body = f"{_CLEAN_BODY}\nAsk `wreck` or `helper`.\n"
    findings = _only(_lint(tmp_path, body), RULE_UNKNOWN_AGENT)
    assert len(findings) == 1
    assert "wreck" in findings[0].message


# ---------------------------------------------------------------------------
# L4 purpose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opening",
    [
        "# Worker\n\nYou are Worker, which does one job.\n",
        "# Worker\nYou are Worker, which does one job.\n",
        "**Worker**\n\nYou are Worker, which does one job (e.g. this one).\n",
        "You are Worker, which does one job.\n",
        "# Worker\n\nYou are **Worker** — you call `read_file.` once.\n",
    ],
)
def test_one_purpose_sentence_after_the_title_passes(opening: str, tmp_path: Path) -> None:
    assert _only(_lint(tmp_path, f"{opening}\n{_SECTIONS}"), RULE_PURPOSE) == []


@pytest.mark.parametrize(
    "opening",
    [
        "# Worker\n\nYou are **Worker**. You do one job.\n",
        "# Worker\n\nYou do one job! Then stop.\n",
        "# Worker\n\n- a list, not a sentence\n",
        "# Worker\n\n## Inputs\n",
        "# Worker\n",
    ],
)
def test_anything_but_one_sentence_after_the_title_is_flagged(opening: str, tmp_path: Path) -> None:
    assert len(_only(_lint(tmp_path, f"{opening}\n{_SECTIONS}"), RULE_PURPOSE)) == 1


# ---------------------------------------------------------------------------
# L5 sections (D26)
# ---------------------------------------------------------------------------


def _missing_sections(findings: list[LintFinding]) -> set[str]:
    return {f.message.split("'")[1].removeprefix("## ") for f in _only(findings, RULE_SECTIONS)}


def test_a_sub_agent_needs_all_four_sections(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "# Worker\n\nOne job.\n")
    assert _missing_sections(findings) == {"Inputs", "Workflow", "Reporting", "What to Avoid"}
    assert all(f.line is None for f in _only(findings, RULE_SECTIONS))


def test_a_critic_needs_inputs_reporting_and_what_to_avoid(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "# Worker\n\nOne job.\n", frontmatter="role: critic\n")
    assert _missing_sections(findings) == {"Inputs", "Reporting", "What to Avoid"}


def test_a_top_level_agent_needs_workflow_and_what_to_avoid(tmp_path: Path) -> None:
    findings = _lint(tmp_path, "# Worker\n\nOne job.\n", top_level=True)
    assert _missing_sections(findings) == {"Workflow", "What to Avoid"}


@pytest.mark.parametrize("name", sorted(ENGINE_DRIVEN_AGENTS))
def test_an_engine_driven_agent_is_exempt_from_sections(name: str, tmp_path: Path) -> None:
    findings = _lint(tmp_path, "# Worker\n\nOne job.\n", name=name)
    assert _only(findings, RULE_SECTIONS) == []


def test_a_section_heading_must_match_exactly(tmp_path: Path) -> None:
    body = _CLEAN_BODY.replace("## What to Avoid", "## What to avoid")
    assert _missing_sections(_lint(tmp_path, body)) == {"What to Avoid"}


def test_a_heading_inside_a_fenced_block_does_not_count(tmp_path: Path) -> None:
    body = _CLEAN_BODY.replace("## Workflow\n", "```\n## Workflow\n```\n")
    assert _missing_sections(_lint(tmp_path, body)) == {"Workflow"}


# ---------------------------------------------------------------------------
# L6 vague-qualifier (D29)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    [
        "almost",
        "possibly",
        "maybe",
        "appropriate",
        "reasonable",
        "as needed",
        "may",
        "could",
        "should consider",
        "May",
        "may not",
    ],
)
def test_a_vague_qualifier_in_prose_is_flagged(word: str, tmp_path: Path) -> None:
    findings = _only(_lint(tmp_path, f"{_CLEAN_BODY}\nYou {word} retry.\n"), RULE_VAGUE_QUALIFIER)
    assert len(findings) == 1
    assert findings[0].line is not None


@pytest.mark.parametrize(
    "text",
    [
        "Set `may` to true.",
        "```\nyou may retry\n```",
        "> The user may write anything.",
        'Reject the phrase "you may retry" outright.',
        "Reject the phrase “you could retry” outright.",
        "The mayor and the maybelline are fine.",
    ],
)
def test_exempt_text_is_not_flagged(text: str, tmp_path: Path) -> None:
    assert _only(_lint(tmp_path, f"{_CLEAN_BODY}\n{text}\n"), RULE_VAGUE_QUALIFIER) == []


def test_every_occurrence_is_reported_on_its_own_line(tmp_path: Path) -> None:
    findings = _only(
        _lint(tmp_path, f"{_CLEAN_BODY}\nYou may go.\nYou could stay, maybe.\n"),
        RULE_VAGUE_QUALIFIER,
    )
    lines = [f.line for f in findings]
    assert len(lines) == 3
    assert lines[1] == lines[2] == lines[0] + 1  # type: ignore[operator]


# ---------------------------------------------------------------------------
# L7 schema-restated
# ---------------------------------------------------------------------------


def test_a_fenced_json_schema_is_flagged_at_its_fence(tmp_path: Path) -> None:
    schema = json.dumps({"type": "object", "properties": {"x": {"type": "string"}}}, indent=2)
    findings = _only(
        _lint(tmp_path, f"{_CLEAN_BODY}\n```json\n{schema}\n```\n"), RULE_SCHEMA_RESTATED
    )
    assert len(findings) == 1
    text = f"---\nname: worker\ntools:\n  - read_file\n---\n{_CLEAN_BODY}\n```json\n"
    assert findings[0].line == text.count("\n")


def test_a_fenced_json_example_is_not_a_schema(tmp_path: Path) -> None:
    body = f'{_CLEAN_BODY}\n```json\n{{"type": "object", "name": "x"}}\n```\n'
    assert _only(_lint(tmp_path, body), RULE_SCHEMA_RESTATED) == []


# ---------------------------------------------------------------------------
# AgentValidator — the per-agent report
# ---------------------------------------------------------------------------


@pytest.fixture
def user_root(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    return root


def _write_user_agent(root: Path, name: str, body: str) -> None:
    directory = root / name
    directory.mkdir()
    (directory / f"agent_{name}.md").write_text(
        f"---\nname: {name}\ntools:\n  - read_file\n---\n{body}\n", encoding="utf-8"
    )
    (directory / f"{name}.json").write_text(
        json.dumps({"name": name, "description": "d"}), encoding="utf-8"
    )


def _validator(user_root: Path) -> AgentValidator:
    return AgentValidator(AgentRegistry(PACKAGED_AGENTS_DIR, user_dir=user_root))


def test_a_builtin_gets_a_report_that_says_it_loads(user_root: Path) -> None:
    validator = _validator(user_root)
    name = sorted(validator.context.agent_names)[0]
    report = validator.validate(name)
    assert report is not None
    assert report.loads
    assert report.load_error == ""
    assert report.path is not None and report.path.is_file()


def test_a_healthy_user_agent_reports_its_lint_findings(user_root: Path) -> None:
    _write_user_agent(user_root, "helper", "# Helper\n\nYou may help. Twice.\n")
    report = _validator(user_root).validate("helper")
    assert report is not None
    assert report.kind == KIND_AGENT
    assert report.loads
    assert {RULE_PURPOSE, RULE_SECTIONS, RULE_VAGUE_QUALIFIER} <= {f.rule for f in report.findings}


def test_a_broken_user_agent_reports_why_and_still_gets_its_prompt_linted(
    user_root: Path,
) -> None:
    _write_user_agent(user_root, "wreck", "# Wreck\n\nYou may help.\n\n{SHARED:no_such_block}\n")
    report = _validator(user_root).validate("wreck")
    assert report is not None
    assert not report.loads
    assert "no_such_block" in report.load_error
    assert RULE_VAGUE_QUALIFIER in {f.rule for f in report.findings}


def test_a_broken_user_agent_whose_prompt_does_not_parse_reports_only_why(
    user_root: Path,
) -> None:
    directory = user_root / "wreck"
    directory.mkdir()
    (directory / "agent_wreck.md").write_text("no frontmatter", encoding="utf-8")
    report = _validator(user_root).validate("wreck")
    assert report is not None
    assert not report.loads
    assert report.findings == ()


def test_an_unknown_name_has_no_report(user_root: Path) -> None:
    assert _validator(user_root).validate("nobody") is None


def test_a_user_subagent_is_reported_as_a_subagent(user_root: Path) -> None:
    directory = user_root / "subagents" / "aide"
    directory.mkdir(parents=True)
    (directory / "subagent_aide.md").write_text(
        "---\nname: aide\ntools:\n  - read_file\n---\n# Aide\n\nHelps.\n\n## Purpose\n\nHelps.\n",
        encoding="utf-8",
    )
    spec = {
        "name": "aide",
        "input_schema": {"shape": "raw", "schema": {"type": "object", "properties": {}}},
        "output_schema": {"shape": "raw", "schema": {"type": "object", "properties": {}}},
    }
    (directory / "aide.json").write_text(json.dumps(spec), encoding="utf-8")
    report = _validator(user_root).validate("aide")
    assert report is not None
    assert report.kind == KIND_SUBAGENT


def test_the_report_payload_is_the_wire_shape(user_root: Path) -> None:
    _write_user_agent(user_root, "helper", "# Helper\n\nHelps.\n")
    report = _validator(user_root).validate("helper")
    assert report is not None
    payload = report.as_payload()
    assert payload["name"] == "helper"
    assert payload["kind"] == KIND_AGENT
    assert payload["load"] == "ok"
    assert payload["error"] == ""
    assert payload["path"] == str(user_root / "helper" / "agent_helper.md")
    assert payload["findings"] == [
        {"rule": f.rule, "severity": f.severity, "line": f.line, "message": f.message}
        for f in report.findings
    ]
