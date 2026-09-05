"""Resolve a consumer's declared artifact needs to concrete paths.

The engine hands an agent an ``input_paths`` built from what the session has
actually produced, rather than from what a model guessed. Given the ledger's
work products and one agent's ``consumes`` declaration, this answers "which
files fill this role, for this spawn?" — and says plainly when nothing does.

Kept here, next to the ledger it reads, rather than in the engine: it is a pure
function of (work products, needs, scope inputs) with no engine state in it, so
it can be tested directly and the engine mixin stays a thin caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._records import WorkProduct

__all__ = ["ResolvedNeed", "resolve_needs"]

# Scope names, duplicated from kodo.subagents._artifacts rather than imported:
# kodo.workproducts is a leaf package that imports nothing from kodo, the same
# rule kodo.findings and kodo.guided_state follow. A test pins the two sets
# equal so they cannot drift.
_SCOPE_GLOBAL = "global"
_SCOPE_SELF = "self"
_SCOPE_ALL = "all"
_SCOPE_UNDER_REVIEW = "under_review"
_SCOPE_DEPENDENCIES = "dependencies"


@dataclass(frozen=True)
class ResolvedNeed:
    """What one declared need resolved to.

    Attributes:
        role: The artifact role that was asked for.
        scope: The scope it was asked for at.
        required: Whether the consumer declared it as essential.
        paths: The files filling it, in ledger order; empty when nothing does.
    """

    role: str
    scope: str
    required: bool
    paths: tuple[str, ...]

    @property
    def unmet(self) -> bool:
        """A required need that resolved to nothing — the case worth shouting about."""
        return self.required and not self.paths


def _related_components(graph: dict[str, list[str]], code: str) -> set[str]:
    """Components *code* consumes or is consumed by.

    Both directions, deliberately: the coder's contract asks for "the Functional
    Designs of consumed/consuming components" because an interface has two
    sides, and changing one without seeing the other is how cross-file drift
    gets written in the first place. *code* itself is excluded — that is what
    ``self`` scope is for, and a consumer normally declares both.
    """
    if not code:
        return set()
    upstream = {dep for dep in graph.get(code, [])}
    downstream = {other for other, deps in graph.items() if code in deps}
    return (upstream | downstream) - {code}


def _role_paths(work_product: WorkProduct, role: str) -> tuple[str, ...]:
    """The members of *work_product* filling *role*.

    Falls back to the whole member set when the work product records no role
    map at all. That is the pre-2026-09-05 shape, and reading it as "all of it"
    keeps a session that spans the change resolvable instead of silently empty.
    """
    if not work_product.roles:
        return work_product.paths
    return work_product.roles.get(role, ())


def resolve_needs(
    work_products: list[WorkProduct],
    needs: list[tuple[str, str, bool]],
    *,
    project: str,
    responsibility_code: str = "",
    under_review: WorkProduct | None = None,
    components: dict[str, list[str]] | None = None,
) -> list[ResolvedNeed]:
    """Resolve each ``(role, scope, required)`` need against the ledger.

    Args:
        work_products: Every work product recorded this session.
        needs: The consumer's declared needs, in declaration order — which is
            preserved in the result, because it is the order the agent's own
            contract lists them in and therefore the order it expects to read.
        project: Only work products from this bound root are considered. A
            session may hold several projects, and one project's architecture
            is not another's.
        responsibility_code: This spawn's component, for ``self`` scope.
        under_review: The work product this critic round is reviewing, for
            ``under_review`` scope. ``None`` outside a review round.
        components: The architect's component graph (``{code: depends_on}``),
            for ``dependencies`` scope. Empty when the architect has not
            declared one — that scope then resolves to nothing rather than
            guessing at a relationship.

    Returns:
        list[ResolvedNeed]: One entry per need, in declaration order.

    ``global`` deliberately takes the **last** matching work product rather
    than erroring on several: the ledger is last-write-wins everywhere else,
    and a role legitimately gets re-produced when an author is re-invoked.
    """
    in_project = [wp for wp in work_products if wp.project == project]
    resolved: list[ResolvedNeed] = []

    for role, scope, required in needs:
        paths: tuple[str, ...] = ()

        if scope == _SCOPE_UNDER_REVIEW:
            # Supplied by the round, never looked up — the engine already knows
            # what it spawned this critic against.
            paths = under_review.paths if under_review is not None else ()
        elif scope == _SCOPE_ALL:
            seen: dict[str, None] = {}
            for work_product in in_project:
                for path in _role_paths(work_product, role):
                    seen.setdefault(path, None)
            paths = tuple(seen)
        elif scope == _SCOPE_SELF:
            paths = _by_component(in_project, role, {responsibility_code})
        elif scope == _SCOPE_DEPENDENCIES:
            paths = _by_component(
                in_project, role, _related_components(components or {}, responsibility_code)
            )
        elif scope == _SCOPE_GLOBAL:
            matches = [wp for wp in in_project if _role_paths(wp, role)]
            paths = _role_paths(matches[-1], role) if matches else ()
        # An unrecognised scope resolves to nothing. The registry's load-time
        # check rejects unknown names, so this is unreachable from a real spec.

        resolved.append(ResolvedNeed(role=role, scope=scope, required=required, paths=paths))

    return resolved


def _by_component(work_products: list[WorkProduct], role: str, wanted: set[str]) -> tuple[str, ...]:
    """Every path filling *role* that belongs to one of the *wanted* components.

    Attribution is per **file**, not per work product: ``functional_designer``
    writes every component's design in one whole-product run, so narrowing by
    the work product's own ``responsibility_code`` would match all of them or
    none. ``WorkProduct.component_of`` falls back to that code for the ordinary
    per-component stage, so both shapes read the same way here.
    """
    if not wanted or not any(wanted):
        return ()
    found: dict[str, None] = {}
    for work_product in work_products:
        for path in _role_paths(work_product, role):
            if work_product.component_of(path) in wanted:
                found.setdefault(path, None)
    return tuple(found)
