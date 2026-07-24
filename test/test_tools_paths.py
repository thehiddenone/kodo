"""Unit tests for :func:`kodo.tools.root_for` — the "which bound root does
this resolved path belong to" lookup shared by every ``kodo.guided_state``
caller since the multi-project rework (doc/WS_PROTOCOL.md).
"""

from __future__ import annotations

from pathlib import Path

from kodo.tools import RootPath, root_for


def test_root_for_finds_containing_root(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    roots = (RootPath(name="proj", path=str(proj)),)

    found = root_for(roots, proj / "specs" / "a.md")

    assert found is not None
    assert found.name == "proj"


def test_root_for_matches_the_root_itself(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    roots = (RootPath(name="proj", path=str(proj)),)

    assert root_for(roots, proj) is not None


def test_root_for_returns_none_when_no_root_contains_the_path(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    other = tmp_path / "other"
    proj.mkdir()
    other.mkdir()
    roots = (RootPath(name="proj", path=str(proj)),)

    assert root_for(roots, other / "a.md") is None


def test_root_for_returns_none_for_empty_roots(tmp_path: Path) -> None:
    assert root_for((), tmp_path / "a.md") is None


def test_root_for_picks_the_longest_matching_root_when_nested(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    roots = (
        RootPath(name="outer", path=str(outer)),
        RootPath(name="inner", path=str(inner)),
    )

    found = root_for(roots, inner / "specs" / "a.md")

    assert found is not None
    assert found.name == "inner"


def test_root_for_disambiguates_multiple_bound_projects(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    roots = (RootPath(name="a", path=str(a)), RootPath(name="b", path=str(b)))

    assert root_for(roots, a / "specs" / "x.md").name == "a"  # type: ignore[union-attr]
    assert root_for(roots, b / "specs" / "x.md").name == "b"  # type: ignore[union-attr]
