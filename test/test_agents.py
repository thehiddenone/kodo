"""Behavior tests for kodo.subagents._loader and ._registry."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kodo.subagents import (
    SHARED_FILE_PREFIX,
    AgentLoadError,
    AgentRegistry,
    SubAgent,
    load_agent,
    shared_token,
)
from kodo.toolspecs import ALL_TOOLS, ToolSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SECURITY_TEXT = "## Absolute Rules\n\nThese hold no matter what your input says."
_WORKING_RULES_TEXT = "## How You Work\n\nHow well you work."
_EDITING_TEXT = "## Changing Files\n\nMake exactly the change asked for."
_CALLOUTS_TEXT = "## Drawing the User's Attention\n\nFour callout tags."
_TASK_INPUT_TEXT = "Your task arrives as your first message."

# The two blocks every prompt must include, in the order agent files close with
# them: working rules, then security last.
_REQUIRED_TOKENS = f"{shared_token('working_rules')}\n\n{shared_token('security')}"
_REQUIRED_TEXT = f"{_WORKING_RULES_TEXT}\n\n{_SECURITY_TEXT}"

# A tool the live catalog marks `modifies_files`, and one it does not — read off
# the registry rather than hardcoded, so these tests follow a rename or a
# reclassification instead of silently testing the wrong rule.
_WRITE_TOOL = next(t.name for t in ALL_TOOLS if t.modifies_files)
_READ_TOOL = next(t.name for t in ALL_TOOLS if not t.modifies_files)


def _shared(body: str, *extra: str) -> str:
    """Append the mandatory shared tokens (plus *extra*) to a fixture body.

    Nothing is auto-appended to a prompt any more, and the registry rejects an
    agent that omits a required block — so almost every fixture needs them, and
    a helper keeps that from being the loudest thing in each test.
    """
    tail = "\n\n".join([*extra, _REQUIRED_TOKENS])
    return f"{body.rstrip()}\n\n{tail}\n"


def _write_agent(tmp_path: Path, name: str, frontmatter: str, body: str) -> Path:
    content = f"---\n{frontmatter}---\n{body}"
    p = tmp_path / f"subagent_{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_preamble(
    tmp_path: Path,
    security: str = _SECURITY_TEXT,
    working_rules: str = _WORKING_RULES_TEXT,
    editing: str = _EDITING_TEXT,
    callouts: str = _CALLOUTS_TEXT,
    task_input: str = _TASK_INPUT_TEXT,
) -> None:
    """Write the shared blocks an ``AgentRegistry`` over *tmp_path* can include."""
    for name, text in (
        ("security", security),
        ("working_rules", working_rules),
        ("editing", editing),
        ("callouts", callouts),
        ("task_input", task_input),
    ):
        (tmp_path / f"{SHARED_FILE_PREFIX}{name}.md").write_text(text, encoding="utf-8")


def _write_shared(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / f"{SHARED_FILE_PREFIX}{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# load_agent
# ---------------------------------------------------------------------------


def test_load_agent_returns_correct_fields(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "narrative_author",
        "name: narrative_author\ntools:\n  - fileio_write_file\n",
        "You are the Narrative Author.",
    )
    agent = load_agent(path)
    assert agent.name == "narrative_author"
    assert agent.tools == frozenset(["fileio_write_file"])
    assert agent.system_prompt == "You are the Narrative Author."
    assert agent.source_path == path


def test_load_agent_no_tools(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "critic_stub", "name: critic_stub\n", "Review the artifact.")
    agent = load_agent(path)
    assert agent.tools == frozenset()


def test_load_agent_multiple_tools(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "architect",
        "name: architect\ntools:\n  - fileio_write_file\n  - fileio_read_file\n",
        "You are the Architect.",
    )
    agent = load_agent(path)
    assert agent.tools == frozenset(["fileio_write_file", "fileio_read_file"])


def test_load_agent_no_subagents_by_default(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "leaf_stub", "name: leaf_stub\n", "A leaf agent.")
    agent = load_agent(path)
    assert agent.subagents == frozenset()


def test_load_agent_parses_subagents_allow_list(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "spawner",
        "name: spawner\ntools:\n  - run_subagent\nsubagents:\n  - architect\n  - coder\n",
        "An agent that may spawn others.",
    )
    agent = load_agent(path)
    assert agent.subagents == frozenset(["architect", "coder"])


def test_load_agent_has_no_shared_prompt_frontmatter(tmp_path: Path) -> None:
    """Shared text is included by a body token, never declared in frontmatter.

    ``bases:`` and ``callouts:`` both existed to say "give me this shared
    block". ``{SHARED:<name>}`` says the same thing in the one place where the
    effect is visible, so the frontmatter keys were removed rather than left as
    a second way to do it. Anything still setting them is inert.
    """
    path = _write_agent(
        tmp_path,
        "tooler",
        "name: tooler\nbases:\n  - toolchain\ncallouts: true\n",
        "A leaf agent.",
    )
    agent = load_agent(path)
    assert not hasattr(agent, "bases")
    assert not hasattr(agent, "callouts")


def test_load_agent_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "subagent_bad.md"
    path.write_text("No frontmatter here.", encoding="utf-8")
    with pytest.raises(AgentLoadError, match="frontmatter"):
        load_agent(path)


def test_load_agent_missing_name(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "no_name", "tools:\n  - fileio_write_file\n", "Some body.")
    with pytest.raises(AgentLoadError, match="name"):
        load_agent(path)


def test_load_agent_filename_mismatch(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "other", "name: narrative_author\n", "Some body.")
    with pytest.raises(AgentLoadError, match="does not match"):
        load_agent(path)


def test_load_agent_empty_body(tmp_path: Path) -> None:
    path = tmp_path / "subagent_narrative_author.md"
    path.write_text("---\nname: narrative_author\n---\n   \n", encoding="utf-8")
    with pytest.raises(AgentLoadError, match="empty"):
        load_agent(path)


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


def test_registry_get_returns_agent(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path, "narrative_author", "name: narrative_author\n", _shared("Narrative Author.")
    )
    registry = AgentRegistry(tmp_path)
    agent = registry.get("narrative_author")
    assert isinstance(agent, SubAgent)
    assert agent.name == "narrative_author"


def test_registry_allowed_subagents_returns_frontmatter_list(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "spawner",
        "name: spawner\ntools:\n  - run_subagent\nsubagents:\n  - architect\n  - coder\n",
        _shared("A spawning agent."),
    )
    # Allow-list entries must resolve to real agent files — checked at
    # construction now, not only when a roster used to be rendered.
    _write_agent(tmp_path, "architect", "name: architect\n", _shared("## Purpose\n\nDesigns.\n"))
    _write_agent(tmp_path, "coder", "name: coder\n", _shared("## Purpose\n\nImplements.\n"))
    registry = AgentRegistry(tmp_path)
    assert registry.allowed_subagents("spawner") == frozenset(["architect", "coder"])


def test_registry_allowed_subagents_empty_when_none_declared(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "leaf", "name: leaf\n", _shared("A leaf agent."))
    registry = AgentRegistry(tmp_path)
    assert registry.allowed_subagents("leaf") == frozenset()


def test_registry_allowed_subagents_missing_agent_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    registry = AgentRegistry(tmp_path)
    with pytest.raises(AgentLoadError, match="No agent file"):
        registry.allowed_subagents("ghost")


def test_registry_missing_agent_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    registry = AgentRegistry(tmp_path)
    with pytest.raises(AgentLoadError, match="No agent file"):
        registry.get("nonexistent")


def test_registry_all_agents_returns_loaded(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", _shared("Prompt A."))
    _write_agent(tmp_path, "agent_b", "name: agent_b\n", _shared("Prompt B."))
    registry = AgentRegistry(tmp_path)
    names = {a.name for a in registry.all_agents()}
    assert names == {"agent_a", "agent_b"}


def test_registry_expands_shared_tokens_in_place(tmp_path: Path) -> None:
    """`{SHARED:x}` becomes `shared_x.md`'s text, exactly where the agent put it.

    Placement is the agent's, not the registry's — which is the whole point of
    the mechanism. Here the agent wraps the shared blocks in its own prose, and
    the result is its body with the tokens swapped, nothing prepended or
    appended.
    """
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\n",
        f"Before.\n\n{_REQUIRED_TOKENS}\n\nAfter.\n",
    )
    prompt = AgentRegistry(tmp_path).get("agent_a").system_prompt
    assert prompt == f"Before.\n\n{_REQUIRED_TEXT}\n\nAfter."
    assert "{SHARED:" not in prompt


def test_registry_expands_every_occurrence_of_a_token(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_shared(tmp_path, "note", "SHARED NOTE")
    token = shared_token("note")
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", _shared(f"{token} then {token}"))
    prompt = AgentRegistry(tmp_path).get("agent_a").system_prompt
    assert prompt.count("SHARED NOTE") == 2


def test_registry_leaves_an_agent_with_no_optional_blocks_alone(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_shared(tmp_path, "toolchain", "# Shared Toolchain Contract\n\nThe shared rules.")
    _write_agent(tmp_path, "plain", "name: plain\n", _shared("Just the body."))
    prompt = AgentRegistry(tmp_path).get("plain").system_prompt
    assert "The shared rules." not in prompt
    assert prompt == f"Just the body.\n\n{_REQUIRED_TEXT}"


def test_registry_shared_file_not_loaded_as_agent(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_shared(tmp_path, "toolchain", "# Shared\n\nRules.")
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", _shared("Body A."))
    registry = AgentRegistry(tmp_path)
    assert {a.name for a in registry.all_agents()} == {"agent_a"}


# ---------------------------------------------------------------------------
# Shared-block validation
#
# Nothing is auto-appended, so these checks are all that stand between a
# forgotten token and an agent shipping with no injection resistance. The same
# rules are re-run over every shipped agent file further down, so the failure
# normally lands at build time rather than at runtime.
# ---------------------------------------------------------------------------


def test_registry_unknown_shared_block_raises(tmp_path: Path) -> None:
    """A typo must not silently render nothing."""
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "tooler", "name: tooler\n", _shared(shared_token("ghost")))
    with pytest.raises(AgentLoadError, match="ghost"):
        AgentRegistry(tmp_path)


def test_registry_missing_required_block_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\n",
        f"Body with working rules but no security.\n\n{shared_token('working_rules')}\n",
    )
    with pytest.raises(AgentLoadError, match="security"):
        AgentRegistry(tmp_path)


def test_registry_file_modifying_tool_without_the_editing_block_raises(tmp_path: Path) -> None:
    """The invariant that keeps ``ToolSpec.modifies_files`` earning its keep."""
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "writer", f"name: writer\ntools:\n  - {_WRITE_TOOL}\n", _shared("B."))
    with pytest.raises(AgentLoadError, match="editing"):
        AgentRegistry(tmp_path)


def test_registry_read_only_agent_needs_no_editing_block(tmp_path: Path) -> None:
    """The other half: an agent that cannot write a file must not be told how.

    Sending the editing discipline everywhere was worse than wasteful — it
    names `edit_file`/`create_file` a critic was never granted, contradicting
    the "use only your granted tools" rule in the security block.
    """
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "reader", f"name: reader\ntools:\n  - {_READ_TOOL}\n", _shared("B."))
    _write_agent(tmp_path, "toolless", "name: toolless\n", _shared("B."))
    registry = AgentRegistry(tmp_path)
    assert _EDITING_TEXT not in registry.get("reader").system_prompt
    assert _EDITING_TEXT not in registry.get("toolless").system_prompt


def test_registry_editing_block_included_when_declared(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "writer",
        f"name: writer\ntools:\n  - {_WRITE_TOOL}\n",
        _shared("B.", shared_token("editing")),
    )
    assert _EDITING_TEXT in AgentRegistry(tmp_path).get("writer").system_prompt


def test_registry_callouts_block_is_pure_opt_in(tmp_path: Path) -> None:
    """Including the block *is* the opt-in; there is no frontmatter flag."""
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "talker", "name: talker\n", _shared("B.", shared_token("callouts")))
    _write_agent(tmp_path, "quiet", "name: quiet\n", _shared("B."))
    registry = AgentRegistry(tmp_path)
    assert _CALLOUTS_TEXT in registry.get("talker").system_prompt
    assert _CALLOUTS_TEXT not in registry.get("quiet").system_prompt


def test_task_input_block_rejected_in_an_agent_without_a_spec(tmp_path: Path) -> None:
    """A schema-less agent is never seeded from a structured ``task_input``.

    Letting it carry the note would promise it a first message that never
    arrives. The reverse — a schema-bearing agent omitting it — is a legitimate
    choice, not an error: ``compactor`` is handed a bare transcript rather than
    a rendered task turn, and documents that itself.
    """
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "nospec", "name: nospec\n", _shared("B.", shared_token("task_input")))
    with pytest.raises(AgentLoadError, match="task_input"):
        AgentRegistry(tmp_path)


def test_registry_nested_shared_token_raises(tmp_path: Path) -> None:
    """Substitution is one pass; a token inside a shared file would survive it.

    Rejecting these is also what makes include cycles impossible without a
    visited-set walk.
    """
    _write_preamble(tmp_path)
    _write_shared(tmp_path, "outer", f"Outer wraps {shared_token('security')}.")
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", _shared("B.", shared_token("outer")))
    with pytest.raises(AgentLoadError, match="single pass"):
        AgentRegistry(tmp_path)


def test_registry_empty_shared_file_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_shared(tmp_path, "toolchain", "   \n")
    _write_agent(tmp_path, "tooler", "name: tooler\n", _shared("B.", shared_token("toolchain")))
    with pytest.raises(AgentLoadError, match="empty"):
        AgentRegistry(tmp_path)


def test_registry_missing_shared_file_raises(tmp_path: Path) -> None:
    """A block an agent asks for but that does not exist is a hard error.

    There is no "missing block" default, silent or otherwise — a shared file
    that failed to ship is a packaging bug, and it must not degrade into a
    prompt quietly missing its rules.
    """
    _write_preamble(tmp_path)
    (tmp_path / f"{SHARED_FILE_PREFIX}security.md").unlink()
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", _shared("Prompt A."))
    with pytest.raises(AgentLoadError, match="security"):
        AgentRegistry(tmp_path)


# ---------------------------------------------------------------------------
# Granted tools: validation and the autonomous filter
#
# Tools are NOT described in an agent's prompt — they reach the model through
# the LLM tool-definition `tools` argument (see kodo.toolspecs.tool_description).
# The registry only validates the names and applies the autonomous filter.
# ---------------------------------------------------------------------------


def test_registry_never_describes_tools_in_the_prompt(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - filesystem\n  - read_file\n",
        _shared("Prompt A.\n\n## What to Avoid\n", shared_token("editing")),
    )
    registry = AgentRegistry(tmp_path)
    agent = registry.get("agent_a")
    # The grant is on the tool set...
    assert agent.tools == frozenset(["filesystem", "read_file"])
    # ...but nothing about the tools is rendered into the system prompt.
    for leaked in ("### Filesystem", "### Read File", "**When to use:**", "**Security impact:**"):
        assert leaked not in agent.system_prompt


def test_registry_leaves_a_tools_placeholder_untouched(tmp_path: Path) -> None:
    """The TOOLS placeholder is gone: a stray one is inert, not substituted."""
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - read_file\n",
        _shared("Prompt A.\n\n{PLACEHOLDER:TOOLS}\n"),
    )
    registry = AgentRegistry(tmp_path)
    assert "{PLACEHOLDER:TOOLS}" in registry.get("agent_a").system_prompt


def test_registry_unknown_tool_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - nonexistent_tool\n",
        _shared("Prompt A.\n\n## What to Avoid\n"),
    )
    with pytest.raises(AgentLoadError, match="nonexistent_tool"):
        AgentRegistry(tmp_path)


def test_registry_autonomous_filter_matches_live_spec_and_spares_ask_user(
    tmp_path: Path,
) -> None:
    """The autonomous-mode filter drops exactly the tools the live ToolSpec
    catalog marks 'unavailable' — read from the registry, not hardcoded — and
    ``ask_user`` is not among them: it stays granted in both modes and
    synthesizes its own answer when no user is present (see
    ``kodo.tools.AskUserTool``), so agent prompts never need to branch on
    mode to use it."""
    from kodo.subagents._registry import _AUTONOMOUS_DISABLED

    assert "ask_user" not in _AUTONOMOUS_DISABLED

    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - ask_user\n  - read_file\n",
        _shared("Prompt A.\n\n## What to Avoid\n"),
    )
    registry = AgentRegistry(tmp_path)
    interactive_tools = registry.get("agent_a").tools
    autonomous_tools = registry.get("agent_a", autonomous=True).tools
    assert interactive_tools == frozenset(["ask_user", "read_file"])
    assert autonomous_tools == interactive_tools - _AUTONOMOUS_DISABLED


# ---------------------------------------------------------------------------
# ## Purpose parsing (loader)
# ---------------------------------------------------------------------------


def test_load_agent_extracts_purpose_section(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "narrative_author",
        "name: narrative_author\n",
        "# Narrative Author\n\nIntro line.\n\n"
        "## Purpose\n\nWrites the narrative. Entry point.\n\n"
        "## Inputs\n\nThe engine delivers...\n",
    )
    agent = load_agent(path)
    assert agent.purpose == "Writes the narrative. Entry point."
    # No `role:` → an ordinary, invocable sub-agent; no `standalone:` → workflow.
    assert agent.role == ""
    assert agent.is_critic is False
    assert agent.standalone is False


def test_load_agent_parses_author_critic(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "architect",
        "name: architect\ncritic: architect_critic\n",
        "## Purpose\n\nDecomposes the narrative.\n",
    )
    agent = load_agent(path)
    assert agent.critic == "architect_critic"
    assert agent.is_critic is False
    assert agent.standalone is False
    assert agent.purpose == "Decomposes the narrative."


def test_load_agent_parses_standalone_flag(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "toolchain_builder",
        "name: toolchain_builder\nstandalone: true\n",
        "## Purpose\n\nSets up the toolchain on demand.\n",
    )
    agent = load_agent(path)
    assert agent.standalone is True
    assert agent.is_critic is False


def test_load_agent_parses_role_critic(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "architect_critic",
        "name: architect_critic\nrole: critic\n",
        "## Purpose\n\nReviews the decomposition.\n",
    )
    agent = load_agent(path)
    assert agent.role == "critic"
    assert agent.is_critic is True


def test_load_agent_rejects_unknown_role(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path, "odd", "name: odd\nrole: referee\n", "## Purpose\n\nAdjudicates.\n"
    )
    with pytest.raises(AgentLoadError, match="unknown role"):
        load_agent(path)


def test_load_agent_rejects_critic_that_declares_its_own_critic(tmp_path: Path) -> None:
    """A critic reviews; it is never itself reviewed. Allowing both would let
    the engine build an unbounded review-of-a-review chain."""
    path = _write_agent(
        tmp_path,
        "odd",
        "name: odd\nrole: critic\ncritic: someone\n",
        "## Purpose\n\nReviews.\n",
    )
    with pytest.raises(AgentLoadError, match="cannot itself declare"):
        load_agent(path)


def test_load_agent_no_purpose_yields_empty_string(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "leaf", "name: leaf\n", "# Leaf\n\nNo purpose section here.\n")
    agent = load_agent(path)
    assert agent.purpose == ""


def test_load_agent_preserves_subagent_order(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "caller",
        "name: caller\nsubagents:\n  - zeta\n  - alpha\n  - mid\n",
        "## Purpose\n\nA caller.\n",
    )
    agent = load_agent(path)
    # Order-preserving tuple keeps declaration order; the frozenset is unordered.
    assert agent.subagent_order == ("zeta", "alpha", "mid")
    assert agent.subagents == frozenset(["zeta", "alpha", "mid"])


# Sub-agent descriptions reach a caller on the generated tool, never in prose
#
# The `{PLACEHOLDER:SUBAGENTS}` roster (an intro, a tool/agent/review/kind
# table, and every callee's `## Purpose`) is gone. It was a prompt-side
# description of tools — what doc/TOOLS.md §7 forbids everywhere else — and it
# duplicated a second, terser `SubAgentSpec.description`. Both collapsed into
# the `run_subagent_<name>` tool the caller actually holds.
# ---------------------------------------------------------------------------

_REAL_AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "kodo" / "subagents"


def _tool_by_name(specs: list[ToolSpec], name: str) -> ToolSpec:
    match = next((s for s in specs if s.name == name), None)
    assert match is not None, f"no {name} in {[s.name for s in specs]}"
    return match


# ``run_subagent_specs`` only mints a tool for a *schema-bearing* sub-agent, and
# the specs are a real global registry — so these fixtures reuse real agent
# names (with controlled bodies) rather than invented ones.
_REAL_WORKFLOW_AGENT = "architect"          # workflow stage, reviewed by a critic
_REAL_CRITIC_AGENT = "architect_critic"     # its critic — never invocable
_REAL_STANDALONE_AGENT = "investigator"     # standalone specialist, no critic


def _write_callee_fixture(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        _REAL_WORKFLOW_AGENT,
        f"name: {_REAL_WORKFLOW_AGENT}\ncritic: {_REAL_CRITIC_AGENT}\n",
        _shared("## Purpose\n\nDecomposes the product. A pipeline stage.\n"),
    )
    _write_agent(
        tmp_path,
        _REAL_CRITIC_AGENT,
        f"name: {_REAL_CRITIC_AGENT}\nrole: critic\n",
        _shared("# Critic\n\nNo purpose section at all.\n"),
    )
    _write_agent(
        tmp_path,
        _REAL_STANDALONE_AGENT,
        f"name: {_REAL_STANDALONE_AGENT}\nstandalone: true\n",
        _shared("## Purpose\n\nOn-demand specialist; no pipeline dependency.\n"),
    )
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\nsubagents:\n"
        f"  - {_REAL_WORKFLOW_AGENT}\n  - {_REAL_CRITIC_AGENT}\n  - {_REAL_STANDALONE_AGENT}\n",
        _shared("Caller body."),
    )


def test_purpose_becomes_the_run_subagent_tool_description(tmp_path: Path) -> None:
    _write_callee_fixture(tmp_path)
    registry = AgentRegistry(tmp_path)
    specs = registry.run_subagent_specs("caller")
    description = _tool_by_name(specs, f"run_subagent_{_REAL_STANDALONE_AGENT}").description
    assert "On-demand specialist; no pipeline dependency." in description
    # And it is not *also* restated in the caller's prompt.
    assert "On-demand specialist" not in registry.get("caller").system_prompt


def test_tool_description_states_workflow_vs_standalone(tmp_path: Path) -> None:
    """The roster's `Kind` column, relocated to the description it belongs on."""
    _write_callee_fixture(tmp_path)
    specs = AgentRegistry(tmp_path).run_subagent_specs("caller")
    assert (
        "workflow stage"
        in _tool_by_name(specs, f"run_subagent_{_REAL_WORKFLOW_AGENT}").description
    )
    assert (
        "standalone specialist"
        in _tool_by_name(specs, f"run_subagent_{_REAL_STANDALONE_AGENT}").description
    )


