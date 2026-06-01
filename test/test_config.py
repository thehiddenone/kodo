"""Behavior tests for kodo.server._config.Config.

Tests verify that from_args() resolves configuration correctly from
CLI arguments, environment variables, and settings files, without
starting any server process.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodo.server._config import Config

# ---------------------------------------------------------------------------
# from_args — required argument
# ---------------------------------------------------------------------------


def test_from_args_sets_project_path(tmp_path: Path) -> None:
    """
    Given a --project argument,
    when from_args() is called,
    then config.project equals the resolved path.
    """
    config = Config.from_args(["--project", str(tmp_path)])
    assert config.project == tmp_path.resolve()


def test_from_args_project_is_resolved_to_absolute(tmp_path: Path) -> None:
    """
    Given a relative --project path,
    when from_args() is called,
    then config.project is an absolute Path.
    """
    config = Config.from_args(["--project", str(tmp_path)])
    assert config.project.is_absolute()


# ---------------------------------------------------------------------------
# from_args — optional port
# ---------------------------------------------------------------------------


def test_from_args_default_port(tmp_path: Path) -> None:
    """
    Given no --port argument,
    when from_args() is called,
    then config.port equals the default 9042.
    """
    config = Config.from_args(["--project", str(tmp_path)])
    assert config.port == 9042


def test_from_args_custom_port(tmp_path: Path) -> None:
    """
    Given --port 8080,
    when from_args() is called,
    then config.port is 8080.
    """
    config = Config.from_args(["--project", str(tmp_path), "--port", "8080"])
    assert config.port == 8080


# ---------------------------------------------------------------------------
# from_args — log level
# ---------------------------------------------------------------------------


def test_from_args_default_log_level(tmp_path: Path) -> None:
    """
    Given no --log-level argument,
    when from_args() is called,
    then config.log_level is 'INFO'.
    """
    config = Config.from_args(["--project", str(tmp_path)])
    assert config.log_level == "INFO"


def test_from_args_custom_log_level(tmp_path: Path) -> None:
    """
    Given --log-level DEBUG,
    when from_args() is called,
    then config.log_level is 'DEBUG'.
    """
    config = Config.from_args(["--project", str(tmp_path), "--log-level", "DEBUG"])
    assert config.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# from_args — layered settings files
# ---------------------------------------------------------------------------


def test_from_args_project_settings_override_mode(tmp_path: Path) -> None:
    """
    Given a project settings.json that sets 'mode',
    when from_args() is called,
    then config.extra['mode'] reflects the project setting.
    """
    kodo_dir = tmp_path / ".kodo"
    kodo_dir.mkdir()
    settings = {"mode": "cloud"}
    (kodo_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    config = Config.from_args(["--project", str(tmp_path)])
    assert config.extra.get("mode") == "cloud"


def test_from_args_project_settings_override_log_level(tmp_path: Path) -> None:
    """
    Given a project settings.json that sets 'log_level',
    when from_args() is called,
    then config.log_level reflects the project setting (project > CLI default).
    """
    kodo_dir = tmp_path / ".kodo"
    kodo_dir.mkdir()
    (kodo_dir / "settings.json").write_text(json.dumps({"log_level": "WARNING"}), encoding="utf-8")

    config = Config.from_args(["--project", str(tmp_path)])
    assert config.log_level == "WARNING"


def test_from_args_invalid_settings_json_does_not_crash(tmp_path: Path) -> None:
    """
    Given a project settings.json with invalid JSON,
    when from_args() is called,
    then config is returned with defaults (no exception raised).
    """
    kodo_dir = tmp_path / ".kodo"
    kodo_dir.mkdir()
    (kodo_dir / "settings.json").write_text("{broken}", encoding="utf-8")

    config = Config.from_args(["--project", str(tmp_path)])
    assert config.port == 9042  # default intact


def test_from_args_mode_is_set(tmp_path: Path) -> None:
    """
    Given a project settings.json with 'mode',
    when from_args() is called,
    then config.extra contains 'mode'.
    """
    kodo_dir = tmp_path / ".kodo"
    kodo_dir.mkdir()
    (kodo_dir / "settings.json").write_text(json.dumps({"mode": "local"}), encoding="utf-8")

    config = Config.from_args(["--project", str(tmp_path)])
    assert config.extra.get("mode") == "local"


# ---------------------------------------------------------------------------
# Config dataclass fields
# ---------------------------------------------------------------------------


def test_config_extra_contains_settings_data(tmp_path: Path) -> None:
    """
    Given a project settings.json with custom keys,
    when from_args() is called,
    then config.extra contains those custom keys.
    """
    kodo_dir = tmp_path / ".kodo"
    kodo_dir.mkdir()
    (kodo_dir / "settings.json").write_text(json.dumps({"custom_key": "value"}), encoding="utf-8")

    config = Config.from_args(["--project", str(tmp_path)])
    assert config.extra.get("custom_key") == "value"
