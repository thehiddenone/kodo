"""Tests for the global (user-wide) security rule store (``kodo.security._store``).

The server is a machine-wide singleton rooted at ``~/.kodo`` (see
test_config.py) — tests redirect ``HOME`` to a temp dir so they never touch
the real user's store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.security import (
    add_global_path_rule,
    add_global_rule,
    global_path_rules,
    global_path_rules_path,
    global_rules,
    global_rules_path,
    remove_global_path_rule,
    remove_global_rule,
)


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    return tmp_path


def test_empty_store_has_no_rules() -> None:
    assert global_rules() == frozenset()


def test_add_global_rule_persists_and_is_visible() -> None:
    add_global_rule("git", "push")
    assert ("git", "push") in global_rules()


def test_add_global_rule_written_beside_settings_json() -> None:
    add_global_rule("npm", "publish")
    path = global_rules_path()
    assert path.parent.name == "etc"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == [["npm", "publish"]]


def test_add_global_rule_accumulates() -> None:
    add_global_rule("git", "push")
    add_global_rule("npm", "publish")
    rules = global_rules()
    assert ("git", "push") in rules
    assert ("npm", "publish") in rules
    assert len(rules) == 2


def test_add_global_rule_is_idempotent() -> None:
    add_global_rule("git", "push")
    add_global_rule("git", "push")
    assert global_rules() == frozenset({("git", "push")})


def test_malformed_store_file_degrades_to_empty_not_raise() -> None:
    path = global_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert global_rules() == frozenset()


def test_non_list_store_file_degrades_to_empty() -> None:
    path = global_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"git": "push"}), encoding="utf-8")
    assert global_rules() == frozenset()


def test_malformed_entries_are_skipped() -> None:
    path = global_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([["git", "push"], ["only-one"], "string", ["a", "b", "c"]]),
        encoding="utf-8",
    )
    assert global_rules() == frozenset({("git", "push")})


def test_remove_global_rule_revokes_and_persists() -> None:
    add_global_rule("git", "push")
    add_global_rule("npm", "publish")
    assert remove_global_rule("git", "push") == frozenset({("npm", "publish")})
    assert global_rules() == frozenset({("npm", "publish")})


def test_remove_absent_global_rule_is_noop() -> None:
    add_global_rule("git", "push")
    assert remove_global_rule("cargo", "publish") == frozenset({("git", "push")})
    assert global_rules() == frozenset({("git", "push")})


def test_path_rules_live_in_a_separate_store() -> None:
    assert global_path_rules() == frozenset()
    updated = add_global_path_rule("cat", "/etc/hosts")
    assert updated == frozenset({("cat", "/etc/hosts")})
    assert global_path_rules() == frozenset({("cat", "/etc/hosts")})
    assert global_rules() == frozenset()
    assert global_path_rules_path() != global_rules_path()
    assert global_path_rules_path().parent == global_rules_path().parent


def test_remove_global_path_rule_revokes() -> None:
    add_global_path_rule("cat", "/etc/hosts")
    add_global_path_rule("ls", "/var/log")
    assert remove_global_path_rule("cat", "/etc/hosts") == frozenset({("ls", "/var/log")})
    assert global_path_rules() == frozenset({("ls", "/var/log")})


def test_write_failure_is_logged_and_returns_updated_set() -> None:
    # Make the would-be ``etc`` directory a regular file so mkdir fails.
    etc = global_rules_path().parent
    etc.parent.mkdir(parents=True, exist_ok=True)
    etc.write_text("blocker", encoding="utf-8")
    assert add_global_rule("git", "push") == frozenset({("git", "push")})
    # Not persisted.
    assert global_rules() == frozenset()