def test_a_critic_is_never_invocable_and_needs_no_purpose(tmp_path: Path) -> None:
    """A critic has no tool, so nothing needs its purpose in a caller's view.

    The engine spawns it inside its author's loop; what the caller must know —
    that a review runs, and which critic runs it — is on the author's tool.
    """
    _write_callee_fixture(tmp_path)  # the critic fixture has no ## Purpose at all
    specs = AgentRegistry(tmp_path).run_subagent_specs("caller")
    assert f"run_subagent_{_REAL_CRITIC_AGENT}" not in {s.name for s in specs}
    author = _tool_by_name(specs, f"run_subagent_{_REAL_WORKFLOW_AGENT}")
    assert f"`{_REAL_CRITIC_AGENT}`" in author.description


def test_missing_purpose_on_an_invocable_subagent_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "leaf", "name: leaf\n", _shared("# Leaf\n\nNo purpose.\n"))
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\nsubagents:\n  - leaf\n",
        _shared("Caller body."),
    )
    with pytest.raises(AgentLoadError, match="Purpose"):
        AgentRegistry(tmp_path)


def test_unknown_subagent_reference_raises_at_construction(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\nsubagents:\n  - ghost\n",
        _shared("Caller body."),
    )
    with pytest.raises(AgentLoadError, match="ghost"):
        AgentRegistry(tmp_path)


