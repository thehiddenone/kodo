"""Global security-rule management — the read/revoke half of Phase 2's
"always allow" grants (doc/SECURITY_RULES_PLAN.md §Phase 3 item 2).

Granting a global rule happens inline in a live engine run
(``_engine/_core.py``, when a `prompt.permission` response asks to remember
at global scope); this module is the separate, run-independent facade the
`server`'s control connection uses to list and revoke what has already been
granted — `security` is consumed only by `runtime` (doc/INTERNALS.md §2.2),
so `server` reaches these stores through here rather than importing
`kodo.security` itself.
"""

from __future__ import annotations

from kodo.security import (
    global_path_rules,
    global_rules,
    remove_global_path_rule,
    remove_global_rule,
)

__all__ = ["delete_global_security_rules", "list_global_security_rules"]


def list_global_security_rules() -> list[dict[str, str]]:
    """Every globally-granted rule, command-shape and path-shape combined.

    Returns:
        list[dict[str, str]]: ``[{"kind": "command"|"path", "executable",
        "value"}, ...]``, sorted for a stable display order.
    """
    rules = [{"kind": "command", "executable": e, "value": v} for e, v in sorted(global_rules())]
    rules += [{"kind": "path", "executable": e, "value": v} for e, v in sorted(global_path_rules())]
    return rules


def delete_global_security_rules(rules: list[dict[str, str]]) -> list[dict[str, str]]:
    """Revoke each ``{kind, executable, value}`` entry; unknown ones are no-ops.

    Returns:
        list[dict[str, str]]: The resulting rule set, same shape as
        :func:`list_global_security_rules` — lets the caller refresh a
        management UI from the response alone.
    """
    for entry in rules:
        executable = entry.get("executable", "")
        value = entry.get("value", "")
        if not executable or not value:
            continue
        if entry.get("kind") == "path":
            remove_global_path_rule(executable, value)
        else:
            remove_global_rule(executable, value)
    return list_global_security_rules()
