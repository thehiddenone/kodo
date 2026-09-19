"""User-installed agents: the store, the two validation regimes, and installing.

Where ``test_agents.py`` sweeps a **closed, curated** set exhaustively, these
tests are open-world: the inputs are third-party files, most of them
deliberately broken, and the property under test is almost always the same one —
*this* entry degrades to a visible row and everything else still loads.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from kodo import __version__
from kodo.agents import (
    BUILTIN_NAME_PREFIX,
    KIND_AGENT,
    KIND_SUBAGENT,
    SHARED_SUBAGENTS_DIRNAME,
    UNVERSIONED,
    AgentInstallError,
    AgentRegistry,
    UserAgentDeleteError,
    UserAgentStore,
    install_source,
    reserved_name_error,
    scan_source,
)

_PACKAGED = Path(__file__).resolve().parents[1] / "src" / "kodo" / "agents"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_agent(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    frontmatter: str = "",
    config: dict[str, object] | None = None,
    body: str = "You are an agent.",
) -> Path:
    """Write one top-level agent bundle under *root* and return its directory."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    version_line = f"version: {version}\n" if version else ""
    (directory / f"agent_{name}.md").write_text(
        f"---\nname: {name}\n{version_line}{frontmatter}---\n{body}\n", encoding="utf-8"
    )
    payload: dict[str, object] = {"name": name, "description": f"The {name} agent.", "rank": 50}
    payload.update(config or {})
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def _write_subagent(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    frontmatter: str = "",
    spec: dict[str, object] | None = None,
) -> Path:
    """Write one user sub-agent (prompt + contract) and return its prompt path."""
    directory = root / SHARED_SUBAGENTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    version_line = f"version: {version}\n" if version else ""
    prompt = directory / f"subagent_{name}.md"
    prompt.write_text(
        f"---\nname: {name}\n{version_line}tools:\n  - read_file\n  - return_result\n"
        f"{frontmatter}---\nYou are {name}.\n\n## Purpose\n\nDoes the {name} job.\n",
        encoding="utf-8",
    )
    payload: dict[str, object] = {
        "name": name,
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
    payload.update(spec or {})
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return prompt


@pytest.fixture
def user_root(tmp_path: Path) -> Path:
    """An empty user agents root."""
    root = tmp_path / "agents"
    root.mkdir()
    return root


def _registry(user_root: Path) -> AgentRegistry:
    """A registry over the real packaged root plus *user_root*."""
    return AgentRegistry(_PACKAGED, user_dir=user_root)


def _broken(registry: AgentRegistry, name: str) -> str:
    """The error recorded against *name*, or ``""`` when it loaded fine."""
    return next((b.error for b in registry.broken_agents if b.name == name), "")


# ---------------------------------------------------------------------------
# Reserved names
# ---------------------------------------------------------------------------


def test_every_builtin_agent_carries_the_reserved_prefix() -> None:
    """The prefix is what makes the two roots share one namespace safely.

    Derived from the packaged directory rather than a hardcoded list, so an
    agent added without the prefix fails here instead of colliding with a user
    file much later.
    """
    stems = [p.stem for p in _PACKAGED.glob("agent_*.md")]
    stems += [p.stem for p in (_PACKAGED / SHARED_SUBAGENTS_DIRNAME).glob("subagent_*.md")]
    assert stems, "no packaged agents found — the glob is wrong"
    for stem in stems:
        name = stem.partition("_")[2]
        assert name.startswith(BUILTIN_NAME_PREFIX), stem


@pytest.mark.parametrize("name", ["kodo_guide", "kodo_", "kodo_anything", SHARED_SUBAGENTS_DIRNAME])
def test_reserved_names_are_refused(name: str) -> None:
    assert reserved_name_error(name)


@pytest.mark.parametrize("name", ["reviewer", "my_kodo_agent", "kodo", "auditor"])
def test_ordinary_names_are_accepted(name: str) -> None:
    assert reserved_name_error(name) == ""


def test_a_user_agent_using_the_reserved_prefix_becomes_a_broken_row(user_root: Path) -> None:
    _write_agent(user_root, "kodo_evil")
    _write_agent(user_root, "fine")
    registry = _registry(user_root)
    assert BUILTIN_NAME_PREFIX in _broken(registry, "kodo_evil")
    # The point of the row: the *other* user agent still loaded.
    assert "fine" in registry.user_agent_names


def test_a_user_subagent_using_the_reserved_prefix_becomes_a_broken_row(user_root: Path) -> None:
    _write_subagent(user_root, "kodo_sneaky")
    registry = _registry(user_root)
    assert BUILTIN_NAME_PREFIX in _broken(registry, "kodo_sneaky")
    assert "kodo_sneaky" not in registry.user_agent_names


# ---------------------------------------------------------------------------
# The layout contract
# ---------------------------------------------------------------------------


def test_a_bundle_with_no_config_is_broken(user_root: Path) -> None:
    directory = user_root / "reviewer"
    directory.mkdir()
    (directory / "agent_reviewer.md").write_text("---\nname: reviewer\n---\nhi\n", encoding="utf-8")
    assert "reviewer.json" in _broken(_registry(user_root), "reviewer")


def test_a_bundle_with_no_prompt_is_broken(user_root: Path) -> None:
    directory = user_root / "reviewer"
    directory.mkdir()
    (directory / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "description": "x"}), encoding="utf-8"
    )
    assert "agent_reviewer.md" in _broken(_registry(user_root), "reviewer")


