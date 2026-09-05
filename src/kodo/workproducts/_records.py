"""The work product — one reviewable set of files — and its log entry type.

A **work product** is what one ``run_subagent_<author>`` review loop produces:
the whole set of files that round wrote, reviewed and accepted together. It
replaced ``primary_path``, which modelled a sub-agent's output as a single
document because the first authors (Narrative, Architecture, Requirements)
each wrote exactly one. A coder does not: a feature spanning five files has to
land in one go or the build breaks, so the *reviewable unit* must be the thing
that builds, not one file out of five.

Identity is derived, not minted, and is deliberately stable across separate
``run_subagent`` calls rather than only across rounds: the Guide routinely
invokes the same author several times on the same subject ("continue resolving
outstanding critic findings"), and its backlog has to survive that. The
project segment keeps two bound projects apart, the same reason
:mod:`kodo.findings` keys on folder-prefixed logical paths.

Membership is per-revision: round two may add a file round one did not have,
or drop one. The id does not change when it does — see
:func:`~kodo.workproducts.record_membership` for what happens to the findings
of a file that leaves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = [
    "ENTRY_COMPONENTS",
    "ENTRY_MEMBERSHIP",
    "WorkProduct",
    "components_entry",
    "membership_entry",
    "work_product_id",
]

ENTRY_MEMBERSHIP = "membership"
#: The architect's component graph — codenames and their declared dependencies.
#: Lives in this log rather than a store of its own because it answers the same
#: kind of question ("what does this project contain, and how does it fit
#: together?") and has the same project-scoped lifetime.
ENTRY_COMPONENTS = "components"

# Same character class kodo.findings._paths sanitises with: a work product id
# becomes a filesystem path there (one findings log per work product), and its
# segments come from workspace-folder display names and agent names.
_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]")


def _sanitize(segment: str) -> str:
    return _UNSAFE.sub("_", segment).strip() or "_"


@dataclass(frozen=True)
class WorkProduct:
    """One review loop's reviewable set of files.

    Attributes:
        id: Derived identity, ``<project>/<agent>[/<responsibility_code>]``.
            Slash-joined so it doubles as the logical key
            :func:`kodo.findings.findings_log_path` turns into a log path.
        project: The bound root's folder name — the first segment of every
            member path.
        agent: The authoring sub-agent's name.
        responsibility_code: The component codename for per-component stages,
            or ``""`` for whole-project ones.
        paths: The member files, folder-prefixed, in the order the author
            reported them. The whole set is what gets reviewed and accepted
            together, regardless of which role each file fills.
        components: ``{member path: component codename}`` for a work product
            whose files belong to different components. Only ``functional_designer``
            needs it — it writes every component's design in one whole-product
            run, so its ``responsibility_code`` is empty and per-file attribution
            is the only way ``self``/``dependencies`` scopes can narrow it. A
            per-component agent leaves this empty; its work product's own
            ``responsibility_code`` already says which component it is.
        roles: ``{artifact role: the member files filling it}``. Derived by the
            engine from the producing agent's declared
            :attr:`~kodo.subagents.SubAgentSpec.produces` map — never from
            anything the model said — so a consumer asking for "the
            architecture" can be handed exactly that. Most work products have a
            single entry covering every path; ``narrative_author`` and
            ``functional_designer`` each fill two roles from one run, which is
            the whole reason this is a map rather than one name.
    """

    id: str
    project: str
    agent: str
    responsibility_code: str
    paths: tuple[str, ...]
    roles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    components: dict[str, str] = field(default_factory=dict)

    def component_of(self, path: str) -> str:
        """Which component *path* belongs to, or ``""`` when it belongs to none.

        Per-file attribution wins where it exists; otherwise the work product's
        own ``responsibility_code`` covers every member, which is the ordinary
        case for a per-component stage.
        """
        return self.components.get(path) or self.responsibility_code


def work_product_id(project: str, agent: str, responsibility_code: str = "") -> str:
    """Derive a work product's identity from what produced it.

    Every segment is sanitised, because this string is turned into a
    filesystem path by the findings store. Returns ``""`` when *agent* is
    empty — a caller with no author name has nothing to key on and should not
    invent one.
    """
    if not agent.strip():
        return ""
    parts = [_sanitize(project) if project.strip() else "_", _sanitize(agent)]
    if responsibility_code.strip():
        parts.append(_sanitize(responsibility_code))
    return "/".join(parts)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def membership_entry(
    *, work_product: WorkProduct, removed: tuple[str, ...] = ()
) -> dict[str, object]:
    """One ``membership`` log line: this revision's member set.

    ``removed`` is carried for the record even though the current membership
    is fully described by ``paths`` — it is what a later reader needs to
    explain why a file's findings were auto-closed.
    """
    return {
        "type": ENTRY_MEMBERSHIP,
        "timestamp": _now(),
        "id": work_product.id,
        "project": work_product.project,
        "agent": work_product.agent,
        "responsibility_code": work_product.responsibility_code,
        "paths": list(work_product.paths),
        "roles": {role: list(paths) for role, paths in work_product.roles.items()},
        "components": dict(work_product.components),
        "removed": list(removed),
    }


def components_entry(*, project: str, components: dict[str, list[str]]) -> dict[str, object]:
    """One ``components`` log line: the architect's component graph.

    ``components`` maps a codename to the codenames it **depends on** — the
    architect's own "upstream/downstream dependencies", declared structurally
    instead of being left as prose in the architecture document for every later
    reader to re-derive.
    """
    return {
        "type": ENTRY_COMPONENTS,
        "timestamp": _now(),
        "project": project,
        "components": {code: list(deps) for code, deps in components.items()},
    }
