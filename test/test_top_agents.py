"""Tests for the top-level agent configs — ``kodo.agents``.

Two layers: :func:`kodo.agents.load_top_agent` on one file, and
:class:`kodo.agents.AgentRegistry`'s cross-checks over the whole set, which
are what actually keep a prompt and its config from drifting apart.

The registry fixtures here build a *complete* miniature agent directory — shared
blocks, one ``agent_<name>.md``, one ``<name>.json`` beside it — because both
halves of every check under test come from the same directory. That is the
property that makes the pairing checkable at all (see the loader's module
docstring), so the fixtures have to honour it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.agents import (
    SUBAGENTS_SUBDIR,
    AgentLoadError,
    AgentRegistry,
    TopAgent,
    TopAgentLoadError,
    load_top_agent,
    load_top_agents,
)
from kodo.agents.subagents import SubAgentSpec

_REAL_AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "kodo" / "agents"

_BODY = "Do the thing.\n\n{SHARED:working_rules}\n{SHARED:security}\n"


def _write_config(tmp_path: Path, name: str, **overrides: object) -> Path:
    """Write ``<name>.json``, the config beside a top-level agent's prompt."""
    payload: dict[str, object] = {
        "name": name,
        "notes": "fixture",
        "description": f"The {name} agent.",
        "rank": 10,
        "default": True,
    }
    payload.update(overrides)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_top_agent(tmp_path: Path, name: str, frontmatter: str = "") -> Path:
    """Write ``agent_<name>.md`` — the prompt half of a top-level agent."""
    path = tmp_path / f"agent_{name}.md"
    path.write_text(f"---\nname: {name}\n{frontmatter}---\n{_BODY}", encoding="utf-8")
    return path


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """A minimal agent directory: the two mandatory shared blocks, nothing else."""
    (tmp_path / "shared_security.md").write_text("## Absolute Rules\n", encoding="utf-8")
    (tmp_path / "shared_working_rules.md").write_text("## How You Work\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# load_top_agent — one file
# ---------------------------------------------------------------------------


def test_load_reads_every_field(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "reviewer",
        label="Reviewer",
        rank=42,
        selectable=False,
        default=False,
    )
    cfg = load_top_agent(path)
    assert cfg == TopAgent(
        name="reviewer",
        label="Reviewer",
        description="The reviewer agent.",
        rank=42,
        selectable=False,
        default=False,
        notes="fixture",
    )


def test_load_defaults_everything_optional(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.json"
    path.write_text(json.dumps({"name": "reviewer", "description": "x"}), encoding="utf-8")
    cfg = load_top_agent(path)
    # `label` stays empty on purpose — only the registry knows the agent's
    # display_name to fall back to.
    assert (cfg.label, cfg.rank, cfg.selectable, cfg.default) == ("", 0, True, False)


def test_load_rejects_an_unknown_key(tmp_path: Path) -> None:
    """A silently-ignored typo would ship an agent in a picker it opted out of."""
    path = _write_config(tmp_path, "reviewer", selectible=False)
    with pytest.raises(TopAgentLoadError, match="unknown key"):
        load_top_agent(path)


def test_load_rejects_a_name_that_disagrees_with_the_filename(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "reviewer")
    path.rename(path.with_name("auditor.json"))
    with pytest.raises(TopAgentLoadError, match="filename stem"):
        load_top_agent(path.with_name("auditor.json"))


def test_load_requires_a_description(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "reviewer", description="")
    with pytest.raises(TopAgentLoadError, match="description"):
        load_top_agent(path)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("rank", "10", "'rank' must be an integer"),
        ("rank", True, "'rank' must be an integer"),
        ("selectable", "yes", "'selectable' must be true or false"),
        ("default", 1, "'default' must be true or false"),
        ("label", 3, "'label' must be a string"),
    ],
)
def test_load_rejects_a_wrong_type(tmp_path: Path, key: str, value: object, match: str) -> None:
    path = _write_config(tmp_path, "reviewer", **{key: value})
    with pytest.raises(TopAgentLoadError, match=match):
        load_top_agent(path)


