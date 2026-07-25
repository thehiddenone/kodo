"""Tests for ``kodo.runtime._security_rules``.

Covers the server-side facade over :mod:`kodo.security`'s global rule store --
the read and revoke half used by the extension's security-management UI
(doc/SECURITY_RULES_PLAN.md, Phase 3 item 2).

The underlying :mod:`kodo.security` helpers are tested in ``test_security_store.py``;
this file tests only the wrapper layer.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kodo.runtime._security_rules import delete_global_security_rules, list_global_security_rules


@pytest.fixture
def _mock_security(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the four security helpers so we exercise the wrapper only."""
    mock_global = MagicMock(return_value={("git", "push"), ("npm", "publish")})
    mock_path = MagicMock(return_value={("python", "/usr/local/bin"), ("node", "/usr/bin")})
    mock_remove_rule = MagicMock()
    mock_remove_path = MagicMock()
    monkeypatch.setattr("kodo.runtime._security_rules.global_rules", mock_global)
    monkeypatch.setattr("kodo.runtime._security_rules.global_path_rules", mock_path)
    monkeypatch.setattr("kodo.runtime._security_rules.remove_global_rule", mock_remove_rule)
    monkeypatch.setattr("kodo.runtime._security_rules.remove_global_path_rule", mock_remove_path)
    return {
        "global_rules": mock_global,
        "global_path_rules": mock_path,
        "remove_global_rule": mock_remove_rule,
        "remove_global_path_rule": mock_remove_path,
    }


def test_list_global_security_rules_combines_command_and_path(
    _mock_security: dict[str, Any],
) -> None:
    rules = list_global_security_rules()
    kinds = {r["kind"] for r in rules}
    assert kinds == {"command", "path"}
    command_rules = [r for r in rules if r["kind"] == "command"]
    path_rules = [r for r in rules if r["kind"] == "path"]
    assert command_rules == sorted(command_rules, key=lambda r: r["executable"])
    assert path_rules == sorted(path_rules, key=lambda r: r["executable"])


def test_list_global_security_rules_empty_when_no_rules(
    _mock_security: dict[str, Any],
) -> None:
    _mock_security["global_rules"].return_value = frozenset()
    _mock_security["global_path_rules"].return_value = frozenset()
    rules = list_global_security_rules()
    assert rules == []


def test_list_global_security_rules_sorted_alphabetically(
    _mock_security: dict[str, Any],
) -> None:
    _mock_security["global_rules"].return_value = frozenset(
        {
            ("zsh", "-c"),
            ("bash", "-i"),
            ("python", "-m"),
        }
    )
    _mock_security["global_path_rules"].return_value = frozenset()
    rules = list_global_security_rules()
    execs = [r["executable"] for r in rules]
    assert execs == sorted(execs)


def test_list_global_security_rules_path_rules_sorted_separately(
    _mock_security: dict[str, Any],
) -> None:
    _mock_security["global_rules"].return_value = frozenset()
    _mock_security["global_path_rules"].return_value = frozenset(
        {
            ("node", "/usr/local/bin"),
            ("python", "/usr/bin"),
            ("bash", "/bin/bash"),
        }
    )
    rules = list_global_security_rules()
    path_execs = [r["executable"] for r in rules]
    assert path_execs == sorted(path_execs)


def test_delete_global_security_rules_removes_command_rules(
    _mock_security: dict[str, Any],
) -> None:
    _mock_security["global_rules"].return_value = {("git", "push"), ("npm", "run")}
    _mock_security["global_path_rules"].return_value = {("python", "/usr/bin")}
    rules_to_delete = [{"kind": "command", "executable": "git", "value": "push"}]
    result = delete_global_security_rules(rules_to_delete)
    _mock_security["remove_global_rule"].assert_called_once_with("git", "push")
    assert isinstance(result, list)
    # remove_global_path_rule should not have been called for command rules
    _mock_security["remove_global_path_rule"].assert_not_called()


