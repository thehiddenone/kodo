"""Tests for ``kodo.runtime._engine._llm.LLMPlumbingMixin``.

Plugin/model resolution, the LLM request log dir, entry-agent capability
lookup, and the two silent (never-streamed) call shapes: the return_result
turn and the multi-round silent tool loop — driven against a fake
``LLMGateway``/key provider rather than the real Anthropic/llama.cpp plugins.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodo.common import ApiKey
from kodo.llms import (
    LLMRouting,
    Message,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    TurnEnd,
    Usage,
)
from kodo.runtime import WorkflowEngine
from kodo.runtime._engine import _llm
from kodo.runtime._session import SessionState

_FAR_FUTURE_DEADLINE = time.time() + 10_000
# Cloud routing for call sites that don't exercise thinking_level: makes
# LLMPlumbingMixin._thinking_kwargs() short-circuit to {} exactly like the
# bare SimpleNamespace() these calls used to pass (which _thinking_kwargs now
# reads .residence off).
_ROUTING = LLMRouting(residence="cloud")


class _FakeGateway:
    def __init__(self, batches: list[list[object]]) -> None:
        self._batches = list(batches)
        self.calls: list[dict[str, object]] = []

    async def stream_query(self, **kwargs: object):
        self.calls.append(kwargs)
        batch = self._batches.pop(0) if self._batches else []
        for event in batch:
            yield event


class _FakeEmitters:
    def __init__(self) -> None:
        self.cost_total = 0.0
        self.cost_only_calls = 0

    def add_cost(self, usd: float) -> None:
        self.cost_total += usd

    async def emit_cost_only(self) -> None:
        self.cost_only_calls += 1


class _FakeKeyProvider:
    def __init__(self, *, api_key: str = "sk-test", error: str | None = None) -> None:
        self.api_key = api_key
        self.error = error
        self.requested: list[str] = []

    async def get_key(self, vendor: str) -> ApiKey:
        self.requested.append(vendor)
        return ApiKey(vendor=vendor, api_key=self.api_key, error=self.error)


def _make_engine(
    *,
    settings: dict[str, object] | None = None,
    gateway: _FakeGateway | None = None,
    key_provider: _FakeKeyProvider | None = None,
) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._get_settings = lambda: settings if settings is not None else {}
    engine._sink = SimpleNamespace()
    engine._orch_session_id = "sess-1"
    engine._workspace_layout = SimpleNamespace(llm_requests_dir=Path("/tmp/llm_requests"))
    engine._key_provider = key_provider or _FakeKeyProvider()
    engine._gateway = gateway or _FakeGateway([])
    engine._emitters = _FakeEmitters()
    engine._session = SessionState(session_id="s1")
    return engine


def _usage(**overrides: object) -> Usage:
    fields = dict(
        input_tokens=10, output_tokens=5, cache_write_tokens=0, cache_read_tokens=0, model="m"
    )
    fields.update(overrides)
    return Usage(**fields)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _isolated_kodo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point kodo_user_dir() at an empty tmp dir so local-registry lookups
    don't depend on the real developer machine's ~/.kodo custom entries."""
    monkeypatch.setattr(_llm, "kodo_user_dir", lambda: tmp_path)


# ---------------------------------------------------------------------------
# _find_cloud_vendor_for_model_id
# ---------------------------------------------------------------------------


def test_find_cloud_vendor_for_model_id_found() -> None:
    assert _llm._find_cloud_vendor_for_model_id("claude-opus-4-8") == "anthropic"


def test_find_cloud_vendor_for_model_id_not_found() -> None:
    assert _llm._find_cloud_vendor_for_model_id("nonexistent-model-xyz") is None


# ---------------------------------------------------------------------------
# _resolve_model_key
# ---------------------------------------------------------------------------


def test_resolve_model_key_local_mode_uses_configured_local_model() -> None:
    engine = _make_engine(settings={"mode": "local", "models": {"local": "my-local-model"}})
    assert engine._resolve_model_key("high") == "my-local-model"


def test_resolve_model_key_local_mode_default_when_unset() -> None:
    engine = _make_engine(settings={"mode": "local"})
    assert engine._resolve_model_key("high") == "llamacpp-qwen36-27b-q4-k-xl"