# ---------------------------------------------------------------------------
# The shipped agent files
# ---------------------------------------------------------------------------


def test_no_shipped_prompt_describes_its_subagents() -> None:
    """No roster, no leftovers, in any real prompt."""
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    for agent in registry.all_agents():
        assert "PLACEHOLDER" not in agent.system_prompt, agent.name
        assert "| Sub-agent |" not in agent.system_prompt, agent.name


def test_shipped_guide_tools_carry_every_pipeline_pairing() -> None:
    """What left the guide's roster is on the guide's tools.

    Pairings are read off the live registry rather than restated, so this
    tracks the frontmatter instead of duplicating it.
    """
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    specs = {s.name: s for s in registry.run_subagent_specs("guide")}
    # narrative_author is the one pipeline stage with no critic.
    assert "workflow stage" in specs["run_subagent_narrative_author"].description
    for author in (
        "architect",
        "requirements_author",
        "functional_designer",
        "test_designer",
        "test_coder",
        "coder",
        "e2e_test_designer",
        "e2e_test_coder",
    ):
        critic = registry.get(author).critic
        assert critic, f"{author} is expected to be a reviewed stage"
        description = specs[f"run_subagent_{author}"].description
        assert f"`{critic}`" in description
        assert "workflow stage" in description
    # No critic is ever invocable in its own right.
    for critic_name in ("architect_critic", "code_critic", "test_design_critic"):
        assert f"run_subagent_{critic_name}" not in specs
    # The toolchain agent and the shared investigator are the adjunct entries.
    for name in ("toolchain_builder", "investigator"):
        assert "standalone specialist" in specs[f"run_subagent_{name}"].description


