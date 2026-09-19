"""Top-level agent configs — the :class:`TopAgent` record and its JSON files.

A **top-level agent** is the kind a user selects and talks to directly
(``guide``, ``problem_solver``, ``judge``), as opposed to a sub-agent another
agent delegates to (:mod:`kodo.agents.subagents`). Where a sub-agent's extra
declaration is a *typed contract* (:class:`~kodo.agents.subagents.SubAgentSpec`),
a top-level agent has none — it talks to a human in prose — so what it needs
declared instead is how it is **chosen**: which wire value selects it, what to
call it in a picker, and whether it belongs in one at all.

That is everything here. Behaviour still comes from the ``agent_<name>.md``
prompt and its frontmatter (tools, capability, sub-agent roster); this record
carries only what the engine and the UI need to route to it.

The file format
===============

One ``<name>.json`` beside the ``agent_<name>.md`` it describes::

    {
      "name": "problem_solver",
      "notes": "Why this entry looks the way it does (never shown to anyone).",
      "label": "Problem Solver",
      "description": "One generalist agent tackles your request end to end.",
      "rank": 10,
      "selectable": true,
      "default": false
    }

``name`` must equal the filename stem, and an ``agent_<name>.md`` must exist for
it — the registry checks both directions. Only ``name`` and ``description`` are
required; every other key defaults the way the dataclass does.

Unknown keys are errors, and so are wrong types. A misspelled ``"selectible"``
that was silently ignored would produce an agent that quietly appears in a
picker it was meant to stay out of — precisely the class of failure fail-fast
checking exists to prevent.

Why the configs are read from a directory, not at import time
=============================================================

:mod:`kodo.agents.subagents.specs` builds ``ALL_SUBAGENTS`` at import time by
globbing its *own* package directory. :func:`load_top_agents` deliberately does
not: the registry calls it on whichever ``agents_dir`` it was constructed with.

The difference matters because of the cross-check. The registry requires that a
top-level agent and its config each imply the other, in both directions. That is
only a true statement when both halves come from the same directory — a registry
built over a temp directory of synthetic agents (which every other test in
``test_agents.py`` does) legitimately has neither, while an import-time constant
would hand it the three packaged configs and no agents to match them.

Naming (doc/TOP_AGENT_PLAN.md §1): the concept is spelled ``agent`` wherever the
word is free, and ``top_agent`` wherever it is already taken by something else.
It is taken here — :attr:`kodo.runtime.SessionState.agent` is the agent
*currently holding the floor*, which is frequently a sub-agent mid-pipeline —
so the record, the registry accessors and the session field all say
``top_agent``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TOP_AGENT_SUFFIX",
    "TopAgent",
    "TopAgentLoadError",
    "load_top_agent",
    "load_top_agents",
]


@dataclass(frozen=True)
class TopAgent:
    """How one top-level agent is selected and presented.

    Attributes:
        name: The agent's name — matches :attr:`kodo.agents.SubAgent.name`
            and the ``agent_<name>.md`` filename stem. This is also the value
            that selects it over the wire; there is deliberately no separate
            "mode" vocabulary to keep in step with it.
        label: User-facing name for a picker row (``"Problem Solver"``).

            **Not the same thing as** :attr:`kodo.agents.SubAgent.display_name`,
            and the two are allowed to differ — ``guide`` is the standing case:
            the feed calls it "Kōdo" while the picker has always called the
            *choice* "Guide". ``display_name`` names the agent as it works;
            ``label`` names the option you pick. Omit it and it falls back to
            ``display_name``.
        description: One sentence describing what choosing this agent does,
            shown under :attr:`label`. Written for someone deciding, not for a
            model — no agent ever reads it.
        rank: Sort key for the picker, ascending. Ties break by :attr:`name`.
        selectable: Whether this agent is offered to the user at all. ``False``
            marks one that is reachable only by selecting it explicitly over the
            wire — ``judge``, which ``kodo.validator`` drives and which has no
            meaning in an interactive session. A non-selectable agent is still
            fully registered and still runs; it is simply absent from the
            catalog the client renders.
        default: Whether an unrecognized selection falls back to this agent.
            Exactly one of a registry's top-level agents declares it.
        notes: Engineer-facing rationale for why this entry looks the way it
            does — the prose a JSON file has nowhere else to put. **Never shown
            to a model or a user**: it reaches no schema, no prompt and no
            picker, and nothing in the engine reads it. Mirrors
            :attr:`kodo.agents.SubAgentSpec.notes`.
    """

    name: str
    label: str
    description: str
    rank: int
    selectable: bool = True
    default: bool = False
    notes: str = ""


#: Extension of a top-level agent's config file. The glob that finds them.
TOP_AGENT_SUFFIX = ".json"

_KEYS = frozenset({"name", "notes", "label", "description", "rank", "selectable", "default"})


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

    return TopAgent(
        name=name,
        label=label,
        description=description,
        rank=rank,
        selectable=selectable,
        default=default,
        notes=notes,
    )


def load_top_agents(directory: Path) -> tuple[TopAgent, ...]:
    """Build every top-level agent config in *directory*, in filename order.

    Every ``*.json`` file directly in the directory is a config — there is no
    manifest to keep in step, so adding one is adding a file beside the prompt
    it describes. ``glob`` does not descend, so the sub-agent specs one level
    down are never mistaken for one.

    Two files cannot collide on a name, because a name *is* its filename stem.
    Collisions between the built-in names and a user-installed agent's are
    cross-root knowledge and are checked by the registry, which holds both
    roots: every built-in name carries the reserved ``kodo_`` prefix, which a
    user file may not use.

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
