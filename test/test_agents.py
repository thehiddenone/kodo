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
# Tools section rendering
# ---------------------------------------------------------------------------


def test_registry_renders_tools_section_for_agent_tools(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - filesystem\n  - read_file\n",
        "Prompt A.\n\n## Tools\n\n{PLACEHOLDER:TOOLS}\n\n## What to Avoid\n",
    )
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("agent_a").system_prompt
    assert "{PLACEHOLDER:TOOLS}" not in prompt
    assert "### Filesystem (`filesystem`)" in prompt
    assert "### Read File (`read_file`)" in prompt
    assert "- **When to use:**" in prompt
    assert "- **External name:**" not in prompt
    assert "- **Description:**" not in prompt
    # Tools are rendered in a stable, sorted order.
    assert prompt.index("Filesystem") < prompt.index("Read File")


def test_registry_renders_empty_tools_section_for_agent_with_no_tools(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\n",
        "Prompt A.\n\n## Tools\n\n{PLACEHOLDER:TOOLS}\n\n## What to Avoid\n",
    )
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("agent_a").system_prompt
    assert "{PLACEHOLDER:TOOLS}" not in prompt
    assert "## Tools\n\n\n\n## What to Avoid" in prompt


def test_registry_unknown_tool_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - nonexistent_tool\n",
        "Prompt A.\n\n## Tools\n\n{PLACEHOLDER:TOOLS}\n\n## What to Avoid\n",
    )
    with pytest.raises(AgentLoadError, match="nonexistent_tool"):
        AgentRegistry(tmp_path)


def test_registry_ask_user_unavailable_in_autonomous_mode(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "agent_a",
        "name: agent_a\ntools:\n  - ask_user\n  - read_file\n",
        "Prompt A.\n\n## Tools\n\n{PLACEHOLDER:TOOLS}\n\n## What to Avoid\n",
    )
    registry = AgentRegistry(tmp_path)
    agent = registry.get("agent_a", autonomous=True)
    assert agent.tools == frozenset(["read_file"])
    assert "ask_user" not in agent.system_prompt
    assert "### Read File" in agent.system_prompt


# ---------------------------------------------------------------------------
# ## Purpose parsing (loader)
# ---------------------------------------------------------------------------


def test_load_agent_extracts_purpose_section(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "narrative_author",
        "name: narrative_author\nsolo: true\n",
        "# Narrative Author\n\nIntro line.\n\n"
        "## Purpose\n\nWrites the narrative. Entry point.\n\n"
        "## Inputs\n\nThe engine delivers...\n",
    )
    agent = load_agent(path)
    assert agent.purpose == "Writes the narrative. Entry point."
    assert agent.solo is True
    # A workflow agent (no `standalone:` flag) defaults to standalone=False.
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
    assert agent.solo is False
    assert agent.standalone is False
    assert agent.purpose == "Decomposes the narrative."