def test_a_bundle_with_two_prompts_is_broken(user_root: Path) -> None:
    """One prompt per bundle: two would make the directory name ambiguous."""
    directory = _write_agent(user_root, "reviewer")
    (directory / "agent_other.md").write_text("---\nname: other\n---\nhi\n", encoding="utf-8")
    assert "exactly one" in _broken(_registry(user_root), "reviewer")


def test_a_prompt_whose_name_disagrees_with_its_directory_is_broken(user_root: Path) -> None:
    """The directory name *is* the agent's name, so a self-consistent prompt
    that simply sits in the wrong directory is still rejected.

    ``load_agent`` already ties the frontmatter ``name`` to the *filename*; this
    is the third corner it cannot see — filename and frontmatter agreeing with
    each other but not with the bundle they were dropped into.
    """
    directory = user_root / "reviewer"
    directory.mkdir()
    (directory / "agent_auditor.md").write_text("---\nname: auditor\n---\nhi\n", encoding="utf-8")
    (directory / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "description": "x"}), encoding="utf-8"
    )
    assert "must agree" in _broken(_registry(user_root), "reviewer")


def test_a_prompt_whose_name_disagrees_with_its_filename_is_broken(user_root: Path) -> None:
    """The strict loader's own rule, reached through the fail-soft wrapper."""
    directory = user_root / "reviewer"
    directory.mkdir()
    (directory / "agent_reviewer.md").write_text("---\nname: auditor\n---\nhi\n", encoding="utf-8")
    (directory / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "description": "x"}), encoding="utf-8"
    )
    assert "does not match expected" in _broken(_registry(user_root), "reviewer")


def test_a_subagent_with_no_contract_is_broken(user_root: Path) -> None:
    directory = user_root / SHARED_SUBAGENTS_DIRNAME
    directory.mkdir()
    (directory / "subagent_auditor.md").write_text(
        "---\nname: auditor\n---\nhi\n", encoding="utf-8"
    )
    assert "auditor.json" in _broken(_registry(user_root), "auditor")


def test_a_stray_file_beside_the_bundles_is_not_an_agent(user_root: Path) -> None:
    (user_root / "README.md").write_text("notes", encoding="utf-8")
    (user_root / ".DS_Store").write_text("", encoding="utf-8")
    registry = _registry(user_root)
    assert registry.broken_agents == ()


def test_an_absent_user_root_is_not_an_error(tmp_path: Path) -> None:
    registry = AgentRegistry(_PACKAGED, user_dir=tmp_path / "nothing-here")
    assert registry.broken_agents == ()
    assert registry.user_agent_names == frozenset()


