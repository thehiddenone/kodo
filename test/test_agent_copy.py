"""Copying a built-in agent with its pipeline into the user root (doc/AGENT_BUILDER.md §3).

Two kinds of fixture. The **real** packaged agents prove the property that
matters most — a copy of every copyable built-in loads in a registry as an
ordinary user agent. A small **synthetic** built-in tree pins the renaming
rules on inputs chosen to break them: a name that is a prefix of another, a
mention of an agent outside the closure, a source with no ``display_name``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kodo.agents import (
    BUILTIN_NAME_PREFIX,
    ENGINE_DRIVEN_AGENTS,
    KIND_AGENT,
    KIND_SUBAGENT,
    PACKAGED_AGENTS_DIR,
    SHARED_SUBAGENTS_DIRNAME,
    UNVERSIONED,
    AgentCopyError,
    AgentRegistry,
    copy_agent,
    copy_sources,
    load_agent,
    load_top_agent,
    plan_copy,
)
from kodo.agents.subagents import load_spec

_PREFIX = "acme_"


@pytest.fixture
def user_root(tmp_path: Path) -> Path:
    """An empty user agents root, inside a parent the copy may stage into."""
    return tmp_path / "home" / ".kodo" / "agents"


def _registry(user_root: Path) -> AgentRegistry:
    return AgentRegistry(PACKAGED_AGENTS_DIR, user_dir=user_root)


def _expected_closure(registry: AgentRegistry, name: str) -> list[str]:
    """The closure as §3.1 defines it, walked over the live registry."""
    order, index = [name], 0
    while index < len(order):
        agent = registry.get(order[index])
        index += 1
        for reached in (*agent.subagent_order, agent.critic):
            if reached.startswith(BUILTIN_NAME_PREFIX) and reached not in order:
                order.append(reached)
    return order


def _renamed(name: str) -> str:
    return _PREFIX + name[len(BUILTIN_NAME_PREFIX) :]


# ---------------------------------------------------------------------------
# A synthetic built-in tree
# ---------------------------------------------------------------------------


_RAW_SPEC: dict[str, object] = {
    "input_schema": {
        "shape": "raw",
        "schema": {
            "type": "object",
            "properties": {"instructions": {"type": "string", "description": "d"}},
            "required": ["instructions"],
        },
    },
    "output_schema": {
        "shape": "raw",
        "schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "d"}},
            "required": ["summary"],
        },
    },
}


def _builtin_subagent(root: Path, name: str, frontmatter: str, body: str) -> None:
    directory = root / "subagents"
    (directory / "specs").mkdir(parents=True, exist_ok=True)
    (directory / f"subagent_{name}.md").write_text(
        f"---\nname: {name}\n{frontmatter}---\n{body}\n", encoding="utf-8"
    )
    (directory / "specs" / f"{name}.json").write_text(
        json.dumps({"name": name, "notes": f"spec of {name}", **_RAW_SPEC}), encoding="utf-8"
    )


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    """A tiny built-in tree: a lead, two authors sharing a critic, and an outsider."""
    root = tmp_path / "builtin"
    root.mkdir()
    (root / "agent_kodo_lead.md").write_text(
        "---\nname: kodo_lead\ntools:\n  - run_subagent\nsubagents:\n  - kodo_coder\n"
        "  - kodo_test_coder\n---\n# Lead\n\n"
        "Call `run_subagent_kodo_coder`, then run_subagent_kodo_test_coder. "
        "Never touch kodo_outsider, subagent_kodo_coder.md or my_kodo_coder.\n",
        encoding="utf-8",
    )
    (root / "kodo_lead.json").write_text(
        json.dumps(
            {
                "name": "kodo_lead",
                "notes": "kept as written",
                "label": "Lead",
                "description": "Leads.",
                "rank": 7,
                "default": True,
            }
        ),
        encoding="utf-8",
    )
    _builtin_subagent(
        root,
        "kodo_coder",
        "display_name: Coder\ncritic: kodo_critic\ntools:\n  - read_file\n",
        "# Coder\n\n## Purpose\n\nWrites code; kodo_critic reviews it.",
    )
    _builtin_subagent(
        root,
        "kodo_test_coder",
        "critic: kodo_critic\ntools:\n  - read_file\n",
        "# Test Coder\n\n## Purpose\n\nWrites tests.",
    )
    _builtin_subagent(root, "kodo_critic", "role: critic\ntools:\n  - read_file\n", "Reviews.")
    _builtin_subagent(
        root, "kodo_outsider", "tools:\n  - read_file\n", "## Purpose\n\nNot in the closure."
    )
    return root


def _copied_prompt(user_root: Path, name: str, *, top_level: bool = False) -> str:
    if top_level:
        return (user_root / name / f"agent_{name}.md").read_text(encoding="utf-8")
    return (user_root / SHARED_SUBAGENTS_DIRNAME / name / f"subagent_{name}.md").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The closure and what a copy of the real built-ins yields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [s.name for s in copy_sources() if s.kind == KIND_AGENT])
def test_the_plan_is_the_closure_over_subagents_and_critics(source: str, user_root: Path) -> None:
    registry = _registry(user_root)
    plan = plan_copy(source, _PREFIX, user_root)
    assert [m.source for m in plan.members] == _expected_closure(registry, source)
    assert [m.name for m in plan.members] == [_renamed(m.source) for m in plan.members]


@pytest.mark.parametrize("source", [s.name for s in copy_sources()])
def test_a_copy_of_every_copyable_builtin_loads_as_a_user_agent(
    source: str, user_root: Path
) -> None:
    result = copy_agent(source, _PREFIX, user_root, replace=False)

    registry = _registry(user_root)
    assert registry.broken_agents == ()
    assert set(result.copied) <= registry.user_agent_names
    assert _renamed(source) in result.copied


def test_a_copied_top_level_agent_appears_in_the_picker_beside_its_source(
    user_root: Path,
) -> None:
    source = next(s for s in copy_sources() if s.kind == KIND_AGENT)
    copy_agent(source.name, _PREFIX, user_root, replace=False)

    labels = {top.name: top.label for top in _registry(user_root).top_agents()}
    assert labels[_renamed(source.name)] == f"{labels[source.name]} (acme)"


def test_the_copy_names_closure_members_in_its_body_by_their_new_names(user_root: Path) -> None:
    source = max(
        (s for s in copy_sources() if s.kind == KIND_AGENT),
        key=lambda s: len(_expected_closure(_registry(user_root), s.name)),
    )
    copy_agent(source.name, _PREFIX, user_root, replace=False)

    copy = _registry(user_root).get(_renamed(source.name))
    for member in _expected_closure(_registry(user_root), source.name)[1:]:
        assert f"run_subagent_{member}" not in copy.system_prompt


def test_no_staging_directory_is_left_behind(user_root: Path) -> None:
    copy_agent(copy_sources()[0].name, _PREFIX, user_root, replace=False)
    assert sorted(p.name for p in user_root.parent.iterdir()) == ["agents"]


# ---------------------------------------------------------------------------
# Renaming rules, on the synthetic tree
# ---------------------------------------------------------------------------


def test_frontmatter_names_are_rewritten(builtin_dir: Path, user_root: Path) -> None:
    copy_agent("kodo_lead", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    lead = load_agent(user_root / "acme_lead" / "agent_acme_lead.md")
    coder = load_agent(user_root / "subagents" / "acme_coder" / "subagent_acme_coder.md")
    assert lead.name == "acme_lead"
    assert lead.subagent_order == ("acme_coder", "acme_test_coder")
    assert coder.critic == "acme_critic"


def test_body_tokens_are_renamed_whole_word_and_only_for_closure_members(
    builtin_dir: Path, user_root: Path
) -> None:
    copy_agent("kodo_lead", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    body = _copied_prompt(user_root, "acme_lead", top_level=True)
    assert "`run_subagent_acme_coder`" in body
    assert "run_subagent_acme_test_coder" in body
    assert "kodo_outsider" in body
    assert "subagent_kodo_coder.md" in body
    assert "my_kodo_coder" in body
    assert "kodo_coder" not in body.replace("subagent_kodo_coder.md", "").replace(
        "my_kodo_coder", ""
    )
    assert "Writes code; acme_critic reviews it." in _copied_prompt(user_root, "acme_coder")


def test_display_names_get_the_prefix_as_a_suffix(builtin_dir: Path, user_root: Path) -> None:
    copy_agent("kodo_lead", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    coder = load_agent(user_root / "subagents" / "acme_coder" / "subagent_acme_coder.md")
    assert coder.display_name == "Coder (acme)"


def test_a_source_without_a_display_name_gets_one_written_explicitly(
    builtin_dir: Path, user_root: Path
) -> None:
    copy_agent("kodo_lead", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    test_coder = load_agent(
        user_root / "subagents" / "acme_test_coder" / "subagent_acme_test_coder.md"
    )
    assert test_coder.display_name == "Kodo Test Coder (acme)"


def test_a_prefix_without_a_trailing_underscore_is_used_as_written(
    builtin_dir: Path, user_root: Path
) -> None:
    copy_agent("kodo_coder", "acme", user_root, replace=False, builtin_dir=builtin_dir)

    coder = load_agent(user_root / "subagents" / "acmecoder" / "subagent_acmecoder.md")
    assert coder.display_name == "Coder (acme)"
    assert coder.critic == "acmecritic"


def test_the_config_is_renamed_suffixed_and_never_the_default(
    builtin_dir: Path, user_root: Path
) -> None:
    copy_agent("kodo_lead", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    config = load_top_agent(user_root / "acme_lead" / "acme_lead.json")
    assert config.name == "acme_lead"
    assert config.label == "Lead (acme)"
    assert config.default is False
    assert config.rank == 7
    assert config.notes == "kept as written"


def test_the_spec_keeps_everything_but_its_name(builtin_dir: Path, user_root: Path) -> None:
    copy_agent("kodo_coder", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    spec = load_spec(user_root / "subagents" / "acme_coder" / "acme_coder.json")
    source = load_spec(builtin_dir / "subagents" / "specs" / "kodo_coder.json")
    assert spec.name == "acme_coder"
    assert spec.notes == source.notes
    assert spec.output_schema == source.output_schema


def test_copying_a_subagent_copies_it_and_its_critic_only(
    builtin_dir: Path, user_root: Path
) -> None:
    result = copy_agent("kodo_coder", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)
    assert result.copied == ("acme_coder", "acme_critic")
    assert sorted(p.name for p in (user_root / "subagents").iterdir()) == [
        "acme_coder",
        "acme_critic",
    ]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", sorted(ENGINE_DRIVEN_AGENTS))
def test_an_engine_driven_agent_cannot_be_copied(source: str, user_root: Path) -> None:
    with pytest.raises(AgentCopyError, match="engine"):
        plan_copy(source, _PREFIX, user_root)
    assert source not in {s.name for s in copy_sources()}


def test_a_non_interactive_agent_cannot_be_copied(user_root: Path) -> None:
    locked = [t.name for t in _registry(user_root).top_agents() if not t.interactive]
    assert locked, "this test assumes a built-in with interactive: false ships"
    for source in locked:
        with pytest.raises(AgentCopyError, match="agent.run"):
            plan_copy(source, _PREFIX, user_root)
        assert source not in {s.name for s in copy_sources()}


@pytest.mark.parametrize("source", ["nobody", "reviewer", "kodo_nobody"])
def test_only_a_builtin_can_be_the_source(source: str, user_root: Path) -> None:
    with pytest.raises(AgentCopyError, match="no built-in agent"):
        plan_copy(source, _PREFIX, user_root)


@pytest.mark.parametrize(
    ("prefix", "reason"),
    [
        ("", "empty"),
        ("kodo_", "reserved"),
        ("kodo_x_", "reserved"),
        ("Acme_", "lowercase"),
        ("9acme_", "lowercase"),
        ("ac-me_", "lowercase"),
        ("a" * 60 + "_", "too long"),
    ],
)
def test_a_prefix_that_cannot_make_a_valid_name_is_refused(
    prefix: str, reason: str, user_root: Path
) -> None:
    with pytest.raises(AgentCopyError, match=reason):
        plan_copy(copy_sources()[0].name, prefix, user_root)


def test_a_refused_copy_writes_nothing(user_root: Path) -> None:
    with pytest.raises(AgentCopyError):
        copy_agent(copy_sources()[0].name, "kodo_", user_root, replace=True)
    assert not user_root.exists()


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_a_free_destination_is_no_conflict(builtin_dir: Path, user_root: Path) -> None:
    plan = plan_copy("kodo_lead", _PREFIX, user_root, builtin_dir=builtin_dir)
    assert plan.conflicting == ()
    assert plan.conflict_report() == ""


def test_a_taken_destination_is_reported_as_an_install_reports_it(
    builtin_dir: Path, user_root: Path
) -> None:
    copy_agent("kodo_coder", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    plan = plan_copy("kodo_lead", _PREFIX, user_root, builtin_dir=builtin_dir)

    taken = {(m.kind, m.name) for m in plan.conflicting}
    assert taken == {(KIND_SUBAGENT, "acme_coder"), (KIND_SUBAGENT, "acme_critic")}
    report = plan.conflict_report().splitlines()
    assert len(report) == 2
    assert all(f"installed: {UNVERSIONED}" in line for line in report)


def test_a_destination_that_does_not_load_still_counts_as_taken(
    builtin_dir: Path, user_root: Path
) -> None:
    junk = user_root / "acme_lead"
    junk.mkdir(parents=True)
    (junk / "notes.txt").write_text("not an agent", encoding="utf-8")

    plan = plan_copy("kodo_lead", _PREFIX, user_root, builtin_dir=builtin_dir)
    assert [m.name for m in plan.conflicting] == ["acme_lead"]
    assert plan.conflicting[0].kind == KIND_AGENT


def test_keeping_leaves_taken_destinations_alone(builtin_dir: Path, user_root: Path) -> None:
    copy_agent("kodo_coder", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)
    edited = user_root / "subagents" / "acme_coder" / "subagent_acme_coder.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")

    result = copy_agent("kodo_lead", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)

    assert result.kept == ("acme_coder", "acme_critic")
    assert "acme_coder" not in result.copied
    assert "Edited." in edited.read_text(encoding="utf-8")


def test_replacing_overwrites_taken_destinations_whole(builtin_dir: Path, user_root: Path) -> None:
    copy_agent("kodo_coder", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)
    stray = user_root / "subagents" / "acme_coder" / "stray.txt"
    stray.write_text("left over", encoding="utf-8")

    result = copy_agent("kodo_lead", _PREFIX, user_root, replace=True, builtin_dir=builtin_dir)

    assert "acme_coder" in result.copied
    assert result.kept == ()
    assert not stray.exists()


def test_a_failed_move_restores_the_user_root(
    builtin_dir: Path, user_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_agent("kodo_coder", _PREFIX, user_root, replace=False, builtin_dir=builtin_dir)
    edited = user_root / "subagents" / "acme_coder" / "subagent_acme_coder.md"
    edited.write_text("---\nname: acme_coder\n---\nMine.\n", encoding="utf-8")
    before = sorted(str(p.relative_to(user_root)) for p in user_root.rglob("*"))

    real_replace = os.replace
    moves: list[str] = []

    def _fail_on_the_fourth_move(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        moves.append(str(dst))
        if len(moves) == 4:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_on_the_fourth_move)
    with pytest.raises(AgentCopyError, match="disk full"):
        copy_agent("kodo_lead", _PREFIX, user_root, replace=True, builtin_dir=builtin_dir)
    monkeypatch.setattr(os, "replace", real_replace)

    assert sorted(str(p.relative_to(user_root)) for p in user_root.rglob("*")) == before
    assert edited.read_text(encoding="utf-8") == "---\nname: acme_coder\n---\nMine.\n"
    assert sorted(p.name for p in user_root.parent.iterdir()) == ["agents"]
