"""Tests for the global (user-wide) security rule store (``kodo.security._store``).

The server is a machine-wide singleton rooted at ``~/.kodo`` (see
test_config.py) — tests redirect ``HOME`` to a temp dir so they never touch
the real user's store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.security import add_global_rule, global_rules, global_rules_path


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
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
