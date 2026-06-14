"""Tests for select_toolchain — Tech Stack → ToolchainPlugin mapping."""

from __future__ import annotations

from pathlib import Path

from kodo.toolchains import NodePlugin, PythonPlugin, select_toolchain


def test_selects_python_from_primary_language_line(tmp_path: Path) -> None:
    content = (
        "# Tech Stack\n"
        "- **Primary programming language:** Python 3.12 — from Narrative.\n"
        "- **Test framework:** pytest — user-specified.\n"
    )
    assert isinstance(select_toolchain(content, tmp_path), PythonPlugin)


def test_selects_node_for_typescript(tmp_path: Path) -> None:
    content = "- **Primary programming language:** TypeScript 5.4 on Node.js 20 LTS — user.\n"
    assert isinstance(select_toolchain(content, tmp_path), NodePlugin)


def test_selects_node_for_javascript(tmp_path: Path) -> None:
    content = "- **Primary programming language:** JavaScript (Node.js 20) — user.\n"
    assert isinstance(select_toolchain(content, tmp_path), NodePlugin)


def test_primary_language_line_wins_over_incidental_mentions(tmp_path: Path) -> None:
    # A Python project that merely mentions a Node-based tool elsewhere.
    content = (
        "- **Primary programming language:** Python 3.12 — from Narrative.\n"
        "- **Frontend build tool:** esbuild (Node) — user-specified.\n"
    )
    assert isinstance(select_toolchain(content, tmp_path), PythonPlugin)


def test_defaults_to_python_when_unrecognized(tmp_path: Path) -> None:
    assert isinstance(select_toolchain("no language here", tmp_path), PythonPlugin)
    assert isinstance(select_toolchain("", tmp_path), PythonPlugin)