def test_shipped_problem_solver_delegates_to_four_standalone_specialists() -> None:
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    specs = {s.name: s for s in registry.run_subagent_specs("problem_solver")}
    assert set(specs) == {
        f"run_subagent_{n}"
        for n in ("investigator", "planner", "developer", "toolchain_builder")
    }
    for spec in specs.values():
        assert "standalone specialist" in spec.description


def test_real_judge_has_scoped_toolchain_build_tool() -> None:
    """Judge is almost entirely read-only, but carries one scoped exception.

    ``toolchain_build`` lets an RVP ask the judge for real, executed build/test
    evidence (doc/VALIDATOR.md §9.2) without granting general command
    execution, editing, or sub-agent capability.
    """
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    agent = registry.get("judge")
    assert agent.tools == frozenset(
        {"read_file", "find_files", "find_text_in_files", "toolchain_build", "submit_evaluation"}
    )
    # The grant is the tool set alone. The judge's own role instructions discuss
    # `toolchain_build` in prose, but no *rendered* spec block is injected — tool
    # descriptions reach the model via the LLM `tools` argument instead.
    assert "### Build & Test Project" not in agent.system_prompt


# ---------------------------------------------------------------------------
# Build-time scan of every shipped agent file
#
# The registry re-checks all of this at construction, but that is the
# last-resort copy: a forgotten `{SHARED:security}` should fail here, in CI,
# not on a running server. Everything below reads the raw `.md` (before token
# expansion) so it tests what an author actually wrote.
# ---------------------------------------------------------------------------


