"""Append/replay a project's work-product membership log.

One append-only ``<root>/.kodo/workproducts.jsonl`` per **project**, replayed
on every read — the same no-index rule :mod:`kodo.findings` and
:mod:`kodo.guided_state` follow. A single log rather than one file per work
product, because the question asked most often is the *reverse* one — "which
work product is this file part of?" (``guided_dev_status`` asks it for every
tracked file in the project) — and answering that from one replay beats
scanning a directory.

**Project-scoped, not session-scoped** (corrected 2026-09-05). Membership was
first stored beside the findings backlog, on the reasoning that both are review
state. They are not the same kind of fact: a backlog is a judgment *this
session's* critics made, while "the architecture is these two files" is a fact
about the project. Session scope meant a new session on an existing tree
resolved every role to nothing even with the documents sitting accepted on
disk — which is precisely the under-supply this store exists to prevent, and
which would have made refusing an unresolvable spawn refuse the whole pipeline.

All functions are synchronous file I/O; callers on a hot async path wrap them
in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ._records import (
    ENTRY_COMPONENTS,
    ENTRY_MEMBERSHIP,
    WorkProduct,
    components_entry,
    membership_entry,
    work_product_id,
)

__all__ = [
    "read_components",
    "read_work_product",
    "read_work_products",
    "record_components",
    "record_membership",
    "work_products_log_path",
    "work_product_for_path",
]

_LOG_NAME = "workproducts.jsonl"


def work_products_log_path(project_root: Path) -> Path:
    """*project_root*'s work-product log, under its ``.kodo/`` directory.

    Sibling of ``guided_dev_state/`` rather than inside it: this package and
    :mod:`kodo.guided_state` are independent leaves, and neither owns the
    other's layout.
    """
    return project_root / ".kodo" / _LOG_NAME


def _read_jsonl(log_path: Path) -> list[dict[str, object]]:
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _replay(history: list[dict[str, object]]) -> dict[str, WorkProduct]:
    """Fold ``membership`` lines into ``{id: latest membership}``, in file order."""
    current: dict[str, WorkProduct] = {}
    for entry in history:
        if entry.get("type") != ENTRY_MEMBERSHIP:
            continue
        wp_id = str(entry.get("id", ""))
        if not wp_id:
            continue
        raw_paths = entry.get("paths")
        paths = (
            tuple(str(p) for p in raw_paths if isinstance(p, str))
            if isinstance(raw_paths, list)
            else ()
        )
        raw_components = entry.get("components")
        components = (
            {str(k): str(v) for k, v in raw_components.items() if isinstance(v, str)}
            if isinstance(raw_components, dict)
            else {}
        )
        raw_roles = entry.get("roles")
        roles = (
            {
                str(role): tuple(str(p) for p in members if isinstance(p, str))
                for role, members in raw_roles.items()
                if isinstance(members, list)
            }
            if isinstance(raw_roles, dict)
            else {}
        )
        current[wp_id] = WorkProduct(
            id=wp_id,
            project=str(entry.get("project", "")),
            agent=str(entry.get("agent", "")),
            responsibility_code=str(entry.get("responsibility_code", "")),
            paths=paths,
            roles=roles,
            components=components,
        )
    return current


def read_work_products(project_root: Path) -> list[WorkProduct]:
    """Every work product recorded for this project, at its latest membership."""
    return list(_replay(_read_jsonl(work_products_log_path(project_root))).values())


def read_work_product(project_root: Path, wp_id: str) -> WorkProduct | None:
    """One work product's latest membership, or ``None`` if never recorded."""
    return _replay(_read_jsonl(work_products_log_path(project_root))).get(wp_id)


def work_product_for_path(project_root: Path, logical_path: str) -> WorkProduct | None:
    """The work product *logical_path* currently belongs to, if any.

    The reverse lookup ``guided_dev_status`` needs: a file's findings live in
    its work product's backlog, not under its own path, so a per-file status
    has to get there through this. A file that has left every work product
    (or was never in one) answers ``None`` and reads as having no backlog.

    Ambiguity is possible in principle — nothing stops two authors writing the
    same file — so the *last* recorded membership wins, matching the replay's
    last-write-wins rule everywhere else.
    """
    if not logical_path:
        return None
    match: WorkProduct | None = None
    for work_product in _replay(_read_jsonl(work_products_log_path(project_root))).values():
        if logical_path in work_product.paths:
            match = work_product
    return match