def test_load_agent_parses_standalone_flag(tmp_path: Path) -> None:
    path = _write_agent(
        tmp_path,
        "toolchain_python",
        "name: toolchain_python\nsolo: true\nstandalone: true\n",
        "## Purpose\n\nSets up the toolchain on demand.\n",
    )
    agent = load_agent(path)
    assert agent.solo is True
    assert agent.standalone is True


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
    """A mini author/critic + solo pipeline plus a caller that lists them all.

    Mirrors a general shape: an entry-point solo, an author/critic pair, and a
    second solo that is *also* the author's critic — the renderer must still
    collapse a solo+critic into one combined row (no live pipeline agent is one
    today, but the rendering path is still supported).
    """
    _write_preamble(tmp_path)
    _write_agent(
        tmp_path,
        "writer",
        "name: writer\ndisplay_name: Writer\nsolo: true\n",
        "## Purpose\n\nWrites the seed doc. Entry point.\n",
    )
    _write_agent(
        tmp_path,
        "designer",
        "name: designer\ndisplay_name: Designer\ncritic: builder\n",
        "## Purpose\n\nDesigns. Author whose critic is `builder`.\n",
    )
    _write_agent(
        tmp_path,
        "builder",
        "name: builder\ndisplay_name: Builder\nsolo: true\n",
        "## Purpose\n\nValidates `designer` as critic, then builds solo.\n",
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
        "name: reviewer\ndisplay_name: Reviewer\n",
        "## Purpose\n\nReviews `coder`'s output as critic.\n",
    )
    # A standalone specialist outside the pipeline (gets a `standalone` Kind).
    _write_agent(
        tmp_path,
        "helper",
        "name: helper\ndisplay_name: Helper\nsolo: true\nstandalone: true\n",
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

    # Author rows use run_author_critic_iteration and name the critic; Kind marks
    # pipeline membership (workflow vs standalone).
    assert "| `run_author_critic_iteration` | `designer` | `builder` | workflow |" in prompt
    assert "| `run_author_critic_iteration` | `coder` | `reviewer` | workflow |" in prompt
    # Solos use run_subagent with no critic.
    assert "| `run_subagent` | `writer` | — | workflow |" in prompt
    assert "| `run_subagent` | `builder` | — | workflow |" in prompt
    # A standalone solo is marked `standalone` in the Kind column.
    assert "| `run_subagent` | `helper` | — | standalone |" in prompt
    # The pure critic `reviewer` is absorbed into coder's row — it gets no row
    # of its own (no run_subagent / run_author_critic_iteration line for it).
    assert "| `run_subagent` | `reviewer` |" not in prompt
    assert "`reviewer` | —" not in prompt
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
    assert section.startswith("The sub-agents below")
    assert "| Tool |" in section
    assert "### Writer (`writer`)" in section


def test_subagents_missing_purpose_raises_at_construction(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    _write_agent(tmp_path, "leaf", "name: leaf\nsolo: true\n", "# Leaf\n\nNo purpose.\n")
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
    _write_agent(tmp_path, "leaf", "name: leaf\nsolo: true\n", "# Leaf\n\nBody, no purpose.\n")
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
    assert "| `run_subagent` | `investigator` | — | standalone |" in prompt
    assert "| `run_subagent` | `planner` | — | standalone |" in prompt
    assert "| `run_subagent` | `developer` | — | standalone |" in prompt
    assert "| `run_subagent` | `toolchain_python` | — | standalone |" in prompt
    assert "### Investigator (`investigator`)" in prompt
    assert "### Planner (`planner`)" in prompt
    assert "### Developer (`developer`)" in prompt
    assert "### Python Toolchain (`toolchain_python`)" in prompt


def test_real_guide_roster_reproduces_pipeline_pairs() -> None:
    registry = AgentRegistry(_REAL_AGENTS_DIR)
    # The guide embeds {PLACEHOLDER:SUBAGENTS}; render the live system prompt.
    prompt = registry.get("guide").system_prompt
    assert "{PLACEHOLDER:SUBAGENTS}" not in prompt
    section = registry.render_subagents_section("guide")
    assert "| `run_subagent` | `narrative_author` | — | workflow |" in section
    assert (
        "| `run_author_critic_iteration` | `architect` | `architect_critic` | workflow |" in section
    )
    # test_designer is paired with test_design_critic (a pure critic, absorbed
    # into the author's row); test_coder is now a plain solo row.
    assert "| `run_author_critic_iteration` | `test_designer` | `test_design_critic` |" in section
    assert "| `run_subagent` | `test_coder` | — | workflow |" in section
    # The two product-level end-to-end stages, each an author/critic pair.
    assert (
        "| `run_author_critic_iteration` | `e2e_test_designer` | `e2e_test_design_critic` |"
        in section
    )
    assert (
        "| `run_author_critic_iteration` | `e2e_test_coder` | `e2e_test_code_critic` |" in section
    )
    # The toolchain agent is the one standalone (adjunct) entry in the guide roster.
    assert "| `run_subagent` | `toolchain_python` | — | standalone |" in section