def test_resolve_model_key_cloud_mode_exact_capability_hit() -> None:
    engine = _make_engine(
        settings={
            "mode": "cloud",
            "active_cloud_vendor": "anthropic",
            "models": {"cloud": {"anthropic": {"high": "claude-opus-4-8"}}},
        }
    )
    assert engine._resolve_model_key("high") == "claude-opus-4-8"


def test_resolve_model_key_cloud_mode_falls_back_through_tiers() -> None:
    engine = _make_engine(
        settings={
            "mode": "cloud",
            "active_cloud_vendor": "anthropic",
            "models": {"cloud": {"anthropic": {"low": "claude-fable-5"}}},
        }
    )
    # "high" is missing, falls through medium/high/max/low in that priority order.
    assert engine._resolve_model_key("high") == "claude-fable-5"


def test_resolve_model_key_cloud_mode_falls_back_to_registry_first_model() -> None:
    engine = _make_engine(settings={"mode": "cloud", "active_cloud_vendor": "anthropic"})
    key = engine._resolve_model_key("high")
    from kodo.llms import get_cloud_registry

    assert key == get_cloud_registry()["anthropic"][0].model_id


def test_resolve_model_key_unknown_vendor_falls_back_to_capability_name() -> None:
    engine = _make_engine(settings={"mode": "cloud", "active_cloud_vendor": "no-such-vendor"})
    assert engine._resolve_model_key("high") == "high"


def test_resolve_model_key_malformed_vendor_map_is_tolerated() -> None:
    engine = _make_engine(
        settings={
            "mode": "cloud",
            "active_cloud_vendor": "anthropic",
            "models": {"cloud": {"anthropic": "not-a-dict"}},
        }
    )
    from kodo.llms import get_cloud_registry

    assert engine._resolve_model_key("high") == get_cloud_registry()["anthropic"][0].model_id


def test_resolve_model_key_malformed_models_map_is_tolerated() -> None:
    engine = _make_engine(
        settings={"mode": "cloud", "active_cloud_vendor": "anthropic", "models": "not-a-dict"}
    )
    from kodo.llms import get_cloud_registry

    assert engine._resolve_model_key("high") == get_cloud_registry()["anthropic"][0].model_id


# ---------------------------------------------------------------------------
# _resolve_plugin
# ---------------------------------------------------------------------------


async def test_resolve_plugin_local_residence() -> None:
    engine = _make_engine(
        settings={"mode": "local", "models": {"local": "atomicchat-qwen36-27b-q8"}}
    )

    plugin, model_id, routing = await engine._resolve_plugin("medium")

    assert model_id == "atomicchat-qwen36-27b-q8"
    assert routing.residence == "local"
    assert engine._current_vendor is None
    assert plugin.name == "llamacpp"


async def test_resolve_plugin_cloud_residence_success() -> None:
    key_provider = _FakeKeyProvider(api_key="sk-abc")
    engine = _make_engine(
        settings={
            "mode": "cloud",
            "active_cloud_vendor": "anthropic",
            "models": {"cloud": {"anthropic": {"medium": "claude-opus-4-8"}}},
        },
        key_provider=key_provider,
    )

    plugin, model_id, routing = await engine._resolve_plugin("medium")

    assert model_id == "claude-opus-4-8"
    assert routing.residence == "cloud"
    assert routing.vendor == "anthropic"
    assert engine._current_vendor == "anthropic"
    assert plugin.name == "anthropic"
    assert key_provider.requested == ["anthropic"]


async def test_resolve_plugin_force_model_key_overrides_settings() -> None:
    engine = _make_engine(
        settings={"mode": "local", "models": {"local": "atomicchat-qwen36-27b-q8"}}
    )

    _plugin, model_id, _routing = await engine._resolve_plugin(
        "medium", force_model_key="atomicchat-qwen36-27b-q8"
    )

    assert model_id == "atomicchat-qwen36-27b-q8"


async def test_resolve_plugin_rejects_unsupported_vendor_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _make_engine(settings={"mode": "cloud", "active_cloud_vendor": "openai"})
    monkeypatch.setattr(_llm, "get_cloud_vendor_module", lambda vendor: "kodo.llms.openai")

    with pytest.raises(RuntimeError, match="Unsupported cloud vendor"):
        await engine._resolve_plugin("medium")