def test_delete_global_security_rules_skips_empty_entries(
    _mock_security: dict[str, Any],
) -> None:
    """Entries missing executable or value should be no-ops."""
    _mock_security["global_rules"].return_value = {("git", "push")}
    _mock_security["global_path_rules"].return_value = frozenset()
    rules_to_delete = [
        {"kind": "command", "executable": "", "value": "push"},
        {"kind": "command", "executable": "git", "value": ""},
        {"kind": "command", "executable": "git", "value": "push"},
    ]
    delete_global_security_rules(rules_to_delete)
    assert _mock_security["remove_global_rule"].call_count == 1
    _mock_security["remove_global_rule"].assert_called_once_with("git", "push")


def test_delete_global_security_rules_handles_mixed_kinds(
    _mock_security: dict[str, Any],
) -> None:
    _mock_security["global_rules"].return_value = {("git", "push")}
    _mock_security["global_path_rules"].return_value = {("python", "/usr/bin")}
    rules_to_delete = [
        {"kind": "command", "executable": "git", "value": "push"},
        {"kind": "path", "executable": "python", "value": "/usr/bin"},
    ]
    delete_global_security_rules(rules_to_delete)
    _mock_security["remove_global_rule"].assert_called_once_with("git", "push")
    _mock_security["remove_global_path_rule"].assert_called_once_with("python", "/usr/bin")


def test_delete_global_security_rules_unknown_rule_does_not_raise(
    _mock_security: dict[str, Any],
) -> None:
    """Deleting a non-existent rule is a no-op at the security store level
    (``remove_global_rule`` is itself idempotent) -- the wrapper just has to
    call it, never crash."""
    _mock_security["global_rules"].return_value = frozenset()
    _mock_security["global_path_rules"].return_value = frozenset()
    rules_to_delete = [{"kind": "command", "executable": "unknown", "value": "cmd"}]
    delete_global_security_rules(rules_to_delete)
    # The wrapper still calls remove_global_rule (the underlying function is
    # idempotent -- deleting a rule that doesn't exist is a no-op).
    _mock_security["remove_global_rule"].assert_called_once_with("unknown", "cmd")
    # No path-rule call should have been made.
    _mock_security["remove_global_path_rule"].assert_not_called()


def test_delete_global_security_rules_returns_list_global_security_rules_shape(
    _mock_security: dict[str, Any],
) -> None:
    """The return value should have the same shape as list_global_security_rules."""
    _mock_security["global_rules"].return_value = {("git", "push")}
    _mock_security["global_path_rules"].return_value = frozenset()
    result = delete_global_security_rules([])
    assert isinstance(result, list)
    for entry in result:
        assert "kind" in entry
        assert "executable" in entry
        assert "value" in entry


def test_delete_global_security_rules_path_kind_uses_path_remove(
    _mock_security: dict[str, Any],
) -> None:
    """A rule with kind=path must call remove_global_path_rule, not remove_global_rule."""
    _mock_security["global_rules"].return_value = frozenset()
    _mock_security["global_path_rules"].return_value = {("python", "/usr/bin")}
    rules_to_delete = [{"kind": "path", "executable": "python", "value": "/usr/bin"}]
    delete_global_security_rules(rules_to_delete)
    _mock_security["remove_global_path_rule"].assert_called_once_with("python", "/usr/bin")
    _mock_security["remove_global_rule"].assert_not_called()


def test_delete_global_security_rules_default_kind_is_command(
    _mock_security: dict[str, Any],
) -> None:
    """An entry without an explicit kind falls through to remove_global_rule."""
    _mock_security["global_rules"].return_value = {("git", "push")}
    _mock_security["global_path_rules"].return_value = frozenset()
    rules_to_delete = [{"executable": "git", "value": "push"}]
    delete_global_security_rules(rules_to_delete)
    _mock_security["remove_global_rule"].assert_called_once_with("git", "push")
    _mock_security["remove_global_path_rule"].assert_not_called()