def test_load_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TopAgentLoadError, match="not valid JSON"):
        load_top_agent(path)


def test_load_top_agents_tolerates_a_missing_directory(tmp_path: Path) -> None:
    """A registry over a directory of sub-agents has no configs, not an error."""
    assert load_top_agents(tmp_path / "nope") == ()


# ---------------------------------------------------------------------------
# The shipped set
# ---------------------------------------------------------------------------


def test_every_shipped_top_agent_pairs_a_prompt_with_a_config() -> None:
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    names = {a.name for a in registry.top_agents()}
    prompts = {p.stem.removeprefix("agent_") for p in _REAL_AGENTS_DIR.glob("agent_*.md")}
    configs = {p.stem for p in _REAL_AGENTS_DIR.glob("*.json")}
    assert names == prompts == configs


def test_every_shipped_config_carries_notes() -> None:
    """``notes`` is the rationale a JSON file has nowhere else to put.

    Enforced by a test rather than the loader, exactly as ``specs/*.json`` does
    it: it is a convention for humans, not a thing any code reads.
    """
    for cfg in load_top_agents(_REAL_AGENTS_DIR):
        assert cfg.notes.strip(), cfg.name


def test_guides_picker_label_is_not_its_display_name() -> None:
    """The one case that proves the two names are separate things.

    The feed calls this agent "Kōdo" (its frontmatter ``display_name``); the
    picker calls the *choice* "Guide". A change that collapsed them would
    silently rename a control the user has always known by the other name.
    """
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    guide = next(a for a in registry.top_agents() if a.name == "kodo_guide")
    assert guide.label == "Guide"
    assert registry.get("kodo_guide").display_name == "Kōdo"


def test_only_selectable_agents_would_reach_a_picker() -> None:
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    hidden = {a.name for a in registry.top_agents() if not a.selectable}
    assert hidden == {"kodo_judge"}, "judge is validator-only; everything else is user-facing"


# ---------------------------------------------------------------------------
# Registry cross-checks over the pair
# ---------------------------------------------------------------------------


def test_registry_rejects_a_prompt_with_no_config(agents_dir: Path) -> None:
    _write_top_agent(agents_dir, "reviewer")
    with pytest.raises(AgentLoadError, match="no reviewer.json"):
        AgentRegistry(agents_dir)


def test_registry_rejects_a_config_with_no_prompt(agents_dir: Path) -> None:
    _write_config(agents_dir, "reviewer")
    with pytest.raises(AgentLoadError, match="has no agent_reviewer.md"):
        AgentRegistry(agents_dir)


