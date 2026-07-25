"""Tests for ``kodo.validator._harness`` -- the high-level validation facade.

Covers the pure dataclasses, the ``__pin_llm_under_test`` helper, and
lifecycle methods with mocked dependencies.  No real server or LLM is spawned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kodo.validator._harness import Modes, TurnResult, ValidationHarness

# ---------------------------------------------------------------------------
# Modes / TurnResult dataclasses
# ---------------------------------------------------------------------------


def test_modes_defaults() -> None:
    m = Modes()
    assert m.autonomous is False
    assert m.workflow == "problem_solving"
    assert m.edit_control == "smart"
    assert m.command_control == "smart"


def test_modes_custom_values() -> None:
    m = Modes(
        autonomous=True,
        workflow="guided",
        edit_control="review_all",
        command_control="defensive",
    )
    assert m.autonomous is True
    assert m.workflow == "guided"
    assert m.edit_control == "review_all"
    assert m.command_control == "defensive"


def test_modes_is_frozen() -> None:
    m = Modes()
    with pytest.raises(AttributeError):
        m.autonomous = True  # type: ignore[misc]


def test_turn_result_defaults() -> None:
    tr = TurnResult(prompt="hi", final_phase="idle", assistant_text="hello")
    assert tr.tool_calls == []
    assert tr.interactions == []
    assert tr.errors == []
    assert tr.entries == []


def test_turn_result_with_data() -> None:
    tr = TurnResult(
        prompt="hi",
        final_phase="idle",
        assistant_text="hello",
        tool_calls=[{"name": "read_file"}],
    )
    assert len(tr.tool_calls) == 1


# ---------------------------------------------------------------------------
# ValidationHarness -- constructor & properties
# ---------------------------------------------------------------------------


def test_harness_creates_run_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    harness = ValidationHarness(
        run_dir,
        llm_under_test="fake-llm",
        validation_llm="fake-val",
    )
    assert run_dir.exists()
    assert harness.run_dir == run_dir.resolve()


def test_harness_llm_under_test_property(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="qwen36-27b",
        validation_llm="fake-val",
    )
    assert harness.llm_under_test == "qwen36-27b"


def test_harness_validation_llm_property(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="fake-llm",
        validation_llm="qwen-val",
    )
    assert harness.validation_llm == "qwen-val"


def test_harness_workspace_property(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="fake-llm",
        validation_llm="fake-val",
    )
    assert harness.workspace is not None


def test_harness_transcript_property(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="fake-llm",
        validation_llm="fake-val",
    )
    assert harness.transcript is not None


def test_harness_client_raises_before_start(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="fake-llm",
        validation_llm="fake-val",
    )
    with pytest.raises(RuntimeError, match="Harness not started"):
        _ = harness.client


# ---------------------------------------------------------------------------
# ValidationHarness -- __pin_llm_under_test
# ---------------------------------------------------------------------------


def test_pin_llm_under_test_empty_overrides(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="my-llm",
        validation_llm="fake-val",
    )
    result = harness._ValidationHarness__pin_llm_under_test(None)
    assert result["mode"] == "local"
    assert result["models"]["local"] == "my-llm"


def test_pin_llm_under_test_merges_with_existing(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="my-llm",
        validation_llm="fake-val",
    )
    overrides = {"theme": "dark", "models": {"remote": "gpt-4"}}
    result = harness._ValidationHarness__pin_llm_under_test(overrides)
    assert result["mode"] == "local"
    assert result["models"]["local"] == "my-llm"
    assert result["models"]["remote"] == "gpt-4"
    assert result["theme"] == "dark"


def test_pin_llm_under_test_overrides_existing_local(tmp_path: Path) -> None:
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="my-llm",
        validation_llm="fake-val",
    )
    overrides = {"models": {"local": "other-llm"}}
    result = harness._ValidationHarness__pin_llm_under_test(overrides)
    assert result["models"]["local"] == "my-llm"


# ---------------------------------------------------------------------------
# ValidationHarness -- start / shutdown / evaluate (mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_deps(tmp_path: Path) -> dict[str, Any]:
    """Pre-mock all the heavy dependencies of start()."""
    deps: dict[str, Any] = {}

    # clone_kodo_home
    deps["clone_kodo_home"] = MagicMock(return_value=tmp_path / "kodo-home")

    # ServerProcess
    mock_server = MagicMock()
    mock_server.start = AsyncMock()
    mock_server.stop = AsyncMock()
    mock_server.ws_url = "ws://127.0.0.1:12345/ws"
    deps["ServerProcess"] = MagicMock(return_value=mock_server)

    # ValidatorClient
    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.hello = AsyncMock(return_value={"local_registry": []})
    mock_client.close = AsyncMock()
    mock_client.request = AsyncMock()
    mock_client.wait_turn_end = AsyncMock(return_value="idle")
    mock_client.session_id = "test-session"
    deps["ValidatorClient"] = MagicMock(return_value=mock_client)

    # ensure_local_llms_installed
    deps["ensure_local_llms_installed"] = AsyncMock()

    return deps


@pytest.mark.asyncio
async def test_harness_start_calls_all_steps(
    tmp_path: Path,
    _mock_deps: dict[str, Any],
) -> None:
    """start() clones home, starts server, connects client."""
    import kodo.validator._harness as _harness

    with (
        patch.object(_harness, "clone_kodo_home", _mock_deps["clone_kodo_home"]),
        patch.object(_harness, "ServerProcess", _mock_deps["ServerProcess"]),
        patch.object(_harness, "ValidatorClient", _mock_deps["ValidatorClient"]),
        patch.object(
            _harness,
            "ensure_local_llms_installed",
            _mock_deps["ensure_local_llms_installed"],
        ),
    ):
        harness = ValidationHarness(
            tmp_path / "run",
            llm_under_test="test-llm",
            validation_llm="fake-val",
        )
        await harness.start()

    _mock_deps["clone_kodo_home"].assert_called_once()
    _mock_deps["ServerProcess"].assert_called_once()
    _mock_deps["ValidatorClient"].assert_called_once()
    _mock_deps["ServerProcess"].return_value.start.assert_awaited_once()
    _mock_deps["ValidatorClient"].return_value.connect.assert_awaited_once()
    _mock_deps["ValidatorClient"].return_value.hello.assert_awaited_once()


@pytest.mark.asyncio
async def test_harness_shutdown_stops_server_and_client(
    tmp_path: Path,
    _mock_deps: dict[str, Any],
) -> None:
    """shutdown() disconnects the client, stops the server."""
    import kodo.validator._harness as _harness

    with (
        patch.object(_harness, "clone_kodo_home", _mock_deps["clone_kodo_home"]),
        patch.object(_harness, "ServerProcess", _mock_deps["ServerProcess"]),
        patch.object(_harness, "ValidatorClient", _mock_deps["ValidatorClient"]),
        patch.object(
            _harness,
            "ensure_local_llms_installed",
            _mock_deps["ensure_local_llms_installed"],
        ),
    ):
        harness = ValidationHarness(
            tmp_path / "run",
            llm_under_test="test-llm",
            validation_llm="fake-val",
        )
        await harness.start()
        await harness.shutdown()

    _mock_deps["ValidatorClient"].return_value.close.assert_awaited_once()
    _mock_deps["ServerProcess"].return_value.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_harness_evaluate_raises_without_rvp(
    tmp_path: Path,
) -> None:
    """evaluate() without a result_validation_prompt raises RuntimeError."""
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="test-llm",
        validation_llm="fake-val",
    )
    with pytest.raises(RuntimeError, match="No result_validation_prompt"):
        await harness.evaluate()


@pytest.mark.asyncio
async def test_harness_evaluate_raises_when_not_started(
    tmp_path: Path,
) -> None:
    """evaluate() before start() raises RuntimeError."""
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="test-llm",
        validation_llm="fake-val",
        result_validation_prompt="judge prompt",
    )
    with pytest.raises(RuntimeError, match="Harness not started"):
        await harness.evaluate()


@pytest.mark.asyncio
async def test_harness_session_id_property_before_start(tmp_path: Path) -> None:
    """session_id is None before start()."""
    harness = ValidationHarness(
        tmp_path / "run",
        llm_under_test="test-llm",
        validation_llm="fake-val",
    )
    assert harness.session_id is None


@pytest.mark.asyncio
async def test_harness_session_id_property_after_start(
    tmp_path: Path,
    _mock_deps: dict[str, Any],
) -> None:
    """session_id reflects the client's session_id after start()."""
    import kodo.validator._harness as _harness

    with (
        patch.object(_harness, "clone_kodo_home", _mock_deps["clone_kodo_home"]),
        patch.object(_harness, "ServerProcess", _mock_deps["ServerProcess"]),
        patch.object(_harness, "ValidatorClient", _mock_deps["ValidatorClient"]),
        patch.object(
            _harness,
            "ensure_local_llms_installed",
            _mock_deps["ensure_local_llms_installed"],
        ),
    ):
        harness = ValidationHarness(
            tmp_path / "run",
            llm_under_test="test-llm",
            validation_llm="fake-val",
        )
        await harness.start()
        assert harness.session_id == "test-session"