def record_membership(
    project_root: Path,
    *,
    project: str,
    agent: str,
    responsibility_code: str,
    paths: list[str],
    roles: dict[str, list[str]] | None = None,
    components: dict[str, str] | None = None,
) -> tuple[WorkProduct, tuple[str, ...]]:
    """Record this round's member set for the work product *agent* produces.

    Args:
        project_root: The project's root directory.
        project: The bound root's folder name (the logical prefix on every
            member path), which is what the work product's id is built from.
        agent: The authoring sub-agent.
        responsibility_code: Component codename, or ``""``.
        paths: Every path the author reported writing this round.
        roles: ``{artifact role: its paths}``, derived by the caller from the
            producing agent's declared ``produces`` map. Entries naming a path
            that is not a member are dropped — the member set is the authority
            on what this round wrote, and a role pointing outside it could
            hand a consumer a file nobody reviewed.
        components: ``{member path: component codename}``, for an agent that
            writes several components' artifacts in one run. Entries naming a
            non-member are dropped, same rule as *roles*.

    Returns:
        tuple: The work product at its new membership, and the paths that were
            members before this round but are not now. The caller is expected
            to auto-close those files' findings — a finding against a file that
            is no longer part of the work product can never be verified fixed,
            so leaving it outstanding would block the loop forever.
    """
    wp_id = work_product_id(project, agent, responsibility_code)
    if not wp_id:
        raise ValueError("a work product needs an authoring agent name")

    log_path = work_products_log_path(project_root)
    previous = _replay(_read_jsonl(log_path)).get(wp_id)
    # Order-preserving de-duplication: the author reports paths in a meaningful
    # order (its entry point first) and a repeat is a model slip, not a set.
    members = tuple(dict.fromkeys(p for p in paths if isinstance(p, str) and p))
    removed = tuple(p for p in (previous.paths if previous else ()) if p not in members)

    member_set = set(members)
    resolved_roles = {
        str(role): tuple(p for p in role_paths if p in member_set)
        for role, role_paths in (roles or {}).items()
    }
    work_product = WorkProduct(
        id=wp_id,
        project=project,
        agent=agent,
        responsibility_code=responsibility_code,
        paths=members,
        roles={role: paths for role, paths in resolved_roles.items() if paths},
        components={path: code for path, code in (components or {}).items() if path in member_set},
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(membership_entry(work_product=work_product, removed=removed)) + "\n")
    return work_product, removed


def record_components(
    project_root: Path, *, project: str, components: dict[str, list[str]]
) -> None:
    """Record the architect's component graph for *project*.

    Appended to the same log as membership: it is the other half of "what does
    this project contain", has the same project-scoped lifetime, and is replayed
    by the same reader. Recording an empty graph is a no-op rather than an entry
    that would shadow a real one recorded earlier.
    """
    if not components:
        return
    log_path = work_products_log_path(project_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(components_entry(project=project, components=components)) + "\n")


def read_components(project_root: Path, project: str = "") -> dict[str, list[str]]:
    """The latest component graph recorded for *project*, or ``{}``.

    Last-write-wins, like every other replay here: the architect is re-invoked
    when its document is revised, and the newest decomposition is the one every
    later stage should resolve against.
    """
    graph: dict[str, list[str]] = {}
    for entry in _read_jsonl(work_products_log_path(project_root)):
        if entry.get("type") != ENTRY_COMPONENTS:
            continue
        if project and str(entry.get("project", "")) != project:
            continue
        raw = entry.get("components")
        if isinstance(raw, dict):
            graph = {
                str(code): [str(d) for d in deps if isinstance(d, str)]
                for code, deps in raw.items()
                if isinstance(deps, list)
            }
    return graph