def test_registry_rejects_a_top_agent_that_also_has_a_spec(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A top-level agent is chosen, never called with arguments.

    A typed I/O contract on one is not surplus but a category error: something
    declared an interface that nothing will ever use.
    """
    import kodo.agents._registry as registry_mod

    _write_top_agent(agents_dir, "reviewer")
    _write_config(agents_dir, "reviewer")
    spec = SubAgentSpec(
        name="reviewer",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
    )
    monkeypatch.setattr(registry_mod, "SUBAGENT_SPECS_BY_NAME", {"reviewer": spec}, raising=True)
    with pytest.raises(AgentLoadError, match="also has specs/reviewer.json"):
        AgentRegistry(agents_dir)


def test_registry_rejects_an_unknown_key_named_aliases(agents_dir: Path) -> None:
    """``aliases`` went with the ``kodo_`` rename, and is now simply unknown.

    The legacy workflow-mode values it carried (``guided``, ``problem_solving``)
    have no agent to resolve to any more, so a config still declaring the key is
    a config written against an older Kōdo — which the unknown-key check is
    there to say out loud rather than ignore.
    """
    path = _write_config(agents_dir, "reviewer")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["aliases"] = ["reviewing"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TopAgentLoadError, match="unknown key"):
        load_top_agent(path)


@pytest.mark.parametrize("defaults", [0, 2])
def test_registry_requires_exactly_one_default(agents_dir: Path, defaults: int) -> None:
    for i, name in enumerate(("reviewer", "auditor")):
        _write_top_agent(agents_dir, name)
        _write_config(agents_dir, name, default=i < defaults, rank=i)
    with pytest.raises(AgentLoadError, match="exactly one top-level agent must declare"):
        AgentRegistry(agents_dir)


def test_registry_reports_an_unreadable_config_without_crashing(agents_dir: Path) -> None:
    _write_top_agent(agents_dir, "reviewer")
    (agents_dir / "reviewer.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(AgentLoadError, match="not valid JSON"):
        AgentRegistry(agents_dir)


def test_label_falls_back_to_display_name(agents_dir: Path) -> None:
    _write_top_agent(agents_dir, "reviewer", frontmatter="display_name: The Reviewer\n")
    _write_config(agents_dir, "reviewer")  # no `label`
    registry = AgentRegistry(agents_dir)
    assert registry.top_agents()[0].label == "The Reviewer"


def test_top_agents_are_ordered_by_rank_then_name(agents_dir: Path) -> None:
    for name, rank in (("charlie", 10), ("alpha", 20), ("bravo", 10)):
        _write_top_agent(agents_dir, name)
        _write_config(agents_dir, name, rank=rank, default=name == "alpha")
    registry = AgentRegistry(agents_dir)
    assert [a.name for a in registry.top_agents()] == ["bravo", "charlie", "alpha"]


def test_a_directory_of_sub_agents_only_has_no_top_agents(agents_dir: Path) -> None:
    """The pairing check must not fire on a registry that has neither half."""
    (agents_dir / SUBAGENTS_SUBDIR).mkdir(exist_ok=True)
    (agents_dir / SUBAGENTS_SUBDIR / "subagent_coder.md").write_text(
        f"---\nname: coder\n---\n{_BODY}", encoding="utf-8"
    )
    registry = AgentRegistry(agents_dir)
    assert registry.top_agents() == ()
    assert registry.default_top_agent() == ""


# ---------------------------------------------------------------------------
# Aggregated reporting
# ---------------------------------------------------------------------------


def test_construction_reports_every_agent_at_fault_not_just_the_first(
    agents_dir: Path,
) -> None:
    """One run should be enough to see the whole broken set.

    Raising on the first problem turned a broken directory into a sequence of
    one-line fixes; each check now records against the agent at fault and
    construction raises once with all of them.
    """
    (agents_dir / SUBAGENTS_SUBDIR).mkdir(exist_ok=True)
    (agents_dir / SUBAGENTS_SUBDIR / "subagent_broken.md").write_text(
        f"---\nname: broken\ntools:\n  - no_such_tool\n---\n{_BODY}", encoding="utf-8"
    )
    _write_top_agent(agents_dir, "orphan")  # no config

    with pytest.raises(AgentLoadError) as excinfo:
        AgentRegistry(agents_dir)

    report = str(excinfo.value)
    assert "2 agents failed validation" in report
    # Both agents are named, and each problem sits under its own agent.
    assert "broken" in report and "orphan" in report
    assert "no_such_tool" in report
    assert "no orphan.json" in report


def test_construction_lists_several_problems_for_one_agent(agents_dir: Path) -> None:
    (agents_dir / SUBAGENTS_SUBDIR).mkdir(exist_ok=True)
    (agents_dir / SUBAGENTS_SUBDIR / "subagent_broken.md").write_text(
        f"---\nname: broken\ntools:\n  - no_such_tool\n  - edit_file\ncritic: ghost\n---\n{_BODY}",
        encoding="utf-8",
    )
    with pytest.raises(AgentLoadError) as excinfo:
        AgentRegistry(agents_dir)

    report = str(excinfo.value)
    assert "1 agent failed validation" in report
    assert "no_such_tool" in report  # unknown tool
    assert "{SHARED:editing}" in report  # file-modifying grant, no discipline
    assert "critic 'ghost' is not loaded" in report  # dangling critic
