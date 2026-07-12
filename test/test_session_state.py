"""Tests for :class:`kodo.runtime.SessionState` wire serialisation.

``to_dict`` is the payload of the ``state`` event (WS_PROTOCOL §5.1). The two
frozen toggles (``autonomous``/``workflow_mode``) carry both the user-facing
*selected* value and its per-turn frozen *effective* twin so the client can tell
"in effect" from "queued for the next prompt". ``edit_control``/
``command_control`` are never frozen — only a single mirrored value is emitted.
"""

from kodo.runtime import SessionState


def test_to_dict_defaults_carry_selected_and_effective_pairs() -> None:
    payload = SessionState().to_dict()
    assert payload["autonomous"] is False
    assert payload["effective_autonomous"] is False
    assert payload["workflow_mode"] == "guided"
    assert payload["effective_workflow_mode"] == "guided"
    assert payload["edit_control"] == "smart"
    assert payload["command_control"] == "smart"
    assert payload["thinking_level"] == ""
    # The never-frozen toggles emit no effective twin.
    assert "effective_edit_control" not in payload
    assert "effective_command_control" not in payload
    assert "effective_thinking_level" not in payload


def test_to_dict_reports_diverged_selected_vs_effective() -> None:
    # A frozen toggle flipped mid-turn: the selected value moves but the frozen
    # effective value (what the in-flight prompt uses) stays put. The unfrozen
    # edit/command postures are reported verbatim.
    state = SessionState()
    state.autonomous = True
    state.workflow_mode = "problem_solving"
    state.edit_control = "allow_all"
    state.command_control = "permissive"
    state.thinking_level = "unlimited"

    payload = state.to_dict()
    assert payload["autonomous"] is True
    assert payload["effective_autonomous"] is False
    assert payload["workflow_mode"] == "problem_solving"
    assert payload["effective_workflow_mode"] == "guided"
    assert payload["edit_control"] == "allow_all"
    assert payload["command_control"] == "permissive"
    assert payload["thinking_level"] == "unlimited"
