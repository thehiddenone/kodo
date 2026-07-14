"""Tests for :mod:`kodo.common._tempdir` — the OS temp-directory helper
shared by the security layer and the file-tool path resolvers."""

from __future__ import annotations

import os
import tempfile

import pytest

from kodo.common import system_temp_roots


def test_includes_gettempdir_realpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/var/folders/xx/T")
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    roots = system_temp_roots()
    assert "/var/folders/xx/T" in roots


def test_posix_always_includes_literal_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/var/folders/xx/T")
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    roots = system_temp_roots()
    assert "/tmp" in roots
    assert "/var/folders/xx/T" in roots


def test_windows_does_not_add_literal_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "C:\\Users\\bob\\AppData\\Local\\Temp")
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    roots = system_temp_roots()
    assert roots == ("C:\\Users\\bob\\AppData\\Local\\Temp",)


def test_dedupes_when_tmp_and_gettempdir_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/tmp")
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    assert system_temp_roots() == ("/tmp",)


def test_real_platform_smoke() -> None:
    # No monkeypatching: exercise the real interpreter/platform values.
    roots = system_temp_roots()
    assert roots
    assert all(os.path.isabs(r) for r in roots)