# ---------------------------------------------------------------------------
# The two validation regimes
# ---------------------------------------------------------------------------


def test_a_broken_user_agent_does_not_stop_the_registry(user_root: Path) -> None:
    """The whole point of the second regime: one bad file, one row, server up."""
    _write_agent(user_root, "badtool", frontmatter="tools:\n  - no_such_tool\n")
    registry = _registry(user_root)
    assert "no_such_tool" in _broken(registry, "badtool")
    assert "badtool" not in registry.user_agent_names
    # Every packaged agent is still there.
    assert {t.name for t in registry.top_agents()} >= {"kodo_guide", "kodo_problem_solver"}


def test_a_user_agent_may_omit_the_shared_blocks(user_root: Path) -> None:
    """Relaxed for user agents; still required of every packaged one."""
    _write_agent(user_root, "reviewer")
    registry = _registry(user_root)
    assert _broken(registry, "reviewer") == ""
    assert "{SHARED:" not in registry.get("reviewer").system_prompt


def test_a_user_agent_may_include_the_shared_blocks_and_gets_the_prose(user_root: Path) -> None:
    _write_agent(user_root, "reviewer", body="Do the job.\n\n{SHARED:security}")
    prompt = _registry(user_root).get("reviewer").system_prompt
    assert "{SHARED:security}" not in prompt
    assert len(prompt) > len("Do the job.")


def test_a_user_agent_naming_an_unknown_shared_block_is_broken(user_root: Path) -> None:
    """Relaxing the *requirement* does not relax the spelling check.

    An unknown block name renders nothing at all, which is a silent failure
    whichever root it came from.
    """
    _write_agent(user_root, "reviewer", body="Do the job.\n\n{SHARED:no_such_block}")
    assert "unknown shared block" in _broken(_registry(user_root), "reviewer")


def test_a_user_subagent_may_coin_a_new_artifact_role(user_root: Path) -> None:
    """The role vocabulary is closed for packaged specs and open for user ones."""
    _write_subagent(
        user_root,
        "auditor",
        spec={
            "produces": {"threat_model": "paths"},
            "output_schema": {
                "shape": "raw",
                "schema": {
                    "type": "object",
                    "properties": {"paths": {"type": "array", "items": {"type": "string"}}},
                    "required": ["paths"],
                },
            },
        },
    )
    registry = _registry(user_root)
    assert _broken(registry, "auditor") == ""
    spec = registry.spec_for("auditor")
    assert spec is not None
    assert spec.produces == {"threat_model": "paths"}


def test_a_user_subagent_consuming_a_role_nobody_produces_is_broken(user_root: Path) -> None:
    """Open vocabulary, but the rule that actually matters still applies."""
    _write_subagent(
        user_root, "auditor", spec={"consumes": [{"role": "nobody_makes_this", "scope": "global"}]}
    )
    assert "no sub-agent declares" in _broken(_registry(user_root), "auditor")


def test_a_user_subagent_may_consume_a_builtin_role(user_root: Path) -> None:
    _write_subagent(
        user_root, "auditor", spec={"consumes": [{"role": "requirements", "scope": "global"}]}
    )
    assert _broken(_registry(user_root), "auditor") == ""


def test_demotion_cascades_to_an_agent_that_named_the_demoted_one(user_root: Path) -> None:
    """Dropping one user agent can invalidate another, which must drop too."""
    _write_subagent(user_root, "auditor", frontmatter="critic: no_such_critic\n")
    _write_agent(
        user_root,
        "reviewer",
        frontmatter="tools:\n  - run_subagent\nsubagents:\n  - auditor\n",
    )
    registry = _registry(user_root)
    assert "no_such_critic" in _broken(registry, "auditor")
    assert "auditor" in _broken(registry, "reviewer")
    assert registry.user_agent_names == frozenset()
    # And the packaged set is untouched by both.
    assert registry.default_top_agent() == "kodo_problem_solver"


