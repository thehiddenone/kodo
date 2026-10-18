"""Behavioral tests for session ↔ LLM binding (doc/SESSION_LLM_BINDING.md).

A session binds only when a prompt is dequeued; from then on it runs on its
own LLM whatever the sidebar does, a sidebar edit is captured as its pending
selection, and every prompt that changes something gets a change element.

Same ``object.__new__(WorkflowEngine)`` pattern as the other engine tests, with
a real :class:`~kodo.state.TransientStore` and the real agent registry so the
binding round-trips through ``transient.json`` exactly as in production.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from kodo.agents import AgentRegistry
from kodo.llms import cloud_thinking_default_tier, cloud_thinking_tiers, get_local_registry
from kodo.runtime import SessionState, WorkflowEngine
from kodo.state import TransientStore

_REGISTRY = AgentRegistry(Path(__file__).resolve().parents[1] / "src" / "kodo" / "agents")

_DEFAULTS: dict[str, object] = {
    "mode": "cloud",
    "active_cloud_vendor": "anthropic",
    "models": {
        "local": "",
        "cloud": {
            "anthropic": {
                "low": "claude-haiku-4-5-20251001",
                "medium": "claude-sonnet-5",
                "high": "claude-opus-5",
                "max": "claude-fable-5",
            },
            "openai": {
                "low": "gpt-5.6-luna",
                "medium": "gpt-5.6-terra",
                "high": "gpt-5.6-terra",
                "max": "gpt-5.6-sol",
            },
        },
        "cloud_uniform": {},
    },
}


class _Emitters:
    def __init__(self) -> None:
        self.params: list[tuple[bool, list[dict[str, object]]]] = []
        self.errors: list[str] = []

    async def emit_session_params(self, first: bool, changes: list[dict[str, object]]) -> None:
        self.params.append((first, changes))

    async def emit_error(self, message: str, *, recoverable: bool) -> None:
        self.errors.append(message)

    async def emit_state(self) -> None:
        return None


class _Compactor:
    def __init__(self) -> None:
        self.switch_limits: list[int] = []

    async def compact_before_switch(self, new_limit: int) -> None:
        self.switch_limits.append(new_limit)

    def note_active_model(self, model_key: str) -> None:
        return None


class _Harness:
    """One engine plus the sidebar state it reads."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings: dict[str, object] = copy.deepcopy(_DEFAULTS)
        transient = TransientStore(tmp_path / "workspace")
        transient.attach_session("s1", resumed=False)
        engine = object.__new__(WorkflowEngine)
        engine._get_settings = lambda: self.settings
        engine._get_default_settings = lambda: copy.deepcopy(_DEFAULTS)
        engine._transient = transient
        engine._registry = _REGISTRY
        engine._session = SessionState(session_id="s1")
        engine._session.top_agent = _REGISTRY.default_top_agent()
        engine._emitters = _Emitters()
        engine._compactor = _Compactor()
        engine._main_messages = []
        engine._last_thinking_base_llm = None
        engine._orch_session_id = "s1"
        self.engine = engine

    @property
    def emitters(self) -> _Emitters:
        return self.engine._emitters  # type: ignore[return-value]

    def pick_vendor(self, vendor: str) -> None:
        self.settings["mode"] = "cloud"
        self.settings["active_cloud_vendor"] = vendor

    def pick_local(self, name: str) -> None:
        self.settings["mode"] = "local"
        self.settings["models"]["local"] = name  # type: ignore[index]


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return _Harness(tmp_path)


def _params(rows: list[dict[str, object]]) -> list[object]:
    return [r["param"] for r in rows]


# ---------------------------------------------------------------------------
# Binding at the prompt
# ---------------------------------------------------------------------------


async def test_a_new_session_is_unbound_and_follows_the_sidebar(h: _Harness) -> None:
    h.engine._refresh_llm_state()
    assert h.engine.session.llm["bound"] is False

    h.pick_vendor("openai")

    assert h.engine._resolve_model_key("medium") == "gpt-5.6-terra"


async def test_the_first_prompt_binds_to_the_sidebar_and_says_so(h: _Harness) -> None:
    assert await h.engine._bind_for_prompt() is True

    llm = h.engine.session.llm
    assert llm["bound"] is True
    assert llm["binding"]["vendor"] == "anthropic"  # type: ignore[index]
    [(first, rows)] = h.emitters.params
    assert first is True
    assert rows[0]["param"] == "llm" and rows[0]["from"] is None


async def test_a_bound_session_ignores_later_sidebar_picks(h: _Harness) -> None:
    await h.engine._bind_for_prompt()

    h.pick_vendor("openai")

    assert h.engine._resolve_model_key("high") == "claude-opus-5"


async def test_the_binding_survives_in_transient_json(h: _Harness, tmp_path: Path) -> None:
    await h.engine._bind_for_prompt()

    reloaded = TransientStore(tmp_path / "workspace")
    reloaded.attach_session("s1", resumed=True)

    assert reloaded.llm_binding is not None
    assert reloaded.llm_binding["vendor"] == "anthropic"
    assert reloaded.params_snapshot is not None


