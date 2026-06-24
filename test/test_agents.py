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
    with pytest.raises(AgentLoadError, match="No subagent file"):
        registry.allowed_subagents("ghost")


def test_registry_missing_agent_raises(tmp_path: Path) -> None:
    _write_preamble(tmp_path)
    registry = AgentRegistry(tmp_path)
    with pytest.raises(AgentLoadError, match="No subagent file"):
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
        "name: agent_a\ntools:\n  - create_file\n  - read_artifact\n",
        "Prompt A.\n\n## Tools\n\n{PLACEHOLDER:TOOLS}\n\n## What to Avoid\n",
    )
    registry = AgentRegistry(tmp_path)
    prompt = registry.get("agent_a").system_prompt
    assert "{PLACEHOLDER:TOOLS}" not in prompt
    assert "### Create File (`create_file`)" in prompt
    assert "### Read Artifact (`read_artifact`)" in prompt
    assert "- **When to use:**" in prompt
    assert "- **External name:**" not in prompt
    assert "- **Description:**" not in prompt
    # Tools are rendered in a stable, sorted order.
    assert prompt.index("Create File") < prompt.index("Read Artifact")


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
        "name: agent_a\ntools:\n  - ask_user\n  - read_artifact\n",
        "Prompt A.\n\n## Tools\n\n{PLACEHOLDER:TOOLS}\n\n## What to Avoid\n",
    )
    registry = AgentRegistry(tmp_path)
    agent = registry.get("agent_a", autonomous=True)
    assert agent.tools == frozenset(["read_artifact"])
    assert "ask_user" not in agent.system_prompt
    assert "### Read Artifact" in agent.system_prompt
