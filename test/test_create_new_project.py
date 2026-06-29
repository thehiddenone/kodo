"""Unit tests for the ``create_new_project`` directory-naming helpers.

These cover the two behaviours chosen for the tool: slugifying a human project
name into a filesystem-safe directory name, and auto-suffixing (``-2``, ``-3``…)
so an existing project directory is never reused or overwritten.
"""

from __future__ import annotations

from pathlib import Path

from kodo.runtime._engine import _slugify_project_name, _unique_child_dir


def test_slugify_lowercases_and_dashes() -> None:
    assert _slugify_project_name("My Todo App") == "my-todo-app"
    assert _slugify_project_name("  Hello, World!  ") == "hello-world"
    assert _slugify_project_name("Foo___Bar 42") == "foo-bar-42"


def test_slugify_falls_back_when_nothing_usable() -> None:
    assert _slugify_project_name("") == "project"
    assert _slugify_project_name("***") == "project"


def test_unique_child_dir_uses_slug_when_free(tmp_path: Path) -> None:
    assert _unique_child_dir(tmp_path, "app") == tmp_path / "app"


def test_unique_child_dir_auto_suffixes_on_collision(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    assert _unique_child_dir(tmp_path, "app") == tmp_path / "app-2"
    (tmp_path / "app-2").mkdir()
    assert _unique_child_dir(tmp_path, "app") == tmp_path / "app-3"