def test_a_user_agent_cannot_claim_the_default(user_root: Path) -> None:
    _write_agent(user_root, "reviewer", config={"default": True})
    registry = _registry(user_root)
    assert "default" in _broken(registry, "reviewer")
    assert registry.default_top_agent() == "kodo_problem_solver"


# ---------------------------------------------------------------------------
# What a loaded user agent can do
# ---------------------------------------------------------------------------


def test_a_user_agent_appears_in_the_picker(user_root: Path) -> None:
    _write_agent(user_root, "reviewer", config={"label": "Reviewer", "rank": 99})
    picker = {t.name: t for t in _registry(user_root).top_agents()}
    assert picker["reviewer"].label == "Reviewer"
    assert picker["reviewer"].selectable


def test_a_user_agent_may_spawn_both_user_and_builtin_subagents(user_root: Path) -> None:
    _write_subagent(user_root, "auditor")
    _write_agent(
        user_root,
        "reviewer",
        frontmatter="tools:\n  - run_subagent\nsubagents:\n  - auditor\n  - kodo_investigator\n",
    )
    tools = {s.name for s in _registry(user_root).run_subagent_specs("reviewer")}
    assert tools == {"run_subagent_auditor", "run_subagent_kodo_investigator"}


def test_a_user_subagent_may_pair_with_a_builtin_critic(user_root: Path) -> None:
    _write_subagent(user_root, "auditor", frontmatter="critic: kodo_code_critic\n")
    registry = _registry(user_root)
    assert _broken(registry, "auditor") == ""
    assert registry.get("auditor").critic == "kodo_code_critic"


def test_user_subagents_are_shared_by_every_user_agent(user_root: Path) -> None:
    _write_subagent(user_root, "auditor")
    for name in ("reviewer", "inspector"):
        _write_agent(
            user_root, name, frontmatter="tools:\n  - run_subagent\nsubagents:\n  - auditor\n"
        )
    registry = _registry(user_root)
    for name in ("reviewer", "inspector"):
        assert "run_subagent_auditor" in {s.name for s in registry.run_subagent_specs(name)}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def test_builtin_agents_are_stamped_with_the_kodo_version(user_root: Path) -> None:
    registry = _registry(user_root)
    assert registry.get("kodo_guide").version == __version__
    assert registry.get("kodo_coder").version == __version__


def test_a_user_agent_carries_its_own_declared_version(user_root: Path) -> None:
    _write_agent(user_root, "reviewer", version="4.2.1")
    assert _registry(user_root).get("reviewer").version == "4.2.1"


def test_a_user_agent_may_omit_its_version(user_root: Path) -> None:
    _write_agent(user_root, "reviewer", version="")
    registry = _registry(user_root)
    assert _broken(registry, "reviewer") == ""
    assert registry.get("reviewer").version == ""


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------


def test_reload_picks_up_an_agent_installed_after_construction(user_root: Path) -> None:
    registry = _registry(user_root)
    assert "reviewer" not in registry.user_agent_names
    _write_agent(user_root, "reviewer")
    registry.reload()
    assert "reviewer" in registry.user_agent_names
    assert "reviewer" in {t.name for t in registry.top_agents()}


def test_reload_drops_an_agent_deleted_after_construction(user_root: Path) -> None:
    _write_agent(user_root, "reviewer")
    registry = _registry(user_root)
    assert "reviewer" in registry.user_agent_names
    UserAgentStore(user_root).delete("reviewer", top_level=True)
    registry.reload()
    assert "reviewer" not in {t.name for t in registry.top_agents()}


def test_reload_reports_a_newly_broken_user_agent_without_raising(user_root: Path) -> None:
    registry = _registry(user_root)
    _write_agent(user_root, "reviewer", frontmatter="tools:\n  - no_such_tool\n")
    broken = registry.reload()
    assert [b.name for b in broken] == ["reviewer"]


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def test_delete_removes_a_top_level_bundle(user_root: Path) -> None:
    _write_agent(user_root, "reviewer")
    UserAgentStore(user_root).delete("reviewer", top_level=True)
    assert not (user_root / "reviewer").exists()


