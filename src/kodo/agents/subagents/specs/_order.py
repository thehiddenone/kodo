"""Derive the catalog's order from the artifact graph the specs already declare.

``ALL_SUBAGENTS`` used to be a hand-written tuple whose order encoded the
pipeline — a fact kept nowhere but in that tuple, agreeing with the specs only
because somebody remembered to keep it agreeing. That is the thing this module
removes: order is now *computed* from ``produces``/``consumes``, so a spec that
declares its inputs correctly lands in the right place by construction and one
that does not cannot be papered over by editing a list.

How the order is derived
========================

An edge runs from the producer of a role to every agent that consumes it, which
makes the catalog a DAG and its topological order the reading order: nothing
appears before the agent whose output it needs. Three rules make that
well-defined:

- **Self-edges are skipped.** An agent that consumes a role it also produces is
  revising its *own* prior work product (``narrative_author`` declares both, so
  a later round is handed what it wrote before). That is a dependency on the
  past, not on another agent.
- **``under_review`` is an edge like any other.** A critic consuming
  ``<role>/under_review`` depends on whoever produces that role, which is
  exactly its author — so every critic sorts directly after the agent it
  reviews without anybody declaring the pairing twice. (The registry exempts
  this scope from its "somebody must produce it" rule, because the engine
  supplies the document from the round itself; the ordering graph still reads
  the edge.)
- **Ties break by depth, then by name.** ``depth`` is the longest path from a
  source — an agent sorts below the deepest thing it consumes — and agents at
  equal depth sort alphabetically. Both halves are total and deterministic, so
  the catalog order is a pure function of the declarations and never depends on
  filesystem order.

Agents that declare **neither** ``produces`` nor ``consumes`` are not in the
pipeline at all (``planner``, ``investigator``, ``developer``, ``compactor``,
``web_search``, the toolchain agents): they take no artifact input and file no
work product, so the graph has nothing to say about them. They sort last,
alphabetically, rather than at depth 0 alongside the pipeline's first stage.

What the graph does *not* determine
===================================

A topological order is not unique, and this one is weaker than the stage
sequence the Guide follows: the end-to-end stages consume the architecture and
the functional designs but **not** ``code`` or ``test_code``, so nothing here
forces them after ``coder``. They therefore sort alongside the per-component
stages rather than after them. That is faithful — it is what the specs
actually declare — and it is harmless, because the pipeline sequence a session
runs lives in ``agent_guide.md``'s prompt, not in this order. The catalog order
is a reading and diagnostic order. If the e2e stages should also be ordered by
the graph, the fix is for them to declare the inputs they really read, not for
this module to hardcode a stage number.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["SpecOrderError", "pipeline_order"]


class SpecOrderError(Exception):
    """Raised when the declared artifact graph cannot be ordered (a cycle)."""


def pipeline_order(specs: tuple[SubAgentSpec, ...]) -> tuple[SubAgentSpec, ...]:
    """Return *specs* in declared-dependency order.

    Args:
        specs: The loaded catalog, in any order.

    Returns:
        tuple[SubAgentSpec, ...]: The same specs, ordered so that every agent
        follows the producers of the roles it consumes; agents declaring no
        artifact relationship at all come last, alphabetically.

    Raises:
        SpecOrderError: The declarations form a cycle, naming the agents in it.
    """
    by_name = {spec.name: spec for spec in specs}
    producers: dict[str, list[str]] = {}
    for spec in specs:
        for role in spec.produces:
            producers.setdefault(role, []).append(spec.name)

    # name -> the agents it depends on. A consumed role nobody produces simply
    # adds no edge; the registry is what reports it, with the cross-agent
    # context needed to say so usefully.
    deps: dict[str, set[str]] = {spec.name: set() for spec in specs}
    for spec in specs:
        for need in spec.consumes:
            if need.role in spec.produces:
                continue  # revising its own prior work product, not an edge
            deps[spec.name].update(producers.get(need.role, ()))

    unattached = sorted(spec.name for spec in specs if not spec.produces and not spec.consumes)
    detached = set(unattached)
    attached = [spec.name for spec in specs if spec.name not in detached]

    depth = _depths(attached, deps)
    ordered = sorted(attached, key=lambda name: (depth[name], name))
    return tuple(by_name[name] for name in ordered + unattached)


def _depths(names: list[str], deps: dict[str, set[str]]) -> dict[str, int]:
    """Longest-path depth of each name, by iterative post-order walk.

    Iterative rather than recursive so a deep catalog cannot exhaust the stack,
    and colour-marked so a cycle is reported as a cycle instead of recursing
    forever.
    """
    depth: dict[str, int] = {}
    on_stack: set[str] = set()
    for root in names:
        if root in depth:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            name, expanded = stack.pop()
            if expanded:
                on_stack.discard(name)
                depth[name] = 1 + max((depth[d] for d in deps[name] if d in depth), default=-1)
                continue
            if name in depth:
                continue
            if name in on_stack:
                raise SpecOrderError(
                    f"sub-agent specs declare a produces/consumes cycle involving "
                    f"{name!r}; the catalog cannot be ordered"
                )
            on_stack.add(name)
            stack.append((name, True))
            for dependency in sorted(deps[name]):
                if dependency not in depth:
                    stack.append((dependency, False))
    return depth
