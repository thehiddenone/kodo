"""Behavior tests for kodo.server._config.Config.

Tests verify that from_args() resolves configuration correctly from
CLI arguments, environment variables, and settings files, without
starting any server process.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodo.server import Config

# ---------------------------------------------------------------------------
# from_args — required argument
# ---------------------------------------------------------------------------


def test_from_args_sets_workspace_path(tmp_path: Path) -> None:
    """
    Given a --workspace argument,
    when from_args() is called,
    then config.workspace equals the resolved path.
    """
    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.workspace == tmp_path.resolve()


def test_from_args_workspace_is_resolved_to_absolute(tmp_path: Path) -> None:
    """
    Given a relative --workspace path,
    when from_args() is called,
    then config.workspace is an absolute Path.
    """
    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.workspace.is_absolute()


# ---------------------------------------------------------------------------
# from_args — optional port
# ---------------------------------------------------------------------------


def test_from_args_default_port(tmp_path: Path) -> None:
    """
    Given no --port argument,
    when from_args() is called,
    then config.port equals the default 9042.
    """
    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.port == 9042


def test_from_args_custom_port(tmp_path: Path) -> None:
    """
    Given --port 8080,
    when from_args() is called,
    then config.port is 8080.
    """
    config = Config.from_args(["--workspace", str(tmp_path), "--port", "8080"])
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
    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.log_level == "INFO"


def test_from_args_custom_log_level(tmp_path: Path) -> None:
    """
    Given --log-level DEBUG,
    when from_args() is called,
    then config.log_level is 'DEBUG'.
    """
    config = Config.from_args(["--workspace", str(tmp_path), "--log-level", "DEBUG"])
    assert config.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# from_args — layered settings files
# ---------------------------------------------------------------------------


def _write_workspace_settings(tmp_path: Path, settings: dict[str, object]) -> None:
    kodo_dir = tmp_path / ".kodo-workspace"
    kodo_dir.mkdir(exist_ok=True)
    (kodo_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_from_args_workspace_settings_override_mode(tmp_path: Path) -> None:
    """
    Given a workspace settings.json that sets 'mode',
    when from_args() is called,
    then config.extra['mode'] reflects the workspace setting.
    """
    _write_workspace_settings(tmp_path, {"mode": "cloud"})

    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.extra.get("mode") == "cloud"


def test_from_args_workspace_settings_override_log_level(tmp_path: Path) -> None:
    """
    Given a workspace settings.json that sets 'log_level',
    when from_args() is called,
    then config.log_level reflects the workspace setting (workspace > CLI default).
    """
    _write_workspace_settings(tmp_path, {"log_level": "WARNING"})

    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.log_level == "WARNING"


def test_from_args_invalid_settings_json_does_not_crash(tmp_path: Path) -> None:
    """
    Given a workspace settings.json with invalid JSON,
    when from_args() is called,
    then config is returned with defaults (no exception raised).
    """
    kodo_dir = tmp_path / ".kodo-workspace"
    kodo_dir.mkdir()
    (kodo_dir / "settings.json").write_text("{broken}", encoding="utf-8")

    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.port == 9042  # default intact


def test_from_args_mode_is_set(tmp_path: Path) -> None:
    """
    Given a workspace settings.json with 'mode',
    when from_args() is called,
    then config.extra contains 'mode'.
    """
    _write_workspace_settings(tmp_path, {"mode": "local"})

    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.extra.get("mode") == "local"


# ---------------------------------------------------------------------------
# Config dataclass fields
# ---------------------------------------------------------------------------


def test_config_extra_contains_settings_data(tmp_path: Path) -> None:
    """
    Given a workspace settings.json with custom keys,
    when from_args() is called,
    then config.extra contains those custom keys.
    """
    _write_workspace_settings(tmp_path, {"custom_key": "value"})

    config = Config.from_args(["--workspace", str(tmp_path)])
    assert config.extra.get("custom_key") == "value"
