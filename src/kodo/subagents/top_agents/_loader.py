"""Build :class:`~kodo.subagents.TopAgent` records from their JSON definitions.

Every entry is a ``<name>.json`` file next to this module. Nothing about how a
top-level agent is selected is hardcoded in Python any more: this module is the
one place that turns such a file into a record, and
:class:`~kodo.subagents.AgentRegistry` runs it over the directory it was built
from.

The file format
===============

::

    {
      "name": "problem_solver",
      "notes": "Why this entry looks the way it does (never shown to anyone).",
      "label": "Problem Solver",
      "description": "One generalist agent tackles your request end to end.",
      "rank": 10,
      "selectable": true,
      "default": false,
      "aliases": ["problem_solving"]
    }

``name`` must equal the filename stem *and* the ``agent_<name>.md`` stem — the
same rule ``specs/*.json`` already follows, so the config, the prompt and the
file name cannot drift apart. Only ``name`` and ``description`` are required;
every other key defaults the way the dataclass does.

Unknown keys are errors, and so are wrong types. A misspelled ``"selectible"``
that was silently ignored would produce an agent that quietly appears in a
picker it was meant to stay out of — precisely the class of failure fail-fast
checking exists to prevent.

Why this is read from a directory, not at import time
=====================================================

:mod:`kodo.subagents.specs` builds ``ALL_SUBAGENTS`` at import time by globbing
its *own* package directory. This package deliberately does **not**: it exports
a loader, and the registry calls it on whichever ``agents_dir`` it was
constructed with.

The difference matters because of the cross-check. The registry requires that a
top-level agent and its config each imply the other, in both directions. That is
only a true statement when both halves come from the same directory — a registry
built over a temp directory of synthetic agents (which every other test in
``test_agents.py`` does) legitimately has neither, while an import-time constant
would hand it the three packaged configs and no agents to match them.
"""

from __future__ import annotations

import json
from pathlib import Path

from .._topagent import TopAgent

__all__ = ["TOP_AGENT_SUFFIX", "TopAgentLoadError", "load_top_agent", "load_top_agents"]

#: Extension of a top-level agent's config file. The glob that finds them.
TOP_AGENT_SUFFIX = ".json"

_KEYS = frozenset(
    {"name", "notes", "label", "description", "rank", "selectable", "default", "aliases"}
)


class TopAgentLoadError(Exception):
    """Raised when a top-level agent's config file is malformed."""


def load_top_agent(path: Path) -> TopAgent:
    """Build one :class:`TopAgent` from its JSON definition.

    Args:
        path: Absolute path to a ``<name>.json`` config file.

    Returns:
        TopAgent: The record, with ``label`` left empty when the file omits it —
        the registry fills that from the agent's ``display_name``, which is the
        only place that knows it.

    Raises:
        TopAgentLoadError: The file is not valid JSON, is not a JSON object,
            holds an unknown key, declares a value of the wrong type, has a
            ``name`` that disagrees with the filename, or omits a required key.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TopAgentLoadError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise TopAgentLoadError(f"{path}: top level must be a JSON object")

    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise TopAgentLoadError(f"{path}: unknown key(s) {unknown}; known keys: {sorted(_KEYS)}")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise TopAgentLoadError(f"{path}: missing or empty 'name'")
    if name != path.stem:
        raise TopAgentLoadError(
            f"{path}: 'name' is {name!r} but the filename stem is {path.stem!r}"
        )

    description = raw.get("description")
    if not isinstance(description, str) or not description:
        raise TopAgentLoadError(
            f"{path}: missing or empty 'description' — it is the one sentence a "
            f"person reads when choosing this agent"
        )

    label = _string(raw, "label", path)
    notes = _string(raw, "notes", path)

    rank = raw.get("rank", 0)
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise TopAgentLoadError(f"{path}: 'rank' must be an integer, got {rank!r}")

    selectable = raw.get("selectable", True)
    if not isinstance(selectable, bool):
        raise TopAgentLoadError(f"{path}: 'selectable' must be true or false, got {selectable!r}")

    default = raw.get("default", False)
    if not isinstance(default, bool):
        raise TopAgentLoadError(f"{path}: 'default' must be true or false, got {default!r}")

    raw_aliases = raw.get("aliases", [])
    if not isinstance(raw_aliases, list) or any(
        not isinstance(a, str) or not a for a in raw_aliases
    ):
        raise TopAgentLoadError(f"{path}: 'aliases' must be a list of non-empty strings")

    return TopAgent(
        name=name,
        label=label,
        description=description,
        rank=rank,
        selectable=selectable,
        aliases=tuple(raw_aliases),
        default=default,
        notes=notes,
    )


def load_top_agents(directory: Path) -> tuple[TopAgent, ...]:
    """Build every top-level agent config in *directory*, in filename order.

    Every ``*.json`` file in the directory is a config — there is no manifest to
    keep in step, so adding one is adding a file. A directory that does not
    exist simply holds none, which is what a registry over a directory of
    sub-agents has.

    Two files cannot collide on a name, because a name *is* its filename stem.
    Collisions between a name and another entry's *alias* are cross-entry
    knowledge and are checked by the registry, which holds the whole set.

    Args:
        directory: Directory holding the ``<name>.json`` config files.

    Returns:
        tuple[TopAgent, ...]: One record per file, in filename order.

    Raises:
        TopAgentLoadError: Any file in the directory fails to load.
    """
    if not directory.is_dir():
        return ()
    return tuple(load_top_agent(path) for path in sorted(directory.glob(f"*{TOP_AGENT_SUFFIX}")))


def _string(raw: dict[str, object], key: str, path: Path) -> str:
    """Read an optional string key, rejecting a non-string that was written."""
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise TopAgentLoadError(f"{path}: {key!r} must be a string, got {value!r}")
    return value