async def test_resolve_plugin_raises_when_key_rejected() -> None:
    key_provider = _FakeKeyProvider(error="user cancelled")
    engine = _make_engine(
        settings={"mode": "cloud", "active_cloud_vendor": "anthropic"}, key_provider=key_provider
    )

    with pytest.raises(RuntimeError, match="API key request rejected"):
        await engine._resolve_plugin("medium")


async def test_resolve_plugin_falls_back_to_settings_vendor_when_model_unknown() -> None:
    engine = _make_engine(settings={"mode": "cloud", "active_cloud_vendor": "anthropic"})

    _plugin, model_id, routing = await engine._resolve_plugin(
        "medium", force_model_key="totally-unknown-model-id"
    )

    assert model_id == "totally-unknown-model-id"
    assert routing.vendor == "anthropic"


# ---------------------------------------------------------------------------
# _llm_logs_dir / _clear_llm_request_logs
# ---------------------------------------------------------------------------


def test_llm_logs_dir_uses_session_id() -> None:
    engine = _make_engine()
    assert engine._llm_logs_dir() == Path("/tmp/llm_requests/sess-1")


def test_llm_logs_dir_falls_back_to_unbound() -> None:
    engine = _make_engine()
    engine._orch_session_id = ""
    assert engine._llm_logs_dir() == Path("/tmp/llm_requests/unbound")


def test_clear_llm_request_logs_noop_when_dir_missing(tmp_path: Path) -> None:
    engine = _make_engine()
    engine._workspace_layout = SimpleNamespace(llm_requests_dir=tmp_path / "nope")
    engine._clear_llm_request_logs()  # must not raise


def test_clear_llm_request_logs_removes_files_and_dirs(tmp_path: Path) -> None:
    engine = _make_engine()
    logs_dir = tmp_path / "llm_requests" / "sess-1"
    logs_dir.mkdir(parents=True)
    (logs_dir / "req1.json").write_text("{}")
    (logs_dir / "subdir").mkdir()
    (logs_dir / "subdir" / "nested.json").write_text("{}")
    engine._workspace_layout = SimpleNamespace(llm_requests_dir=tmp_path / "llm_requests")

    engine._clear_llm_request_logs()

    assert list(logs_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# _entry_agent_name / _entry_capability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("problem_solving", "problem_solver"),
        ("judge", "judge"),
        ("guided", "guide"),
        ("anything_else", "guide"),
    ],
)
def test_entry_agent_name(mode: str, expected: str) -> None:
    engine = _make_engine()
    engine._session.workflow_mode = mode
    assert engine._entry_agent_name() == expected


def test_entry_capability_reads_registry() -> None:
    engine = _make_engine()
    engine._session.workflow_mode = "guided"
    engine._registry = SimpleNamespace(get=lambda name: SimpleNamespace(capability="high"))
    assert engine._entry_capability() == "high"


def test_entry_capability_defaults_to_medium_on_error() -> None:
    engine = _make_engine()
    engine._session.workflow_mode = "guided"

    def _raise(name: str):
        raise RuntimeError("not registered")

    engine._registry = SimpleNamespace(get=_raise)
    assert engine._entry_capability() == "medium"


# ---------------------------------------------------------------------------
# _run_silent_return_turn
# ---------------------------------------------------------------------------


async def test_run_silent_return_turn_captures_text_and_result() -> None:
    events = [
        TokenDelta(text="hello "),
        TokenDelta(text="world"),
        ToolCallEvent(
            tool_use_id="tu_1", tool_name="return_result", tool_input={"result": {"summary": "x"}}
        ),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    gateway = _FakeGateway([events])
    engine = _make_engine(gateway=gateway)
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())

    result, text = await engine._run_silent_return_turn(
        _ROUTING, SimpleNamespace(), "model-x", agent, [Message(role="user", content="hi")]
    )

    assert result == {"summary": "x"}
    assert text == "hello world"
    assert engine._emitters.cost_only_calls == 1


