"""Agent markdown file parser — frontmatter + system-prompt body.

Each agent file is a Markdown document with YAML frontmatter:

    ---
    name: narrative_author
    tools:
      - fileio_write_file
    ---
    <system prompt body>

The filename stem must be ``subagent_<name>`` for sub-agents (everything driven
by ``run_subagent`` / ``run_author_critic_iteration``) or ``agent_<name>`` for
the user-facing entry agents (``guide``, ``problem_solver``) that drive a
session directly rather than being spawned by one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SubAgent", "AgentLoadError", "load_agent"]

_FRONT_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

# Captures the body of a ``## Purpose`` section: everything after the heading
# line up to (but not including) the next ``#``/``##`` heading or end of file.
_PURPOSE_RE = re.compile(r"(?ms)^##[ \t]+Purpose[ \t]*$\n?(.*?)(?=^#{1,2}[ \t]|\Z)")


class AgentLoadError(Exception):
    """Raised when a subagent file cannot be parsed or lacks required fields."""


@dataclass(frozen=True)
class SubAgent:
    """A loaded subagent definition.

    Attributes:
        name: Subagent name from frontmatter (e.g. ``'narrative_author'``).
        tools: MCP tool names this subagent may invoke.
        subagents: Names of sub-agents this agent is permitted to spawn (via
            ``run_subagent`` / ``run_author_critic_iteration``). Empty by default,
            so an agent can spawn nothing unless its frontmatter opts in. There is
            no built-in "only the guide spawns" assumption — any agent that
            declares both a spawning tool and a ``subagents`` allow-list can drive
            sub-agents, and the engine enforces the allow-list at dispatch time.
        system_prompt: Full system prompt body.
        source_path: Absolute path to the source ``.md`` file.
        capability: Preferred LLM capability tier — ``'max'``, ``'high'``,
            ``'medium'``, or ``'low'``.  Defaults to ``'medium'`` when not set
            in frontmatter.
        display_name: User-friendly name shown in the UI (e.g. in subsession
            takeover dividers). Falls back to a title-cased ``name`` when the
            frontmatter does not set ``display_name``.
        bases: Names of shared base snippets (``base_<name>.md`` in the subagents
            dir) whose bodies are prepended to this agent's prompt at render time,
            after the global preambles and before the agent's own body. Empty by
            default. Lets a family of agents (e.g. the toolchain-setup agents)
            share one contract without duplicating it; the registry validates each
            reference exists at load time.
        subagent_order: The ``subagents:`` allow-list in declaration order. Same
            membership as :attr:`subagents` (a set, order-free, used for the
            dispatch gate), but order-preserving so a caller's ``## Subagents``
            roster table/paragraphs render in the order the author listed them.
        purpose: Body of this agent's ``## Purpose`` section — a *caller-agnostic*
            description of what the agent does and when to call it. Empty when the
            file has no ``## Purpose`` section. The registry renders it into a
            caller's roster when filling ``{PLACEHOLDER:SUBAGENTS}``.
        solo: ``True`` when this agent is invoked on its own via ``run_subagent``
            (frontmatter ``solo: true``). Gives it a ``run_subagent`` row in a
            caller's roster table. Mutually informative with :attr:`critic`.
        critic: Name of the critic this agent is paired with (frontmatter
            ``critic:``). A non-empty value marks the agent an **author**, driven
            via ``run_author_critic_iteration``; it gets one roster row naming the
            critic. Empty for solos and for critics themselves.
        standalone: ``True`` when this agent is **not** part of the ordered
            pipeline (frontmatter ``standalone: true``) — a specialist invoked on
            demand whenever the need arises, with no upstream dependency on any
            other agent's output. ``False`` (the default) marks a **workflow**
            agent that advances the pre-determined pipeline and consumes the
            artifacts of the stage before it. Shown as the ``Kind`` column in a
            caller's roster table.
    """

    name: str
    tools: frozenset[str]
    system_prompt: str
    source_path: Path
    capability: str = "medium"
    display_name: str = ""
    subagents: frozenset[str] = frozenset()
    bases: tuple[str, ...] = ()
    subagent_order: tuple[str, ...] = ()
    purpose: str = ""
    solo: bool = False
    critic: str = ""
    standalone: bool = False


def load_agent(path: Path) -> SubAgent:
    """Parse a single subagent markdown file.

    Args:
        path: Absolute path to the ``.md`` file.

    Returns:
        SubAgent: Fully populated subagent dataclass.

    Raises:
        AgentLoadError: File is missing frontmatter, a required field, or has an
            empty system-prompt body.
    """
    text = path.read_text(encoding="utf-8")
    fm_dict, body = _parse_frontmatter(text, path)

    name = fm_dict.get("name")
    if not isinstance(name, str) or not name:
        raise AgentLoadError(f"{path}: missing or empty 'name' in frontmatter")

    tools_raw = fm_dict.get("tools", [])
    if isinstance(tools_raw, list):
        tools: frozenset[str] = frozenset(str(t) for t in tools_raw)
    elif isinstance(tools_raw, str):
        tools = frozenset([tools_raw])
    else:
        tools = frozenset()

    subagents_raw = fm_dict.get("subagents", [])
    if isinstance(subagents_raw, list):
        subagent_order: tuple[str, ...] = tuple(str(s) for s in subagents_raw)
    elif isinstance(subagents_raw, str):
        subagent_order = (subagents_raw,)
    else:
        subagent_order = ()
    subagents: frozenset[str] = frozenset(subagent_order)

    bases_raw = fm_dict.get("bases", [])
    if isinstance(bases_raw, list):
        bases: tuple[str, ...] = tuple(str(b) for b in bases_raw)
    elif isinstance(bases_raw, str):
        bases = (bases_raw,)
    else:
        bases = ()

    expected_stems = (f"subagent_{name}", f"agent_{name}")
    if path.stem not in expected_stems:
        raise AgentLoadError(
            f"{path}: filename stem {path.stem!r} does not match expected "
            f"{expected_stems[0]!r} or {expected_stems[1]!r}"
        )

    if not body:
        raise AgentLoadError(f"{path}: system-prompt body is empty")

    capability_raw = fm_dict.get("capability", "medium")
    capability = str(capability_raw) if isinstance(capability_raw, str) else "medium"
    if capability not in ("max", "high", "medium", "low"):
        capability = "medium"

    display_raw = fm_dict.get("display_name")
    display_name = (
        str(display_raw).strip()
        if isinstance(display_raw, str) and display_raw.strip()
        else _default_display_name(name)
    )

    solo = _scalar(fm_dict.get("solo")).lower() in ("true", "yes", "1")
    critic = _scalar(fm_dict.get("critic"))
    standalone = _scalar(fm_dict.get("standalone")).lower() in ("true", "yes", "1")
    purpose = _extract_purpose(body)

    return SubAgent(
        name=name,
        tools=tools,
        system_prompt=body,
        source_path=path,
        capability=capability,
        display_name=display_name,
        subagents=subagents,
        bases=bases,
        subagent_order=subagent_order,
        purpose=purpose,
        solo=solo,
        critic=critic,
        standalone=standalone,
    )


def _scalar(value: object) -> str:
    """Coerce a frontmatter value to a trimmed scalar string.

    The lightweight frontmatter parser yields scalars as ``str`` and lists as
    ``list[str]``; an empty scalar (``key:`` with nothing after the colon) comes
    back as an empty list. Normalize all of these to a plain string.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return ""


def _extract_purpose(body: str) -> str:
    """Return the body of the ``## Purpose`` section, or ``""`` when absent."""
    m = _PURPOSE_RE.search(body)
    return m.group(1).strip() if m else ""


def _default_display_name(name: str) -> str:
    """Derive a user-friendly name from a snake_case agent name.

    ``narrative_author`` → ``"Narrative Author"``.
    """
    return " ".join(part.capitalize() for part in name.split("_") if part) or name


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    m = _FRONT_RE.match(text)
    if not m:
        raise AgentLoadError(f"{path}: missing --- frontmatter delimiters")

    fm_text = m.group(1)
    body = text[m.end() :].strip()

    result: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] = []

    def _flush() -> None:
        if current_key is not None:
            result[current_key] = list(current_list)

    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            current_list.append(stripped[2:].strip())
        elif ":" in stripped:
            _flush()
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            current_key = key
            current_list = []
            if val:
                result[key] = val
                current_key = None

    _flush()
    return result, body
