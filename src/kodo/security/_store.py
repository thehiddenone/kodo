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

__all__ = ["add_global_rule", "global_rules", "global_rules_path"]

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
    updated = global_rules() | {(executable, subcommand)}
    path = global_rules_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted([list(rule) for rule in updated]), indent=2),
            encoding="utf-8",
        )
    except OSError:
        _log.warning("Failed to persist global security rule to %s", path)
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