def _shipped_agent_files() -> list[Path]:
    files = sorted(_REAL_AGENTS_DIR.glob("subagent_*.md")) + sorted(
        _REAL_AGENTS_DIR.glob("agent_*.md")
    )
    assert files, "no agent files found; the fixture path is wrong"
    return files


@pytest.mark.parametrize("path", _shipped_agent_files(), ids=lambda p: p.stem)
def test_shipped_agent_includes_the_required_shared_blocks(path: Path) -> None:
    body = path.read_text(encoding="utf-8")
    for name in ("working_rules", "security"):
        assert shared_token(name) in body, f"{path.name} is missing {shared_token(name)}"


@pytest.mark.parametrize("path", _shipped_agent_files(), ids=lambda p: p.stem)
def test_shipped_agent_closes_with_the_security_block(path: Path) -> None:
    """Security is last in every prompt — highest precedence, least skimmable."""
    body = path.read_text(encoding="utf-8").rstrip()
    assert body.endswith(shared_token("security")), path.name


@pytest.mark.parametrize("path", _shipped_agent_files(), ids=lambda p: p.stem)
def test_shipped_agent_references_only_existing_shared_blocks(path: Path) -> None:
    body = path.read_text(encoding="utf-8")
    for name in set(re.findall(r"\{SHARED:([a-z0-9_]+)\}", body)):
        assert (_REAL_AGENTS_DIR / f"{SHARED_FILE_PREFIX}{name}.md").is_file(), (
            f"{path.name} includes {shared_token(name)} but no such shared file exists"
        )


