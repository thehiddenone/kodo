"""Tests for the global workspace-escape path rule store
(``kodo.security._store``'s ``global_path_rules``/``add_global_path_rule``).

The path-rule sibling of ``test_security_store.py`` — same machine-wide
``~/.kodo``-rooted singleton, same ``HOME``-redirect convention, kept in a
separate on-disk file (``security_path_rules.json``) from the command-shape
store (doc/SECURITY_RULES_PLAN.md §2.7).
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
)


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    return tmp_path


def test_empty_store_has_no_rules() -> None:
    assert global_path_rules() == frozenset()


def test_add_global_path_rule_persists_and_is_visible() -> None:
    add_global_path_rule("cat", "/etc/hosts")
    assert ("cat", "/etc/hosts") in global_path_rules()


def test_add_global_path_rule_written_beside_settings_json() -> None:
    add_global_path_rule("cd", "/outside/path")
    path = global_path_rules_path()
    assert path.parent.name == "etc"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == [["cd", "/outside/path"]]


def test_add_global_path_rule_accumulates() -> None:
    add_global_path_rule("cat", "/etc/hosts")
    add_global_path_rule("cd", "/outside/path")
    rules = global_path_rules()
    assert ("cat", "/etc/hosts") in rules
    assert ("cd", "/outside/path") in rules
    assert len(rules) == 2


def test_add_global_path_rule_is_idempotent() -> None:
    add_global_path_rule("cat", "/etc/hosts")
    add_global_path_rule("cat", "/etc/hosts")
    assert global_path_rules() == frozenset({("cat", "/etc/hosts")})


def test_malformed_store_file_degrades_to_empty_not_raise() -> None:
    path = global_path_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json{{{", encoding="utf-8")
    assert global_path_rules() == frozenset()


def test_path_rule_store_is_independent_of_the_command_rule_store() -> None:
    # Two genuinely separate files/sets — granting one never leaks into the
    # other, even with an (executable, value) pair that could coincidentally
    # collide in shape.
    add_global_rule("cat", "push")
    add_global_path_rule("cat", "/etc/hosts")
    assert global_rules() == frozenset({("cat", "push")})
    assert global_path_rules() == frozenset({("cat", "/etc/hosts")})
    assert global_rules_path() != global_path_rules_path()