async def test_run_silent_return_turn_ignores_non_return_result_tool_calls() -> None:
    events = [
        ToolCallEvent(tool_use_id="tu_1", tool_name="other_tool", tool_input={"result": {"x": 1}}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    engine = _make_engine(gateway=_FakeGateway([events]))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())

    result, text = await engine._run_silent_return_turn(
        _ROUTING, SimpleNamespace(), "model-x", agent, []
    )

    assert result is None
    assert text == ""


async def test_run_silent_return_turn_ignores_non_dict_result_payload() -> None:
    events = [
        ToolCallEvent(
            tool_use_id="tu_1", tool_name="return_result", tool_input={"result": "not-a-dict"}
        ),
    ]
    engine = _make_engine(gateway=_FakeGateway([events]))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())

    result, _text = await engine._run_silent_return_turn(
        _ROUTING, SimpleNamespace(), "model-x", agent, []
    )
    assert result is None


async def test_run_silent_return_turn_no_turn_end_skips_cost() -> None:
    engine = _make_engine(gateway=_FakeGateway([[TokenDelta(text="hi")]]))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())

    await engine._run_silent_return_turn(_ROUTING, SimpleNamespace(), "model-x", agent, [])

    assert engine._emitters.cost_only_calls == 0
    assert engine._emitters.cost_total == 0.0


# ---------------------------------------------------------------------------
# _run_silent_tool_loop_turn
# ---------------------------------------------------------------------------


class _FakeToolDispatcher:
    def __init__(
        self, *, stop_after: int | None = None, returned_output: dict[str, object] | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, object], str]] = []
        self._stop_after = stop_after
        self.returned_output = returned_output
        self.stop_requested = False

    async def dispatch(
        self, name: str, tool_input: dict[str, object], tool_use_id: str, recovered: bool = False
    ) -> str:
        self.calls.append((name, tool_input, tool_use_id))
        if self._stop_after is not None and len(self.calls) >= self._stop_after:
            self.stop_requested = True
        if name == "return_result":
            self.returned_output = dict(tool_input.get("result", {}))
        return "ok"


async def test_silent_tool_loop_turn_stops_when_dispatcher_flags_stop() -> None:
    events = [
        ToolCallEvent(tool_use_id="tu_1", tool_name="query_search_engine", tool_input={"q": "x"}),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    gateway = _FakeGateway([events])
    engine = _make_engine(gateway=gateway)
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(stop_after=1, returned_output={"themes": ["a"]})

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=_FAR_FUTURE_DEADLINE,
    )

    assert result == {"themes": ["a"]}
    assert dispatcher.calls == [("query_search_engine", {"q": "x"}, "tu_1")]


async def test_silent_tool_loop_turn_no_tool_calls_nudges_and_calls_on_round_text() -> None:
    events = [
        [TokenDelta(text="thinking out loud"), TurnEnd(usage=_usage(), stop_reason="end_turn")],
        [
            ToolCallEvent(
                tool_use_id="tu_1", tool_name="return_result", tool_input={"result": {"done": True}}
            ),
            TurnEnd(usage=_usage(), stop_reason="tool_use"),
        ],
    ]
    gateway = _FakeGateway(events)
    engine = _make_engine(gateway=gateway)
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(stop_after=1)

    narrated: list[str] = []

    async def _on_round_text(text: str) -> None:
        narrated.append(text)

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=_FAR_FUTURE_DEADLINE,
        on_round_text=_on_round_text,
    )

    assert narrated == ["thinking out loud"]
    assert result == {"done": True}


async def test_silent_tool_loop_turn_breaks_on_deadline_when_no_tool_calls() -> None:
    events = [[TokenDelta(text="stalling")]]
    engine = _make_engine(gateway=_FakeGateway(events))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(returned_output=None)

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=0.0,
        max_rounds=5,
    )

    # Deadline already passed -> breaks after round 1, then forced final turn
    # (which produced no events here) leaves the output as whatever the
    # dispatcher already had.
    assert result is None
    # Two stream_query calls: the one nudge round + the final forced turn.
    assert len(engine._gateway.calls) == 2


async def test_silent_tool_loop_turn_exhausts_max_rounds() -> None:
    # Every round makes a tool call but never stops and never crosses the
    # deadline, so the loop must fall through after max_rounds and force a
    # final turn.
    def _round_events(i: int) -> list[object]:
        return [
            ToolCallEvent(tool_use_id=f"tu_{i}", tool_name="noop_tool", tool_input={}),
            TurnEnd(usage=_usage(), stop_reason="tool_use"),
        ]

    events = [_round_events(i) for i in range(3)] + [[]]  # 3 rounds + final forced turn
    engine = _make_engine(gateway=_FakeGateway(events))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(returned_output=None)

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=_FAR_FUTURE_DEADLINE,
        max_rounds=3,
    )

    assert result is None
    assert len(dispatcher.calls) == 3
    assert len(engine._gateway.calls) == 4  # 3 rounds + 1 final forced turn


