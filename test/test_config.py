"""Behavior tests for kodo.server._config.Config.

The server is a machine-wide singleton rooted at ``~/.kodo``; ``from_args`` takes
no ``--workspace`` and loads a single ``~/.kodo/settings.json``.  Tests redirect
``HOME`` to a temp dir so they never touch the real user settings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.server import Config


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    return tmp_path


def _write_settings(home: Path, settings: dict[str, object]) -> None:
    kodo_dir = home / ".kodo"
    kodo_dir.mkdir(exist_ok=True)
    (kodo_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


# ---------------------------------------------------------------------------
# port / log level
# ---------------------------------------------------------------------------


def test_from_args_default_port() -> None:
    assert Config.from_args([]).port == 9042


def test_from_args_custom_port() -> None:
    assert Config.from_args(["--port", "8080"]).port == 8080


def test_from_args_default_log_level() -> None:
    assert Config.from_args([]).log_level == "INFO"


def test_from_args_custom_log_level() -> None:
    assert Config.from_args(["--log-level", "DEBUG"]).log_level == "DEBUG"


# ---------------------------------------------------------------------------
# settings file (single ~/.kodo/settings.json)
# ---------------------------------------------------------------------------


def test_settings_override_mode(_temp_home: Path) -> None:
    _write_settings(_temp_home, {"mode": "cloud"})
    assert Config.from_args([]).extra.get("mode") == "cloud"


def test_settings_override_log_level(_temp_home: Path) -> None:
    _write_settings(_temp_home, {"log_level": "WARNING"})
    assert Config.from_args([]).log_level == "WARNING"


def test_invalid_settings_json_does_not_crash(_temp_home: Path) -> None:
    kodo_dir = _temp_home / ".kodo"
    kodo_dir.mkdir()
    (kodo_dir / "settings.json").write_text("{broken}", encoding="utf-8")
    # Falls back to compiled defaults rather than raising.
    assert Config.from_args([]).port == 9042


def test_extra_contains_custom_keys(_temp_home: Path) -> None:
    _write_settings(_temp_home, {"custom_key": "value"})
    assert Config.from_args([]).extra.get("custom_key") == "value"


def test_cloud_concurrency_default_present() -> None:
    # The compiled defaults expose cloud_concurrency so the gateway can read it.
    assert Config.from_args([]).extra.get("cloud_concurrency") == 2