def test_delete_removes_both_halves_of_a_subagent(user_root: Path) -> None:
    prompt = _write_subagent(user_root, "auditor")
    UserAgentStore(user_root).delete("auditor", top_level=False)
    assert not prompt.exists()
    assert not (user_root / SHARED_SUBAGENTS_DIRNAME / "auditor.json").exists()


@pytest.mark.parametrize("name", ["", "..", "../escape", "a/b"])
def test_delete_refuses_a_name_that_is_not_a_single_component(user_root: Path, name: str) -> None:
    with pytest.raises(UserAgentDeleteError):
        UserAgentStore(user_root).delete(name, top_level=True)


def test_delete_refuses_something_not_installed(user_root: Path) -> None:
    with pytest.raises(UserAgentDeleteError, match="no user agent"):
        UserAgentStore(user_root).delete("absent", top_level=True)


def test_ensure_root_creates_both_halves_of_the_layout(tmp_path: Path) -> None:
    store = UserAgentStore(tmp_path / "agents")
    store.ensure_root()
    assert store.root.is_dir()
    assert store.subagents_dir.is_dir()


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """A source directory holding one agent and one sub-agent."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "agent_reviewer.md").write_text(
        "---\nname: reviewer\nversion: 2.0.0\ntools:\n  - read_file\n---\nYou are Reviewer.\n",
        encoding="utf-8",
    )
    (src / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "description": "Audits a codebase."}), encoding="utf-8"
    )
    _write_subagent(src, "auditor", version="1.5.0")
    return src


def test_scan_reports_what_a_source_offers(source: Path, user_root: Path) -> None:
    scan = scan_source(str(source), user_root)
    found = {(c.kind, c.name, c.version) for c in scan.candidates}
    assert found == {(KIND_AGENT, "reviewer", "2.0.0"), (KIND_SUBAGENT, "auditor", "1.5.0")}
    assert scan.conflict_report() == ""


def test_install_puts_each_half_where_the_registry_reads_it(source: Path, user_root: Path) -> None:
    """The redistribution is the whole reason the installer exists."""
    result = install_source(str(source), user_root, replace=False)
    assert set(result.installed) == {"reviewer", "auditor"}
    assert (user_root / "reviewer" / "agent_reviewer.md").is_file()
    assert (user_root / "reviewer" / "reviewer.json").is_file()
    assert (user_root / SHARED_SUBAGENTS_DIRNAME / "subagent_auditor.md").is_file()
    assert (user_root / SHARED_SUBAGENTS_DIRNAME / "auditor.json").is_file()
    assert "reviewer" in {t.name for t in _registry(user_root).top_agents()}


def test_a_source_with_only_subagents_installs(tmp_path: Path, user_root: Path) -> None:
    src = tmp_path / "subs-only"
    src.mkdir()
    _write_subagent(src, "auditor")
    result = install_source(str(src), user_root, replace=False)
    assert result.installed == ("auditor",)


def test_scan_reports_both_versions_when_something_is_already_installed(
    source: Path, user_root: Path
) -> None:
    install_source(str(source), user_root, replace=False)
    (source / "agent_reviewer.md").write_text(
        "---\nname: reviewer\nversion: 3.0.0\ntools:\n  - read_file\n---\nYou are Reviewer.\n",
        encoding="utf-8",
    )
    scan = scan_source(str(source), user_root)
    report = scan.conflict_report()
    assert "installed: 2.0.0" in report
    assert "incoming: 3.0.0" in report
    assert {c.name for c in scan.conflicting} == {"reviewer", "auditor"}


def test_keeping_leaves_the_installed_version_alone(source: Path, user_root: Path) -> None:
    install_source(str(source), user_root, replace=False)
    (source / "agent_reviewer.md").write_text(
        "---\nname: reviewer\nversion: 3.0.0\ntools:\n  - read_file\n---\nnew body\n",
        encoding="utf-8",
    )
    result = install_source(str(source), user_root, replace=False)
    assert "reviewer" in result.kept
    assert result.installed == ()
    assert "version: 2.0.0" in (user_root / "reviewer" / "agent_reviewer.md").read_text()


def test_replacing_overwrites_the_installed_version(source: Path, user_root: Path) -> None:
    install_source(str(source), user_root, replace=False)
    (source / "agent_reviewer.md").write_text(
        "---\nname: reviewer\nversion: 3.0.0\ntools:\n  - read_file\n---\nnew body\n",
        encoding="utf-8",
    )
    result = install_source(str(source), user_root, replace=True)
    assert "reviewer" in result.installed
    assert "version: 3.0.0" in (user_root / "reviewer" / "agent_reviewer.md").read_text()


def test_an_unversioned_entry_is_labelled_not_refused(tmp_path: Path, user_root: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "agent_reviewer.md").write_text(
        "---\nname: reviewer\ntools:\n  - read_file\n---\nbody\n", encoding="utf-8"
    )
    (src / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "description": "x"}), encoding="utf-8"
    )
    scan = scan_source(str(src), user_root)
    assert scan.candidates[0].version == UNVERSIONED
    assert scan.candidates[0].installable


def test_install_reports_a_bad_entry_instead_of_dropping_it(source: Path, user_root: Path) -> None:
    (source / "agent_kodo_evil.md").write_text(
        "---\nname: kodo_evil\n---\nbody\n", encoding="utf-8"
    )
    (source / "kodo_evil.json").write_text(
        json.dumps({"name": "kodo_evil", "description": "x"}), encoding="utf-8"
    )
    scan = scan_source(str(source), user_root)
    bad = next(c for c in scan.candidates if c.name == "kodo_evil")
    assert not bad.installable
    assert BUILTIN_NAME_PREFIX in bad.error
    # …and the good entries still install.
    result = install_source(str(source), user_root, replace=False)
    assert set(result.installed) == {"reviewer", "auditor"}
    assert any("kodo_evil" in line for line in result.skipped)


def test_install_can_be_narrowed_to_a_subset(source: Path, user_root: Path) -> None:
    result = install_source(str(source), user_root, replace=False, names=("auditor",))
    assert result.installed == ("auditor",)
    assert not (user_root / "reviewer").exists()


def test_asking_for_something_the_source_lost_is_reported_as_missing(
    source: Path, user_root: Path
) -> None:
    result = install_source(str(source), user_root, replace=False, names=("gone",))
    assert result.missing == ("gone",)


def test_scanning_a_path_that_is_not_a_directory_raises(tmp_path: Path, user_root: Path) -> None:
    with pytest.raises(AgentInstallError, match="not a directory"):
        scan_source(str(tmp_path / "nope"), user_root)


# ---------------------------------------------------------------------------
# Installing from a repository
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(source: Path) -> Iterator[str]:
    """*source*, committed to a local git repository, as a cloneable URL."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not available")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=source,
        check=True,
    )
    yield f"file://{source}"


def test_scan_reads_a_repository_without_installing(repo: str, user_root: Path) -> None:
    scan = scan_source(repo, user_root)
    assert {c.name for c in scan.candidates} == {"reviewer", "auditor"}
    assert list(user_root.iterdir()) == []


def test_install_from_a_repository_lands_in_the_same_layout(repo: str, user_root: Path) -> None:
    result = install_source(repo, user_root, replace=False)
    assert set(result.installed) == {"reviewer", "auditor"}
    assert (user_root / "reviewer" / "agent_reviewer.md").is_file()
    assert (user_root / SHARED_SUBAGENTS_DIRNAME / "subagent_auditor.md").is_file()


def test_cloning_an_unreachable_repository_raises(user_root: Path) -> None:
    with pytest.raises(AgentInstallError, match="cloning"):
        scan_source("file:///nonexistent/kodo-agents-test.git", user_root)
