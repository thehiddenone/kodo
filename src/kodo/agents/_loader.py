"""Agent markdown file parser — frontmatter + system-prompt body.

Each agent file is a Markdown document with YAML frontmatter:

    ---
    name: narrative_author
    tools:
      - fileio_write_file
    ---
    <system prompt body>

The model is encoded in the filename: ``<name>.<model>.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Agent", "AgentLoadError", "load_agent"]

_FRONT_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


class AgentLoadError(Exception):
    """Raised when an agent file cannot be parsed or lacks required fields."""


@dataclass(frozen=True)
class Agent:
    """A loaded agent definition.

    Attributes:
        name: Agent name from frontmatter (e.g. ``'narrative_author'``).
        model: Model identifier from filename (e.g. ``'claude-sonnet-4-6'``).
        tools: MCP tool names this agent may invoke.
        system_prompt: Full system prompt body.
        source_path: Absolute path to the source ``.md`` file.
    """

    name: str
    model: str
    tools: frozenset[str]
    system_prompt: str
    source_path: Path


def load_agent(path: Path) -> Agent:
    """Parse a single agent markdown file.

    Args:
        path: Absolute path to the ``.md`` file.

    Returns:
        Agent: Fully populated agent dataclass.

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

    # Extract model from filename stem: <name>.<model>
    stem = path.stem  # removes .md
    prefix = name + "."
    if not stem.startswith(prefix):
        raise AgentLoadError(
            f"{path}: filename stem {stem!r} does not start with {prefix!r}"
        )
    model = stem[len(prefix):]
    if not model:
        raise AgentLoadError(f"{path}: cannot extract model from filename {path.name!r}")

    if not body:
        raise AgentLoadError(f"{path}: system-prompt body is empty")

    return Agent(name=name, model=model, tools=tools, system_prompt=body, source_path=path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    m = _FRONT_RE.match(text)
    if not m:
        raise AgentLoadError(f"{path}: missing --- frontmatter delimiters")

    fm_text = m.group(1)
    body = text[m.end():].strip()

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
