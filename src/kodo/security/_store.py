"""The global (user-wide) security rule store (doc/SECURITY_RULES_PLAN.md Phase 2).

A user rule is exactly the generalized ``(executable, subcommand)`` shape
:class:`~._rules.RuleDecision` carries as ``shape`` — never arguments, paths,
or the literal command line (§2.1). The permission prompt offers to remember
one at either of two scopes:

- **Session** — held entirely as ordinary runtime state
  (``kodo.runtime.SessionState.security_rules``, persisted for crash-resume
  via ``kodo.state.TransientStore``). This module has nothing to do with that
  scope and never imports ``kodo.runtime``/``kodo.state`` —
  ``kodo.security`` stays free of any session/runtime dependency
  ([[feedback-tools-layer]]'s tier discipline: T2 may not reach up into T3+).
- **Global** — *this* module. A flat JSON list of ``[executable,
  subcommand]`` pairs at ``~/.kodo/etc/security_rules.json``, beside the
  server's existing ``settings.json`` (:class:`kodo.project.WorkspaceLayout`
  — "one instance per machine, shared by every VS Code window's session").
  A global rule is therefore a genuinely user-wide, cross-project
  relaxation of the *user's own* future asks, not a per-project trust
  boundary — the deliberate reading of the plan's "beside the server's
  existing settings storage" (as opposed to the unrelated, unused
  ``ProjectLayout.security_json`` stub, which would have been per-project).

:func:`global_rules` re-reads the (tiny) file on every call rather than
caching in memory — this is checked at most once per HIGH-impact
``run_command`` judgement, so re-parsing a short JSON list is not a
measurable cost, and it means every concurrently open session in this
process sees a newly granted global rule immediately with no cache to
invalidate.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kodo.project import WorkspaceLayout

__all__ = [
    "add_global_path_rule",
    "add_global_rule",
    "global_path_rules",
    "global_path_rules_path",
    "global_rules",
    "global_rules_path",
    "remove_global_path_rule",
    "remove_global_rule",
]

_log = logging.getLogger(__name__)


def global_rules_path() -> Path:
    """``~/.kodo/etc/security_rules.json`` — beside ``settings.json``."""
    return WorkspaceLayout().etc_dir / "security_rules.json"


def global_rules() -> frozenset[tuple[str, str]]:
    """The current global rule set, read fresh from disk."""
    return _load(global_rules_path())


def add_global_rule(executable: str, subcommand: str) -> frozenset[tuple[str, str]]:
    """Grant ``(executable, subcommand)`` globally and persist it.

    Best-effort: a write failure (e.g. a read-only home directory) is only
    logged — the caller gets back the rule set it would have had, even
    though this particular grant won't survive a restart.

    Returns:
        frozenset[tuple[str, str]]: The updated global rule set.
    """
    return _add(global_rules_path(), global_rules(), executable, subcommand)


def global_path_rules_path() -> Path:
    """``~/.kodo/etc/security_path_rules.json`` — the workspace-escape
    sibling of :func:`global_rules_path` (doc/SECURITY_RULES_PLAN.md §2.7).

    Deliberately a separate file rather than a new key/shape in
    ``security_rules.json``: a command-shape rule is a literal ``(executable,
    subcommand)`` match, while a path-shape rule's second element is a
    resolved absolute path matched by *resolving the incoming argument
    first* — different semantics, kept in genuinely separate stores rather
    than requiring a "kind" discriminator on every read of the existing,
    already-shipped file.
    """
    return WorkspaceLayout().etc_dir / "security_path_rules.json"


def global_path_rules() -> frozenset[tuple[str, str]]:
    """The current global path-rule set — ``(executable, resolved_absolute_
    path)`` pairs — read fresh from disk."""
    return _load(global_path_rules_path())


def add_global_path_rule(executable: str, path: str) -> frozenset[tuple[str, str]]:
    """Grant ``(executable, resolved_absolute_path)`` globally and persist it.

    Same best-effort semantics as :func:`add_global_rule`.

    Returns:
        frozenset[tuple[str, str]]: The updated global path-rule set.
    """
    return _add(global_path_rules_path(), global_path_rules(), executable, path)


def remove_global_rule(executable: str, subcommand: str) -> frozenset[tuple[str, str]]:
    """Revoke a previously granted global ``(executable, subcommand)`` rule.

    A no-op (not an error) if the rule isn't present — the management UI's
    "delete selected" always reflects the store's state back afterward, so a
    stale selection racing a second deletion just settles on the same result.

    Returns:
        frozenset[tuple[str, str]]: The updated global rule set.
    """
    return _remove(global_rules_path(), global_rules(), executable, subcommand)


def remove_global_path_rule(executable: str, path: str) -> frozenset[tuple[str, str]]:
    """Revoke a previously granted global ``(executable, resolved_path)`` rule.

    Same best-effort, no-op-if-absent semantics as :func:`remove_global_rule`.

    Returns:
        frozenset[tuple[str, str]]: The updated global path-rule set.
    """
    return _remove(global_path_rules_path(), global_path_rules(), executable, path)


def _add(
    path: Path, current: frozenset[tuple[str, str]], executable: str, value: str
) -> frozenset[tuple[str, str]]:
    return _write(path, current | {(executable, value)})


def _remove(
    path: Path, current: frozenset[tuple[str, str]], executable: str, value: str
) -> frozenset[tuple[str, str]]:
    return _write(path, current - {(executable, value)})


def _write(path: Path, updated: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted([list(rule) for rule in updated]), indent=2),
            encoding="utf-8",
        )
    except OSError:
        _log.warning("Failed to persist global security rules to %s", path)
    return updated


def _load(path: Path) -> frozenset[tuple[str, str]]:
    if not path.exists():
        return frozenset()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _log.warning("Could not parse %s — starting with no global security rules", path)
        return frozenset()
    if not isinstance(raw, list):
        return frozenset()
    out: set[tuple[str, str]] = set()
    for entry in raw:
        if isinstance(entry, list) and len(entry) == 2:
            out.add((str(entry[0]), str(entry[1])))
    return frozenset(out)