async def test_an_unchanged_second_prompt_shows_no_change_element(h: _Harness) -> None:
    await h.engine._bind_for_prompt()

    await h.engine._bind_for_prompt()

    assert len(h.emitters.params) == 1


async def test_nothing_selected_refuses_the_prompt(h: _Harness) -> None:
    h.pick_local("")

    assert await h.engine._bind_for_prompt() is False

    assert h.emitters.errors and "No LLM is selected" in h.emitters.errors[0]
    assert h.engine.session.llm["bound"] is False


async def test_an_uninstalled_local_card_refuses_the_prompt(h: _Harness) -> None:
    h.pick_local("no-such-quant")

    assert await h.engine._bind_for_prompt() is False

    assert "no-such-quant" in h.emitters.errors[0]


async def test_a_session_from_before_bindings_binds_without_an_element(h: _Harness) -> None:
    h.engine._main_messages = [object()]  # type: ignore[list-item]

    assert await h.engine._bind_for_prompt() is True

    assert h.emitters.params == []
    assert h.engine.session.llm["bound"] is True


async def test_a_local_card_binds_with_its_entry(h: _Harness, tmp_path: Path) -> None:
    name = next(iter(get_local_registry(tmp_path / "home" / ".kodo")))
    h.pick_local(name)

    await h.engine._bind_for_prompt()

    binding = h.engine.session.llm["binding"]
    assert binding["kind"] == "local" and binding["entry"] == name  # type: ignore[index]


# ---------------------------------------------------------------------------
# Capture (sidebar edits on a bound session) and the next prompt
# ---------------------------------------------------------------------------


async def test_capture_on_an_unbound_session_does_nothing(h: _Harness) -> None:
    assert await h.engine.handle_llm_selection_capture() is False
    assert h.engine.session.llm["selection"] is None


async def test_capture_sets_the_next_llm_without_moving_the_current_one(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    h.pick_vendor("openai")

    assert await h.engine.handle_llm_selection_capture() is True

    llm = h.engine.session.llm
    assert llm["selection"]["vendor"] == "openai"  # type: ignore[index]
    assert llm["binding"]["vendor"] == "anthropic"  # type: ignore[index]
    assert llm["next_label"] != ""
    assert h.engine._resolve_model_key("high") == "claude-opus-5"


async def test_the_next_prompt_switches_and_lists_the_llm(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    h.pick_vendor("openai")
    await h.engine.handle_llm_selection_capture()

    await h.engine._bind_for_prompt()

    assert h.engine._resolve_model_key("high") == "gpt-5.6-terra"
    first, rows = h.emitters.params[-1]
    assert first is False
    assert rows[0]["param"] == "llm"
    assert h.engine.session.llm["next_label"] == ""


async def test_switching_compacts_against_the_new_window_first(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    h.pick_vendor("openai")
    await h.engine.handle_llm_selection_capture()

    await h.engine._bind_for_prompt()

    assert len(h.engine._compactor.switch_limits) == 1  # type: ignore[attr-defined]


async def test_a_default_thinking_tier_is_not_listed_on_a_switch(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    h.pick_vendor("openai")
    await h.engine.handle_llm_selection_capture()

    await h.engine._bind_for_prompt()

    assert "thinking" not in _params(h.emitters.params[-1][1])
    assert h.engine.session.thinking_level == cloud_thinking_default_tier("openai")


async def test_a_non_default_thinking_tier_is_listed_on_a_switch(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    tier = next(
        t
        for t in cloud_thinking_tiers("anthropic")
        if t != cloud_thinking_default_tier("anthropic")
    )
    assert await h.engine.handle_thinking_level_set(tier) is True
    await h.engine._bind_for_prompt()  # a prompt runs on the non-default tier
    h.pick_vendor("openai")
    await h.engine.handle_llm_selection_capture()

    await h.engine._bind_for_prompt()

    rows = h.emitters.params[-1][1]
    thinking = next(r for r in rows if r["param"] == "thinking")
    assert thinking["from"] == tier
    assert thinking["to"] == cloud_thinking_default_tier("openai")


async def test_a_tier_set_and_switched_away_before_any_prompt_is_not_listed(
    h: _Harness,
) -> None:
    # The element diffs prompt to prompt: a value no prompt ever ran with is
    # not a change the transcript needs to show.
    await h.engine._bind_for_prompt()
    tier = next(
        t
        for t in cloud_thinking_tiers("anthropic")
        if t != cloud_thinking_default_tier("anthropic")
    )
    await h.engine.handle_thinking_level_set(tier)
    h.pick_vendor("openai")
    await h.engine.handle_llm_selection_capture()

    await h.engine._bind_for_prompt()

    assert "thinking" not in _params(h.emitters.params[-1][1])


async def test_a_mode_change_alone_gets_its_own_element(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    h.engine.session.autonomous = True

    await h.engine._bind_for_prompt()

    assert _params(h.emitters.params[-1][1]) == ["mode"]


async def test_thinking_tiers_follow_the_pending_llm(h: _Harness) -> None:
    await h.engine._bind_for_prompt()
    h.pick_vendor("openai")
    await h.engine.handle_llm_selection_capture()

    assert h.engine._current_base_llm() == "openai"
