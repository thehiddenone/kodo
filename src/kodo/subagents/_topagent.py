"""The :class:`TopAgent` record — one top-level agent's selection metadata.

A **top-level agent** is the kind a user selects and talks to directly (``guide``,
``problem_solver``, ``judge``), as opposed to a sub-agent another agent delegates
to. Where a sub-agent's extra declaration is a *typed contract*
(:class:`~kodo.subagents.SubAgentSpec`), a top-level agent has none — it talks to
a human in prose — so what it needs declared instead is how it is **chosen**:
which wire value selects it, what to call it in a picker, and whether it belongs
in one at all.

That is everything here. Behaviour still comes from the ``agent_<name>.md``
prompt and its frontmatter (tools, capability, sub-agent roster); this record
carries only what the engine and the UI need to route to it.

Naming (doc/TOP_AGENT_PLAN.md §1): the concept is spelled ``agent`` wherever the
word is free, and ``top_agent`` wherever it is already taken by something else.
It is taken here — :attr:`kodo.runtime.SessionState.agent` is the agent
*currently holding the floor*, which is frequently a sub-agent mid-pipeline —
so the record, the registry accessors and the session field all say
``top_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TopAgent"]


@dataclass(frozen=True)
class TopAgent:
    """How one top-level agent is selected and presented.

    Attributes:
        name: The agent's name — matches :attr:`kodo.subagents.SubAgent.name`
            and the ``agent_<name>.md`` filename stem. This is also the value
            that selects it over the wire; there is deliberately no separate
            "mode" vocabulary to keep in step with it.
        label: User-facing name for a picker row (``"Problem Solver"``).

            **Not the same thing as** :attr:`kodo.subagents.SubAgent.display_name`,
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
        aliases: Legacy values that resolve to this agent. These exist only so
            sessions persisted under the old workflow-mode vocabulary
            (``"guided"``, ``"problem_solving"``) still resume onto the right
            agent; they are accepted on every read path and never emitted.
        default: Whether an unrecognized selection falls back to this agent.
            Exactly one of a registry's top-level agents declares it.
        notes: Engineer-facing rationale for why this entry looks the way it
            does — the prose a JSON file has nowhere else to put. **Never shown
            to a model or a user**: it reaches no schema, no prompt and no
            picker, and nothing in the engine reads it. Mirrors
            :attr:`kodo.subagents.SubAgentSpec.notes`.
    """

    name: str
    label: str
    description: str
    rank: int
    selectable: bool = True
    aliases: tuple[str, ...] = ()
    default: bool = False
    notes: str = ""
