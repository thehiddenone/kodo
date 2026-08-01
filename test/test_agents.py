"""Behavior tests for kodo.subagents._loader and ._registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.subagents import AgentLoadError, AgentRegistry, SubAgent, load_agent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SECURITY_TEXT = "# Security Preamble\n\nThese rules apply to every sub-agent."
_PERFORMANCE_TEXT = "# Performance Preamble\n\nHow well you work."
# Security first, then performance — the order the registry prepends them in.
_PREAMBLE_TEXT = f"{_SECURITY_TEXT}\n\n{_PERFORMANCE_TEXT}"


def _write_agent(tmp_path: Path, name: str, frontmatter: str, body: str) -> Path:
    content = f"---\n{frontmatter}---\n{body}"
    p = tmp_path / f"subagent_{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_preamble(
    tmp_path: Path,
    security: str = _SECURITY_TEXT,
    performance: str = _PERFORMANCE_TEXT,
) -> None:
    (tmp_path / "preamble_security.md").write_text(security, encoding="utf-8")
    (tmp_path / "preamble_performance.md").write_text(performance, encoding="utf-8")


def _write_base(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / f"base_{name}.md"
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


def test_load_agent_no_bases_by_default(tmp_path: Path) -> None:
    path = _write_agent(tmp_path, "leaf_stub", "name: leaf_stub\n", "A leaf agent.")
    agent = load_agent(path)
    assert agent.bases == ()


def test_load_agent_parses_bases_list(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "tooler",
        "name: tooler\nbases:\n  - toolchain\n  - shared\n",
        "An agent built on shared bases.",
    )
    agent = load_agent(path)
    assert agent.bases == ("toolchain", "shared")


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
    _write_agent(tmp_path, "narrative_author", "name: narrative_author\n", "Narrative Author.")
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
        "A spawning agent.",
    )
    registry = AgentRegistry(tmp_path)
    assert registry.allowed_subagents("spawner") == frozenset(["architect", "coder"])


def test_registry_allowed_subagents_empty_when_none_declared(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "leaf", "name: leaf\n", "A leaf agent.")
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
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", "Prompt A.")
    _write_agent(tmp_path, "agent_b", "name: agent_b\n", "Prompt B.")
    registry = AgentRegistry(tmp_path)
    names = {a.name for a in registry.all_agents()}
    assert names == {"agent_a", "agent_b"}


def test_registry_prepends_both_preambles_to_every_prompt(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", "Prompt A.")
    _write_agent(tmp_path, "agent_b", "name: agent_b\n", "Prompt B.")
    registry = AgentRegistry(tmp_path)
    for agent in registry.all_agents():
        # Security comes first (it takes precedence), then performance, then body.
        assert agent.system_prompt.startswith(_PREAMBLE_TEXT)
        assert _SECURITY_TEXT in agent.system_prompt
        assert _PERFORMANCE_TEXT in agent.system_prompt
        assert agent.system_prompt.index(_SECURITY_TEXT) < agent.system_prompt.index(
            _PERFORMANCE_TEXT
        )
    assert registry.get("agent_a").system_prompt == f"{_PREAMBLE_TEXT}\n\nPrompt A."


def test_registry_prepends_base_after_preamble_before_body(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_base(tmp_path, "toolchain", "# Shared Toolchain Contract\n\nThe shared rules.")
    _write_agent(
        tmp_path,
        "tooler",
        "name: tooler\nbases:\n  - toolchain\n",
        "Agent-specific body.",
    )
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("tooler").system_prompt
    # preamble first, then base contract, then the agent body.
    assert prompt.startswith(_PREAMBLE_TEXT)
    assert "The shared rules." in prompt
    assert "Agent-specific body." in prompt
    assert prompt.index("The shared rules.") < prompt.index("Agent-specific body.")
    assert prompt.index(_PREAMBLE_TEXT) < prompt.index("The shared rules.")


def test_registry_agent_without_bases_has_no_base_text(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_base(tmp_path, "toolchain", "# Shared Toolchain Contract\n\nThe shared rules.")
    _write_agent(tmp_path, "plain", "name: plain\n", "Just the body.")
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("plain").system_prompt
    assert "The shared rules." not in prompt
    assert prompt == f"{_PREAMBLE_TEXT}\n\nJust the body."


def test_registry_unknown_base_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "tooler", "name: tooler\nbases:\n  - ghost\n", "Body.")
    with pytest.raises(AgentLoadError, match="ghost"):
        AgentRegistry(tmp_path)


def test_registry_empty_base_file_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_base(tmp_path, "toolchain", "   \n")
    _write_agent(tmp_path, "tooler", "name: tooler\nbases:\n  - toolchain\n", "Body.")
    with pytest.raises(AgentLoadError, match="empty"):
        AgentRegistry(tmp_path)


def test_registry_base_file_not_loaded_as_agent(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_base(tmp_path, "toolchain", "# Shared\n\nRules.")
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", "Body A.")
    registry = AgentRegistry(tmp_path)
    names = {a.name for a in registry.all_agents()}
    assert names == {"agent_a"}


def test_registry_missing_performance_preamble_raises(tmp_path: Path) -> None:
    # Only the security preamble present — the performance one is mandatory too.
    (tmp_path / "preamble_security.md").write_text(_SECURITY_TEXT, encoding="utf-8")
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", "Prompt A.")
    with pytest.raises(AgentLoadError, match="preamble"):
        AgentRegistry(tmp_path)


def test_registry_missing_preamble_raises(tmp_path: Path) -> None:
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", "Prompt A.")
    with pytest.raises(AgentLoadError, match="preamble"):
        AgentRegistry(tmp_path)


def test_registry_empty_preamble_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path, "   \n")
    _write_agent(tmp_path, "agent_a", "name: agent_a\n", "Prompt A.")
    with pytest.raises(AgentLoadError, match="empty"):
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
        "Prompt A.\n\n## What to Avoid\n",
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
        "Prompt A.\n\n{PLACEHOLDER:TOOLS}\n",
    )
    registry = AgentRegistry(tmp_path)
    assert "{PLACEHOLDER:TOOLS}" in registry.get("agent_a").system_prompt


def test_registry_unknown_tool_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - nonexistent_tool\n",
        "Prompt A.\n\n## What to Avoid\n",
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
        "Prompt A.\n\n## What to Avoid\n",
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


# ---------------------------------------------------------------------------
# {PLACEHOLDER:SUBAGENTS} roster rendering
# ---------------------------------------------------------------------------


def _write_pipeline_fixture(tmp_path: Path) -> None:
    """A mini pipeline (reviewed and unreviewed agents) plus a caller.

    Mirrors the general shape: an unreviewed entry-point agent, two agents each
    paired with a critic, a shared critic reviewing two of them, and a
    standalone specialist outside the pipeline.
    """
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "writer",
        "name: writer\ndisplay_name: Writer\n",
        "## Purpose\n\nWrites the seed doc. Entry point.\n",
    )
    _write_agent(
        tmp_path,
        "designer",
        "name: designer\ndisplay_name: Designer\ncritic: reviewer\n",
        "## Purpose\n\nDesigns. Author whose critic is `reviewer`.\n",
    )
    _write_agent(
        tmp_path,
        "builder",
        "name: builder\ndisplay_name: Builder\n",
        "## Purpose\n\nBuilds, unreviewed.\n",
    )
    _write_agent(
        tmp_path,
        "coder",
        "name: coder\ndisplay_name: Coder\ncritic: reviewer\n",
        "## Purpose\n\nImplements. Author paired with `reviewer`.\n",
    )
    _write_agent(
        tmp_path,
        "reviewer",
        "name: reviewer\ndisplay_name: Reviewer\nrole: critic\n",
        "## Purpose\n\nReviews `coder`'s output as critic.\n",
    )
    # A standalone specialist outside the pipeline (gets a `standalone` Kind).
    _write_agent(
        tmp_path,
        "helper",
        "name: helper\ndisplay_name: Helper\nstandalone: true\n",
        "## Purpose\n\nOn-demand specialist; no pipeline dependency.\n",
    )
    # Caller lists them in pipeline order, critics interleaved, helper last.
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\n"
        "subagents:\n  - writer\n  - designer\n  - builder\n  - coder\n  - reviewer\n  - helper\n",
        "Caller body.\n\n## Subagents\n\n{PLACEHOLDER:SUBAGENTS}\n\n## End\n",
    )


def test_subagents_roster_table_rows_and_tools(tmp_path: Path) -> None:
    _write_pipeline_fixture(tmp_path)
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("caller").system_prompt
    assert "{PLACEHOLDER:SUBAGENTS}" not in prompt

    # Every invocable sub-agent gets its own tool; a reviewed one names the
    # critic the engine runs inside that call. Kind marks pipeline membership.
    assert (
        "| `run_subagent_designer` | `designer` | `reviewer`, automatically | workflow |" in prompt
    )
    assert "| `run_subagent_coder` | `coder` | `reviewer`, automatically | workflow |" in prompt
    # Unreviewed agents say so explicitly rather than leaving the cell blank.
    assert "| `run_subagent_writer` | `writer` | none — single pass | workflow |" in prompt
    assert "| `run_subagent_builder` | `builder` | none — single pass | workflow |" in prompt
    # A standalone agent is marked `standalone` in the Kind column.
    assert "| `run_subagent_helper` | `helper` | none — single pass | standalone |" in prompt
    # The critic `reviewer` is absorbed into its authors' rows and is never
    # invocable itself — no tool is ever minted for it.
    assert "run_subagent_reviewer" not in prompt
    # The intro paragraph explaining the Kind column precedes the table.
    assert "**Workflow** sub-agents" in prompt
    assert "**Standalone** sub-agents" in prompt
    assert prompt.index("**Workflow** sub-agents") < prompt.index("| Tool |")


def test_subagents_roster_includes_purpose_for_every_listed_agent(tmp_path: Path) -> None:
    _write_pipeline_fixture(tmp_path)
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("caller").system_prompt
    # Every sub-agent — authors, solos, AND pure critics — gets a purpose para.
    for name, display in [
        ("writer", "Writer"),
        ("designer", "Designer"),
        ("builder", "Builder"),
        ("coder", "Coder"),
        ("reviewer", "Reviewer"),
        ("helper", "Helper"),
    ]:
        assert f"### {display} (`{name}`)" in prompt
    assert "Reviews `coder`'s output as critic." in prompt


def test_subagents_roster_orders_by_allow_list_then_table_then_purposes(tmp_path: Path) -> None:
    _write_pipeline_fixture(tmp_path)
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("caller").system_prompt
    # Intro paragraph, then table, then the purpose paragraphs.
    assert prompt.index("**Workflow** sub-agents") < prompt.index("| Tool |")
    assert prompt.index("| Tool |") < prompt.index("### Writer (`writer`)")
    # Purpose paragraphs follow allow-list order (designer before builder, etc.).
    assert prompt.index("### Writer") < prompt.index("### Designer")
    assert prompt.index("### Designer") < prompt.index("### Builder")
    assert prompt.index("### Builder") < prompt.index("### Coder")
    assert prompt.index("### Coder") < prompt.index("### Reviewer")
    assert prompt.index("### Reviewer") < prompt.index("### Helper")


def test_subagents_roster_render_via_public_method(tmp_path: Path) -> None:
    _write_pipeline_fixture(tmp_path)
    registry = AgentRegistry(tmp_path)
    # Public method renders the same roster even for a caller without the
    # placeholder embedded (used by prompt-review tooling).
    section = registry.render_subagents_section("caller")
    assert section.startswith("Each row's **Tool**")
    assert "| Tool |" in section
    assert "### Writer (`writer`)" in section


def test_subagents_missing_purpose_raises_at_construction(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "leaf", "name: leaf\n", "# Leaf\n\nNo purpose.\n")
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\nsubagents:\n  - leaf\n",
        "## Subagents\n\n{PLACEHOLDER:SUBAGENTS}\n",
    )
    with pytest.raises(AgentLoadError, match="Purpose"):
        AgentRegistry(tmp_path)


def test_subagents_unknown_reference_raises_at_construction(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\nsubagents:\n  - ghost\n",
        "## Subagents\n\n{PLACEHOLDER:SUBAGENTS}\n",
    )
    with pytest.raises(AgentLoadError, match="ghost"):
        AgentRegistry(tmp_path)


def test_agent_without_subagents_placeholder_is_untouched(tmp_path: Path) -> None:
    # An agent that lists subagents but does NOT embed the placeholder renders
    # normally — no roster injected, no purpose validation forced.
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "leaf", "name: leaf\n", "# Leaf\n\nBody, no purpose.\n")
    _write_agent(
        tmp_path,
        "caller",
        "name: caller\ntools:\n  - run_subagent\nsubagents:\n  - leaf\n",
        "Body without a subagents section.",
    )
    registry = AgentRegistry(tmp_path)  # must not raise despite leaf lacking purpose
    assert "{PLACEHOLDER:SUBAGENTS}" not in registry.get("caller").system_prompt


# ---------------------------------------------------------------------------
# Real subagent files — the shipped roster is well-formed
# ---------------------------------------------------------------------------

_REAL_AGENTS_DIR = Path(__file__).resolve().parents[1] / "src" / "kodo" / "subagents"


def test_real_problem_solver_renders_subagent_roster() -> None:
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    prompt = registry.get("problem_solver").system_prompt
    assert "{PLACEHOLDER:SUBAGENTS}" not in prompt
    # Problem Solver orchestrates four standalone solos: its own investigate ->
    # plan -> develop trio plus the toolchain setup agent.
    for name in ("investigator", "planner", "developer", "toolchain_builder"):
        assert f"| `run_subagent_{name}` | `{name}` | none — single pass | standalone |" in prompt
    assert "### Investigator (`investigator`)" in prompt
    assert "### Planner (`planner`)" in prompt
    assert "### Developer (`developer`)" in prompt
    assert "### Toolchain Builder (`toolchain_builder`)" in prompt


def test_real_guide_roster_reproduces_pipeline_pairs() -> None:
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    # The guide embeds {PLACEHOLDER:SUBAGENTS}; render the live system prompt.
    prompt = registry.get("guide").system_prompt
    assert "{PLACEHOLDER:SUBAGENTS}" not in prompt
    section = registry.render_subagents_section("guide")
    # narrative_author is the one pipeline stage with no critic.
    assert (
        "| `run_subagent_narrative_author` | `narrative_author` | none — single pass | workflow |"
        in section
    )
    # Every other workflow stage runs its critic inside the same call. Read the
    # pairings off the live registry rather than restating them, so this test
    # tracks the frontmatter instead of duplicating it.
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
        assert (
            f"| `run_subagent_{author}` | `{author}` | `{critic}`, automatically | workflow |"
            in section
        )
    # No critic is ever invocable in its own right.
    for critic_name in ("architect_critic", "code_critic", "test_design_critic"):
        assert f"run_subagent_{critic_name}" not in section
    # The toolchain agent and the shared investigator are the standalone
    # (adjunct) entries in the guide roster.
    for name in ("toolchain_builder", "investigator"):
        assert f"| `run_subagent_{name}` | `{name}` | none — single pass | standalone |" in section


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