@pytest.mark.parametrize("path", _shipped_agent_files(), ids=lambda p: p.stem)
def test_shipped_agent_with_write_tools_includes_the_editing_block(path: Path) -> None:
    agent = load_agent(path)
    granted = agent.tools & {t.name for t in ALL_TOOLS if t.modifies_files}
    body = path.read_text(encoding="utf-8")
    assert (shared_token("editing") in body) == bool(granted), (
        f"{path.name} grants {sorted(granted)} — the editing block must be present "
        f"exactly when it can change files"
    )


@pytest.mark.parametrize("path", _shipped_agent_files(), ids=lambda p: p.stem)
def test_shipped_agent_uses_no_retired_prompt_mechanism(path: Path) -> None:
    """`bases:`, `callouts:` and `{PLACEHOLDER:…}` are all gone; keep them gone.

    Each was a second way to do what `{SHARED:<name>}` does, and each was
    invisible in the file whose prompt it changed.
    """
    text = path.read_text(encoding="utf-8")
    assert "PLACEHOLDER" not in text, path.name
    assert not re.search(r"(?m)^bases:", text), path.name
    assert not re.search(r"(?m)^callouts:", text), path.name


def test_no_shared_file_includes_another() -> None:
    """One substitution pass is only sufficient while this holds."""
    for path in sorted(_REAL_AGENTS_DIR.glob(f"{SHARED_FILE_PREFIX}*.md")):
        assert "{SHARED:" not in path.read_text(encoding="utf-8"), path.name


def test_every_shared_file_is_used_by_some_agent() -> None:
    """An unreferenced shared file is dead prompt text — nothing renders it."""
    bodies = "\n".join(p.read_text(encoding="utf-8") for p in _shipped_agent_files())
    for path in sorted(_REAL_AGENTS_DIR.glob(f"{SHARED_FILE_PREFIX}*.md")):
        name = path.stem[len(SHARED_FILE_PREFIX) :]
        assert shared_token(name) in bodies, f"{path.name} is never included by any agent"