async def test_silent_tool_loop_turn_final_forced_turn_dispatches_return_result_only() -> None:
    final_events = [
        ToolCallEvent(tool_use_id="tu_x", tool_name="other_tool", tool_input={}),
        ToolCallEvent(
            tool_use_id="tu_y", tool_name="return_result", tool_input={"result": {"ok": True}}
        ),
        TurnEnd(usage=_usage(), stop_reason="tool_use"),
    ]
    gateway = _FakeGateway([[TokenDelta(text="x")], final_events])
    engine = _make_engine(gateway=gateway)
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(returned_output=None)

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING, SimpleNamespace(), "model-x", agent, [], dispatcher, deadline=0.0
    )

    # Only return_result was dispatched in the final round, not other_tool.
    assert dispatcher.calls == [("return_result", {"result": {"ok": True}}, "tu_y")]
    assert result == {"ok": True}


async def test_silent_tool_loop_turn_max_rounds_zero_returns_immediately_if_already_stopped() -> (
    None
):
    engine = _make_engine(gateway=_FakeGateway([]))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(returned_output={"already": "done"})
    dispatcher.stop_requested = True

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=_FAR_FUTURE_DEADLINE,
        max_rounds=0,
    )

    assert result == {"already": "done"}
    assert engine._gateway.calls == []  # no round ever ran


async def test_silent_tool_loop_turn_breaks_on_deadline_after_tool_dispatch_without_stop() -> None:
    events = [
        [
            ToolCallEvent(tool_use_id="tu_1", tool_name="query_search_engine", tool_input={}),
            TurnEnd(usage=_usage(), stop_reason="tool_use"),
        ],
        [],  # final forced turn
    ]
    engine = _make_engine(gateway=_FakeGateway(events))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(returned_output=None)  # never sets stop_requested

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING, SimpleNamespace(), "model-x", agent, [], dispatcher, deadline=0.0
    )

    assert result is None
    assert len(dispatcher.calls) == 1  # one round's dispatch, then broke on deadline
    assert len(engine._gateway.calls) == 2  # the round + the final forced turn


async def test_silent_tool_loop_turn_round_with_text_and_tool_calls_appends_text_block() -> None:
    events = [
        [
            TokenDelta(text="checking that for you"),
            ToolCallEvent(tool_use_id="tu_1", tool_name="query_search_engine", tool_input={}),
            TurnEnd(usage=_usage(), stop_reason="tool_use"),
        ]
    ]
    engine = _make_engine(gateway=_FakeGateway(events))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(stop_after=1, returned_output={"ok": True})

    narrated: list[str] = []

    async def _on_round_text(text: str) -> None:
        narrated.append(text)

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=_FAR_FUTURE_DEADLINE,
        on_round_text=_on_round_text,
    )

    assert narrated == ["checking that for you"]
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# ThinkingSignature / ThinkingDelta flow through the tool loop's assistant block
# ---------------------------------------------------------------------------


async def test_silent_tool_loop_turn_carries_thinking_signature_into_assistant_block() -> None:
    events = [
        [
            ThinkingDelta(text="pondering "),
            ThinkingDelta(text="deeply"),
            ThinkingSignature(signature="sig-123"),
            ToolCallEvent(
                tool_use_id="tu_1", tool_name="return_result", tool_input={"result": {"ok": True}}
            ),
            TurnEnd(usage=_usage(), stop_reason="tool_use"),
        ]
    ]
    engine = _make_engine(gateway=_FakeGateway(events))
    agent = SimpleNamespace(system_prompt="sys", tools=frozenset())
    dispatcher = _FakeToolDispatcher(stop_after=1)

    result = await engine._run_silent_tool_loop_turn(
        _ROUTING,
        SimpleNamespace(),
        "model-x",
        agent,
        [],
        dispatcher,
        deadline=_FAR_FUTURE_DEADLINE,
    )

    assert result == {"ok": True}
