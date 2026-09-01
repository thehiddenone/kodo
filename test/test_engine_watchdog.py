"""Tests for ``kodo.runtime._engine._watchdog`` — the stuck-agent watchdog
(doc/STUCK_DETECTION.md).

Three layers, tested separately then together:

- Pure detector functions (``detect_red_flags``) and settings parsing
  (``_stuck_settings``) — no engine needed.
- The ``on_stall`` closure built by ``_make_stall_handler``, exercised
  directly against a fake engine for each remediation path (immediate,
  entry-agent deferred, sub-agent inline gate, the stall-count cap).
- End-to-end through the real ``_run_agent_turn`` loop with a fake gateway
  that actually emits a stuck (empty-final-turn) response, proving the
  watchdog is reachable from the real turn loop and not just callable in
  isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

from kodo.llms import (
    LLMRouting,
    Message,
    ThinkingDelta,
    TokenDelta,
    ToolCallArgDelta,
    TurnEnd,
    Usage,
)
from kodo.runtime import WorkflowEngine
from kodo.runtime._engine import _watchdog
from kodo.runtime._engine._watchdog import (
    _MAX_CONSECUTIVE_NUDGES,
    TurnSignal,
    _stuck_settings,
    detect_red_flags,
)
from kodo.runtime._gates import StuckAlertResponse
from kodo.runtime._session import SessionState

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGateway:
    def __init__(self, batches: list[list[object]]) -> None:
        self._batches = list(batches)
        self.calls: list[dict[str, object]] = []
        # How many scripted events were actually yielded per call -- lets a
        # test prove an early `break` (cyclic-thinking abort) really stopped
        # consuming the rest of a batch, not just that cancel() was called.
        self.consumed: list[int] = []

    async def stream_query(self, **kwargs: object):
        self.calls.append(kwargs)
        batch = self._batches.pop(0) if self._batches else []
        count = 0
        self.consumed.append(count)
        for event in batch:
            count += 1
            self.consumed[-1] = count
            yield event


class _FakeLLM:
    """Minimal LLMPlugin double with a real, trackable cancel()."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.cancel_calls: list[str] = []

    async def cancel(self, stream_id: str) -> None:
        self.cancel_calls.append(stream_id)


class _FakeEmitters:
    def __init__(self) -> None:
        self.nudges: list[tuple[str, list[str], str, str]] = []
        self.critical_messages: list[str] = []
        self.cyclic_critical_messages: list[str] = []
        self.think_in_tool_call_critical_messages: list[str] = []
        self.tool_call_cyclic_critical_messages: list[str] = []
        self.cost_total = 0.0
        self.token_usages: list[object] = []

    async def handle_stream_event(self, event: object, stream_id: str) -> None:
        pass

    def add_cost(self, usd: float) -> None:
        self.cost_total += usd

    def add_tokens_from_usage(self, usage: object) -> None:
        self.token_usages.append(usage)

    @property
    def cumulative_usd(self) -> float:
        return self.cost_total

    async def emit_usage(
        self, turn_end: object, model: str, duration: float, agent_name: str
    ) -> None:
        pass

    async def emit_context_stats(self) -> None:
        pass

    async def emit_agent_started(self, name: str) -> None:
        pass

    async def emit_agent_finished(self, name: str) -> None:
        pass

    async def emit_nudge(self, ui_text: str, reasons: list[str], mode: str, source: str) -> None:
        self.nudges.append((ui_text, reasons, mode, source))

    async def emit_agent_stuck_critical(self, message: str) -> None:
        self.critical_messages.append(message)

    async def emit_cyclic_thinking_critical(self, message: str) -> None:
        self.cyclic_critical_messages.append(message)

    async def emit_think_in_tool_call_critical(self, message: str) -> None:
        self.think_in_tool_call_critical_messages.append(message)

    async def emit_tool_call_cyclic_critical(self, message: str) -> None:
        self.tool_call_cyclic_critical_messages.append(message)


_AppendedEntry = tuple[str, object, str | None, str | None, "dict[str, object] | None"]
_AppendedSubEntry = tuple[str, str, object, str | None, "dict[str, object] | None"]


class _FakeTransient:
    def __init__(self) -> None:
        self.appended: list[_AppendedEntry] = []
        self.appended_sub: list[_AppendedSubEntry] = []

    def append_message(
        self, role, content, entry_agent=None, attachments=None, kind=None, detail=None
    ) -> None:
        self.appended.append((role, content, entry_agent, kind, detail))

    def append_subsession_message(
        self, subsession_id, role, content, kind=None, detail=None
    ) -> None:
        self.appended_sub.append((subsession_id, role, content, kind, detail))


class _FakeGate:
    """Records every ``fire_stuck_alert`` call; returns a scripted sequence of answers."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self._answers = list(answers) if answers is not None else []
        self.calls: list[dict[str, object]] = []

    async def fire_stuck_alert(
        self, *, agent_name: str, display_name: str, reasons: list[str]
    ) -> StuckAlertResponse:
        self.calls.append(
            {"agent_name": agent_name, "display_name": display_name, "reasons": reasons}
        )
        action = self._answers.pop(0) if self._answers else "dismiss"
        return StuckAlertResponse(action=action)


class _FakeDispatcher:
    """Minimal ToolDispatcher double — only ``returned_output`` is read."""

    def __init__(self, returned_output: dict[str, object] | None = None) -> None:
        self.returned_output = returned_output


class _FakeRegistry:
    def get(self, name: str, autonomous: bool = False):
        return SimpleNamespace(display_name=name.replace("_", " ").title())

    def run_subagent_specs(self, caller: str) -> list[object]:
        return []

    def return_result_specs(self, name: str) -> list[object]:
        return []


def _usage() -> Usage:
    return Usage(
        input_tokens=10, output_tokens=5, cache_write_tokens=0, cache_read_tokens=0, model="m"
    )


def _watchdog_engine(
    *,
    autonomous: bool = False,
    settings: dict[str, object] | None = None,
    gate_answers: list[str] | None = None,
    gateway: _FakeGateway | None = None,
) -> WorkflowEngine:
    engine = object.__new__(WorkflowEngine)
    engine._orch_session_id = "s1"
    engine._registry = _FakeRegistry()
    engine._session = SessionState(session_id="s1")
    engine._session.effective_autonomous = autonomous
    engine._session.phase = "awaiting_user"
    engine._transient = _FakeTransient()
    engine._emitters = _FakeEmitters()
    engine._gate = _FakeGate(gate_answers)
    engine._queue = asyncio.Queue()
    engine._entry_turn_seq = 0
    engine._stuck_watchdog_task = None
    engine._stuck_streak = False
    engine._cycle_streak = False
    engine._think_tag_streak = False
    engine._tool_call_cycle_streak = False
    default_settings: dict[str, object] = {
        "stuck_detection": {
            "active": "local_only",
            "scope": "top_level_and_subagents",
            "auto_unstuck_interactive": False,
        }
    }
    resolved_settings = settings if settings is not None else default_settings
    engine._get_settings = lambda: resolved_settings
    engine._gateway = gateway or _FakeGateway([])
    engine._sink = SimpleNamespace(send=_noop_async)

    def _llm_logs_dir() -> Path:
        return Path("/tmp/llm_logs")

    engine._llm_logs_dir = _llm_logs_dir
    return engine


async def _noop_async(*args: object, **kwargs: object) -> None:
    pass


_LOCAL_ROUTING = LLMRouting(residence="local")
_CLOUD_ROUTING = LLMRouting(residence="cloud")


# ---------------------------------------------------------------------------
# detect_red_flags — pure detector functions
# ---------------------------------------------------------------------------


def test_detect_red_flags_healthy_turn_has_none() -> None:
    signal = TurnSignal(
        text="all done, see summary above", thinking_text="", stop_reason="end_turn"
    )
    assert detect_red_flags(signal) == []


def test_detect_red_flags_empty_final_turn() -> None:
    # Empty text is also zero words once punctuation-stripped, so this now
    # trips terse_final_response too — both detectors independently agree an
    # empty turn is a stall.
    signal = TurnSignal(text="", thinking_text="hmm let me check", stop_reason="end_turn")
    flags = detect_red_flags(signal)
    assert {f.code for f in flags} == {"empty_final_turn", "terse_final_response"}


def test_detect_red_flags_whitespace_only_text_still_counts_as_empty() -> None:
    signal = TurnSignal(text="   \n\t", thinking_text="", stop_reason="end_turn")
    flags = detect_red_flags(signal)
    assert {f.code for f in flags} == {"empty_final_turn", "terse_final_response"}


def test_detect_red_flags_truncated_generation() -> None:
    signal = TurnSignal(
        text="I was about to explain the fix", thinking_text="", stop_reason="max_tokens"
    )
    flags = detect_red_flags(signal)
    assert [f.code for f in flags] == ["truncated_generation"]


def test_detect_red_flags_all_three_can_fire_together() -> None:
    signal = TurnSignal(text="", thinking_text="", stop_reason="max_tokens")
    flags = detect_red_flags(signal)
    assert {f.code for f in flags} == {
        "empty_final_turn",
        "truncated_generation",
        "terse_final_response",
    }


def test_detect_red_flags_terse_final_response() -> None:
    signal = TurnSignal(text="Done.", thinking_text="", stop_reason="end_turn")
    flags = detect_red_flags(signal)
    assert [f.code for f in flags] == ["terse_final_response"]


def test_detect_red_flags_terse_final_response_strips_a_standalone_punctuation_token() -> None:
    # Without stripping punctuation first, "Done !" would naively split into
    # two whitespace-separated tokens ("Done", "!"); stripping punctuation
    # first removes the standalone "!" so this still reads as one real word.
    signal = TurnSignal(text="Done !", thinking_text="", stop_reason="end_turn")
    flags = detect_red_flags(signal)
    assert [f.code for f in flags] == ["terse_final_response"]


def test_detect_red_flags_terse_final_response_pure_punctuation_counts_as_zero_words() -> None:
    # Punctuation-only text strips down to zero words, which is still "at
    # most one" and counts as terse — this also happens to trip
    # empty_final_turn, since text.strip() here is non-empty but the text
    # has no real content either way.
    signal = TurnSignal(text="...", thinking_text="", stop_reason="end_turn")
    flags = detect_red_flags(signal)
    assert "terse_final_response" in {f.code for f in flags}


def test_detect_red_flags_two_words_is_acceptable() -> None:
    signal = TurnSignal(text="Sounds good.", thinking_text="", stop_reason="end_turn")
    assert detect_red_flags(signal) == []


def test_detect_red_flags_multi_word_response_is_not_terse() -> None:
    signal = TurnSignal(
        text="Finished the benchmark and wrote the report",
        thinking_text="",
        stop_reason="end_turn",
    )
    assert detect_red_flags(signal) == []


# ---------------------------------------------------------------------------
# _stuck_settings — defensive parsing
# ---------------------------------------------------------------------------


def test_stuck_settings_defaults_on_missing_block() -> None:
    cfg = _stuck_settings({})
    assert cfg.active == "local_only"
    assert cfg.scope == "top_level_and_subagents"
    assert cfg.auto_unstuck_interactive is False


def test_stuck_settings_invalid_values_fall_back_to_defaults() -> None:
    cfg = _stuck_settings({"stuck_detection": {"active": "yolo", "scope": "everything"}})
    assert cfg.active == "local_only"
    assert cfg.scope == "top_level_and_subagents"


def test_stuck_settings_applies_off_never_applies() -> None:
    cfg = _stuck_settings({"stuck_detection": {"active": "off"}})
    assert cfg.applies(residence="local", is_entry_turn=True) is False
    assert cfg.applies(residence="cloud", is_entry_turn=True) is False


def test_stuck_settings_applies_local_only_gates_by_residence() -> None:
    cfg = _stuck_settings({"stuck_detection": {"active": "local_only"}})
    assert cfg.applies(residence="local", is_entry_turn=True) is True
    assert cfg.applies(residence="cloud", is_entry_turn=True) is False


def test_stuck_settings_applies_local_and_cloud_covers_both() -> None:
    cfg = _stuck_settings({"stuck_detection": {"active": "local_and_cloud"}})
    assert cfg.applies(residence="local", is_entry_turn=True) is True
    assert cfg.applies(residence="cloud", is_entry_turn=True) is True


def test_stuck_settings_scope_gates_subagent_turns() -> None:
    cfg = _stuck_settings({"stuck_detection": {"active": "local_only", "scope": "top_level"}})
    assert cfg.applies(residence="local", is_entry_turn=True) is True
    assert cfg.applies(residence="local", is_entry_turn=False) is False

    cfg2 = _stuck_settings(
        {"stuck_detection": {"active": "local_only", "scope": "top_level_and_subagents"}}
    )
    assert cfg2.applies(residence="local", is_entry_turn=False) is True


# ---------------------------------------------------------------------------
# _make_stall_handler — the on_stall closure, exercised directly
# ---------------------------------------------------------------------------


async def test_on_stall_no_flags_is_a_pure_noop() -> None:
    """A healthy turn never touches settings, the registry, the gate, or persistence."""
    engine = _watchdog_engine()
    del engine._registry  # proves display_name is never resolved on this path
    handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    decision = await handler(
        TurnSignal(text="all done, see summary above", thinking_text="", stop_reason="end_turn")
    )

    assert decision.retry is False
    assert engine._transient.appended == []
    assert engine._stuck_watchdog_task is None


async def test_on_stall_settings_off_suppresses_a_real_stall() -> None:
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "off"}},
    )
    handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    assert decision.retry is False
    assert engine._transient.appended == []


async def test_on_stall_local_only_ignores_cloud_residence() -> None:
    engine = _watchdog_engine(autonomous=True)  # default active="local_only"
    handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_CLOUD_ROUTING, is_entry_turn=True
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    assert decision.retry is False


async def test_on_stall_autonomous_nudges_immediately_and_persists() -> None:
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    assert decision.retry is True
    assert decision.message is not None
    assert decision.message.role == "user"
    assert "continue" in decision.message.content.lower()
    # Persisted as a main-session message, tagged so the feed renders it
    # specially instead of as a fake user-typed bubble.
    assert len(engine._transient.appended) == 1
    role, content, entry_agent, kind, detail = engine._transient.appended[0]
    assert (role, entry_agent, kind) == ("user", "problem_solver", "nudge")
    assert detail is not None
    assert detail["mode"] == "auto"
    assert detail["source"] == "stall"
    assert detail["reasons"] == ["empty_final_turn", "terse_final_response"]
    # No local echo on the client, so the live event carries the explanation.
    assert engine._emitters.nudges == [
        (detail["ui_text"], ["empty_final_turn", "terse_final_response"], "auto", "stall")
    ]
    # Immediate path never touches the gate.
    assert engine._gate.calls == []


async def test_on_stall_interactive_auto_unstuck_also_nudges_immediately() -> None:
    engine = _watchdog_engine(
        autonomous=False,
        settings={
            "stuck_detection": {
                "active": "local_only",
                "scope": "top_level",
                "auto_unstuck_interactive": True,
            }
        },
    )
    handler = engine._make_stall_handler(
        agent_name="guide", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="max_tokens"))

    assert decision.retry is True
    assert engine._gate.calls == []
    assert engine._stuck_watchdog_task is None


async def test_on_stall_interactive_entry_turn_schedules_deferred_alarm() -> None:
    """Not auto: the turn ends normally now; remediation is a decoupled
    follow-up. Uses the real 1s delay deliberately (unlike the sibling tests
    below) to prove scheduling itself doesn't block the caller or fire the
    gate early — cancelled immediately after, so this stays fast."""
    engine = _watchdog_engine(autonomous=False, gate_answers=["unstick"])
    handler = engine._make_stall_handler(
        agent_name="guide", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    # The caller (_run_agent_turn) ends the turn exactly as if nothing happened...
    assert decision.retry is False
    assert engine._transient.appended == []
    assert engine._gate.calls == []
    # ...but a background watcher is now pinned to this turn, still asleep.
    assert engine._stuck_watchdog_task is not None
    assert not engine._stuck_watchdog_task.done()
    await asyncio.sleep(0)
    assert engine._gate.calls == []  # still waiting out the grace period

    engine._stuck_watchdog_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await engine._stuck_watchdog_task


async def test_on_stall_interactive_entry_turn_alarm_fires_and_unsticks(monkeypatch) -> None:
    monkeypatch.setattr(_watchdog, "_ENTRY_TURN_ALARM_DELAY_S", 0.01)
    engine = _watchdog_engine(autonomous=False, gate_answers=["unstick"])
    handler = engine._make_stall_handler(
        agent_name="guide", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))
    assert decision.retry is False
    assert engine._stuck_watchdog_task is not None

    await engine._stuck_watchdog_task

    assert len(engine._gate.calls) == 1
    assert engine._gate.calls[0]["reasons"] == [
        "its last turn ended with no tool call and no visible response",
        "its last response was at most one word, not a real completion",
    ]
    # "unstick" re-enters through the normal worker queue, tagged as a nudge.
    assert engine._queue.qsize() == 1
    task = engine._queue.get_nowait()
    assert task["nudge_detail"]["mode"] == "manual"
    assert "continue" in task["text"].lower()


async def test_on_stall_interactive_entry_turn_alarm_dismissed_queues_nothing(monkeypatch) -> None:
    monkeypatch.setattr(_watchdog, "_ENTRY_TURN_ALARM_DELAY_S", 0.01)
    engine = _watchdog_engine(autonomous=False, gate_answers=["dismiss"])
    handler = engine._make_stall_handler(
        agent_name="guide", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))
    await engine._stuck_watchdog_task

    assert len(engine._gate.calls) == 1
    assert engine._queue.qsize() == 0


async def test_on_stall_entry_turn_alarm_honors_a_new_turn_during_the_grace_period(
    monkeypatch,
) -> None:
    """Q: if a new turn starts while the 1s grace period is running, does the
    watchdog honor that and skip the stale alarm? Yes — _entry_turn_seq is
    bumped once per new entry-agent turn (_run_entry_agent/_resume_main_turn),
    and the watcher re-checks it (and session.phase) right after waking, both
    before firing the gate and again after it resolves."""
    monkeypatch.setattr(_watchdog, "_ENTRY_TURN_ALARM_DELAY_S", 0.01)
    engine = _watchdog_engine(autonomous=False, gate_answers=["unstick"])
    handler = engine._make_stall_handler(
        agent_name="guide", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))
    assert engine._stuck_watchdog_task is not None

    # Simulate a brand-new prompt starting (and, for good measure, finishing)
    # before the watcher's 1s nap is over — exactly what _run_entry_agent does
    # at the top of every call.
    engine._entry_turn_seq += 1

    await engine._stuck_watchdog_task

    # The stale watcher must not alarm about a turn the user already moved
    # past: no gate call, nothing queued.
    assert engine._gate.calls == []
    assert engine._queue.qsize() == 0


async def test_on_stall_entry_turn_alarm_honors_phase_no_longer_idle(monkeypatch) -> None:
    """Same guarantee, via the session.phase half of the check: a turn that is
    still running when the watcher wakes (not just one that already finished)
    also suppresses the stale alarm."""
    monkeypatch.setattr(_watchdog, "_ENTRY_TURN_ALARM_DELAY_S", 0.01)
    engine = _watchdog_engine(autonomous=False, gate_answers=["unstick"])
    handler = engine._make_stall_handler(
        agent_name="guide", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))
    engine._session.phase = "running"

    await engine._stuck_watchdog_task

    assert engine._gate.calls == []
    assert engine._queue.qsize() == 0


async def test_on_stall_subagent_scope_asks_inline_no_delay() -> None:
    engine = _watchdog_engine(
        autonomous=False,
        gate_answers=["unstick"],
        settings={
            "stuck_detection": {
                "active": "local_only",
                "scope": "top_level_and_subagents",
                "auto_unstuck_interactive": False,
            }
        },
    )
    handler = engine._make_stall_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    # No background task — the sub-agent's own turn awaited the gate directly.
    assert engine._stuck_watchdog_task is None
    assert len(engine._gate.calls) == 1
    assert decision.retry is True
    assert len(engine._transient.appended_sub) == 1
    subsession_id, role, content, kind, detail = engine._transient.appended_sub[0]
    assert (subsession_id, role, kind) == ("sub-1", "user", "nudge")
    assert detail["mode"] == "manual"


async def test_on_stall_subagent_scope_dismiss_does_not_retry() -> None:
    engine = _watchdog_engine(
        autonomous=False,
        gate_answers=["dismiss"],
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level_and_subagents"}},
    )
    handler = engine._make_stall_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    assert decision.retry is False
    assert engine._transient.appended_sub == []


async def test_on_stall_subagent_scope_included_by_default() -> None:
    """Default scope ("top_level_and_subagents") watches sub-agent turns too
    — changed 2026-08-31 after a sub-agent's return_result call ballooned a
    tool-call argument into 1000+ repeated elements with nothing watching
    that turn at all (doc/STUCK_DETECTION.md §2.2/§2.10)."""
    engine = _watchdog_engine(autonomous=True)  # default settings, no override
    handler = engine._make_stall_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    assert decision.retry is True
    assert len(engine._transient.appended_sub) == 1


async def test_on_stall_subagent_scope_excluded_when_scope_set_to_top_level() -> None:
    """Explicit scope="top_level" opts back out of watching sub-agents (the
    default is "top_level_and_subagents" — see the scope-inclusion test
    above)."""
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level"}},
    )
    handler = engine._make_stall_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )

    decision = await handler(TurnSignal(text="", thinking_text="", stop_reason="end_turn"))

    assert decision.retry is False
    assert engine._gate.calls == []


async def test_on_stall_entry_turn_streak_escalates_to_critical_after_one_nudge() -> None:
    """Entry-agent scope gets exactly one nudge per streak (doc/STUCK_DETECTION.md
    §2.4a): a second consecutive stall right after goes critical instead of
    nudging (or retrying) again — and stays critical on a third, since only a
    genuine response clears the streak."""
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    signal = TurnSignal(text="", thinking_text="", stop_reason="end_turn")

    decisions = [await handler(signal) for _ in range(3)]

    assert [d.retry for d in decisions] == [True, False, False]
    assert len(engine._transient.appended) == 1  # exactly one nudge ever persisted
    assert len(engine._emitters.critical_messages) == 2  # 2nd and 3rd stall both go critical
    assert engine._stuck_streak is True  # never cleared except by a real response


async def test_on_stall_entry_turn_streak_clears_on_a_genuine_response() -> None:
    """ "get stuck -> good response -> get stuck" nudges both times: a
    non-stalled round in between clears the streak."""
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    stalled = TurnSignal(text="", thinking_text="", stop_reason="end_turn")
    healthy = TurnSignal(
        text="all done, see summary above", thinking_text="", stop_reason="end_turn"
    )

    first = await handler(stalled)
    recovered = await handler(healthy)
    second = await handler(stalled)

    assert [first.retry, recovered.retry, second.retry] == [True, False, True]
    assert len(engine._transient.appended) == 2  # nudged both times
    assert engine._emitters.critical_messages == []


async def test_on_stall_entry_turn_streak_clears_on_a_successful_tool_call_round() -> None:
    """ "get stuck -> successful tool call -> get stuck" nudges both times too:
    a productive round in between (no on_stall call at all — _run_agent_turn
    only invokes on_stall when a round has *no* tool calls) still needs to
    clear the streak via _make_progress_handler, or an unrelated later stall
    escalates straight to critical despite the agent having made real
    progress in between (the exact bug traced in session 1784487585:
    read_file/create_file all succeeded between two stalls, yet the second
    stall went critical because nothing had cleared _stuck_streak)."""
    engine = _watchdog_engine(autonomous=True)
    stall_handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    progress_handler = engine._make_progress_handler(is_entry_turn=True)
    assert progress_handler is not None
    stalled = TurnSignal(text="", thinking_text="", stop_reason="end_turn")

    first = await stall_handler(stalled)
    progress_handler()  # a round with tool calls — _run_agent_turn's on_tool_calls hook
    second = await stall_handler(stalled)

    assert [first.retry, second.retry] == [True, True]
    assert len(engine._transient.appended) == 2  # nudged both times
    assert engine._emitters.critical_messages == []


async def test_make_progress_handler_is_a_noop_for_subagent_scope() -> None:
    """_stuck_streak is entry-agent-only (doc/STUCK_DETECTION.md §2.4a) — a
    sub-agent turn has no cross-turn streak to clear, so the hook is simply
    absent rather than a callable that does nothing."""
    engine = _watchdog_engine()
    assert engine._make_progress_handler(is_entry_turn=False) is None


async def test_on_stall_subagent_stall_count_cap_gives_up_after_max_consecutive_nudges() -> None:
    """Sub-agent scope is unchanged by the entry-agent streak/critical logic:
    still capped at _MAX_CONSECUTIVE_NUDGES inline retries, no critical notice."""
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level_and_subagents"}},
    )
    handler = engine._make_stall_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )
    signal = TurnSignal(text="", thinking_text="", stop_reason="end_turn")

    decisions = [await handler(signal) for _ in range(_MAX_CONSECUTIVE_NUDGES + 1)]

    assert [d.retry for d in decisions] == [True] * _MAX_CONSECUTIVE_NUDGES + [False]
    assert len(engine._transient.appended_sub) == _MAX_CONSECUTIVE_NUDGES
    assert engine._emitters.critical_messages == []


# ---------------------------------------------------------------------------
# Missing-`return_result` enforcement (doc/STUCK_DETECTION.md §2.8) — the
# toolchain_builder incident (session 1785719012): a clean, non-stalled final
# response that never called return_result at all.
# ---------------------------------------------------------------------------


async def test_on_stall_missing_return_result_nudges_once_then_fails() -> None:
    engine = _watchdog_engine()  # default settings, autonomous=False
    dispatcher = _FakeDispatcher(returned_output=None)
    handler = engine._make_stall_handler(
        agent_name="toolchain_builder",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
        dispatcher=dispatcher,
    )
    healthy = TurnSignal(
        text="All scripts pass and the project tree is complete.",
        thinking_text="",
        stop_reason="end_turn",
    )

    first = await handler(healthy)
    second = await handler(healthy)

    # Exactly one nudge, then the turn is allowed to actually end — it's up
    # to _drive_subsession's existing {schema_compliance: False} fallback to
    # surface this as a failed subsession from there.
    assert [first.retry, second.retry] == [True, False]
    assert len(engine._transient.appended_sub) == 1
    subsession_id, role, content, kind, detail = engine._transient.appended_sub[0]
    assert (subsession_id, role, kind) == ("sub-1", "user", "nudge")
    assert detail["reasons"] == ["missing_return_result"]
    assert detail["mode"] == "auto"
    assert "return_result" in content


async def test_on_stall_missing_return_result_is_independent_of_stuck_detection_settings() -> None:
    """Unlike every ordinary stall, this is a hard sub-agent contract, not a
    heuristic — it must still fire even with stuck_detection off entirely."""
    engine = _watchdog_engine(settings={"stuck_detection": {"active": "off"}})
    dispatcher = _FakeDispatcher(returned_output=None)
    handler = engine._make_stall_handler(
        agent_name="toolchain_builder",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
        dispatcher=dispatcher,
    )
    healthy = TurnSignal(text="All done.", thinking_text="", stop_reason="end_turn")

    decision = await handler(healthy)

    assert decision.retry is True
    assert len(engine._transient.appended_sub) == 1


async def test_on_stall_skips_missing_return_result_nudge_once_return_result_is_set() -> None:
    """A sub-agent that already called return_result never gets this nudge —
    proves the well-behaved case is untouched."""
    engine = _watchdog_engine()
    dispatcher = _FakeDispatcher(returned_output={"schema_compliance": True})
    handler = engine._make_stall_handler(
        agent_name="toolchain_builder",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
        dispatcher=dispatcher,
    )
    healthy = TurnSignal(
        text="All done, result submitted.", thinking_text="", stop_reason="end_turn"
    )

    decision = await handler(healthy)

    assert decision.retry is False
    assert engine._transient.appended_sub == []


async def test_on_stall_missing_return_result_also_gates_the_stall_count_cap() -> None:
    """The safety net applies uniformly: even after the generic stall-flag
    retries are exhausted, the sub-agent still gets its one dedicated
    return_result nudge before the turn is finally allowed to end."""
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level_and_subagents"}},
    )
    dispatcher = _FakeDispatcher(returned_output=None)
    handler = engine._make_stall_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
        dispatcher=dispatcher,
    )
    stalled = TurnSignal(text="", thinking_text="", stop_reason="end_turn")

    decisions = [await handler(stalled) for _ in range(_MAX_CONSECUTIVE_NUDGES + 2)]

    # _MAX_CONSECUTIVE_NUDGES generic stall retries, then one more dedicated
    # return_result retry, then the turn is finally allowed to end.
    assert [d.retry for d in decisions] == [True] * (_MAX_CONSECUTIVE_NUDGES + 1) + [False]
    all_reasons = [entry[4]["reasons"] for entry in engine._transient.appended_sub]
    generic_reasons = ["empty_final_turn", "terse_final_response"]
    assert all_reasons[:_MAX_CONSECUTIVE_NUDGES] == [generic_reasons] * _MAX_CONSECUTIVE_NUDGES
    assert all_reasons[_MAX_CONSECUTIVE_NUDGES] == ["missing_return_result"]


# ---------------------------------------------------------------------------
# _make_cyclic_thinking_handler — the on_cyclic_thinking closure (doc/STUCK_DETECTION.md §2.7)
# ---------------------------------------------------------------------------


def test_make_cyclic_thinking_handler_returns_none_when_settings_off() -> None:
    engine = _watchdog_engine(settings={"stuck_detection": {"active": "off"}})
    handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is None


def test_make_cyclic_thinking_handler_returns_none_for_cloud_when_local_only() -> None:
    engine = _watchdog_engine()  # default active="local_only"
    handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_CLOUD_ROUTING, is_entry_turn=True
    )
    assert handler is None


def test_make_cyclic_thinking_handler_returns_callable_for_subagent_by_default() -> None:
    """Default scope ("top_level_and_subagents") watches sub-agent turns too
    — changed 2026-08-31, see the matching stall-handler test above."""
    engine = _watchdog_engine(autonomous=True)  # default settings, no override
    handler = engine._make_cyclic_thinking_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )
    assert handler is not None


def test_make_cyclic_thinking_handler_returns_none_for_subagent_when_scope_is_top_level() -> None:
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level"}},
    )
    handler = engine._make_cyclic_thinking_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )
    assert handler is None


def test_make_cyclic_thinking_handler_returns_callable_when_enabled() -> None:
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is not None


async def test_cyclic_thinking_handler_entry_turn_strike_one_notices_and_sets_streak() -> None:
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is not None

    decision = await handler("some repeated thinking text")

    assert decision.retry is True
    assert decision.message is not None
    assert decision.message.role == "assistant"
    assert "repetitive" in decision.message.content.lower()
    assert engine._cycle_streak is True
    # Single artifact: the notice is both the LLM-visible retry message and
    # the one thing persisted (kind-tagged, so it round-trips as a <kodo_warn>
    # callout on replay -- doc/STUCK_DETECTION.md §2.7).
    assert len(engine._transient.appended) == 1
    role, content, entry_agent, kind, detail = engine._transient.appended[0]
    assert (role, content, entry_agent, kind) == (
        "assistant",
        decision.message.content,
        "problem_solver",
        "nudge",
    )
    assert detail["source"] == "cyclic_thinking"
    assert engine._emitters.nudges == [
        (decision.message.content, ["cyclic_thinking"], "auto", "cyclic_thinking")
    ]
    assert engine._emitters.cyclic_critical_messages == []


async def test_cyclic_thinking_handler_entry_turn_strike_two_goes_critical() -> None:
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is not None

    first = await handler("loop take 1")
    second = await handler("loop take 2")
    third = await handler("loop take 3")

    assert [first.retry, second.retry, third.retry] == [True, False, False]
    assert len(engine._transient.appended) == 1  # exactly one notice ever persisted
    # Stays critical on a third hit too -- only genuine progress clears the streak.
    assert len(engine._emitters.cyclic_critical_messages) == 2
    assert engine._cycle_streak is True


async def test_cyclic_streak_is_dedicated_from_ordinary_stuck_streak() -> None:
    """An ordinary stall and a cyclic-thinking hit must not combine to trip
    either escalation's two-strike cap -- each gets its own streak."""
    engine = _watchdog_engine(autonomous=True)
    stall_handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    cyclic_handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert cyclic_handler is not None

    stall_decision = await stall_handler(
        TurnSignal(text="", thinking_text="", stop_reason="end_turn")
    )
    cyclic_decision = await cyclic_handler("loop text")

    assert stall_decision.retry is True
    assert cyclic_decision.retry is True  # not already critical -- separate streak
    assert engine._stuck_streak is True
    assert engine._cycle_streak is True
    assert engine._emitters.critical_messages == []
    assert engine._emitters.cyclic_critical_messages == []


async def test_cyclic_streak_clears_on_a_genuine_response() -> None:
    engine = _watchdog_engine(autonomous=True)
    cyclic_handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    stall_handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert cyclic_handler is not None

    await cyclic_handler("loop text")
    assert engine._cycle_streak is True

    healthy = TurnSignal(
        text="all done, see summary above", thinking_text="", stop_reason="end_turn"
    )
    await stall_handler(healthy)

    assert engine._cycle_streak is False


async def test_cyclic_streak_clears_on_a_successful_tool_call_round() -> None:
    engine = _watchdog_engine(autonomous=True)
    cyclic_handler = engine._make_cyclic_thinking_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    progress_handler = engine._make_progress_handler(is_entry_turn=True)
    assert cyclic_handler is not None
    assert progress_handler is not None

    await cyclic_handler("loop text")
    assert engine._cycle_streak is True

    progress_handler()

    assert engine._cycle_streak is False


async def test_cyclic_thinking_handler_subagent_scope_capped_then_silent() -> None:
    """Mirrors the ordinary stall's sub-agent path: capped local counter,
    inline retry, silent end on the cap -- no critical banner at all."""
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level_and_subagents"}},
    )
    handler = engine._make_cyclic_thinking_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )
    assert handler is not None

    decisions = [await handler(f"loop {i}") for i in range(_MAX_CONSECUTIVE_NUDGES + 1)]

    assert [d.retry for d in decisions] == [True] * _MAX_CONSECUTIVE_NUDGES + [False]
    assert len(engine._transient.appended_sub) == _MAX_CONSECUTIVE_NUDGES
    assert engine._emitters.cyclic_critical_messages == []


# ---------------------------------------------------------------------------
# _make_think_in_tool_call_handler -- the on_think_in_tool_call closure
# (doc/STUCK_DETECTION.md §2.9)
# ---------------------------------------------------------------------------


def test_make_think_in_tool_call_handler_always_returns_a_callable() -> None:
    """Unlike every other closure in this module, settings never suppress
    this one -- a stray <think> tag in tool-call arguments is a hard
    protocol violation, not a stall heuristic."""
    engine = _watchdog_engine(settings={"stuck_detection": {"active": "off"}})
    handler = engine._make_think_in_tool_call_handler(
        agent_name="problem_solver", is_entry_turn=True
    )
    assert handler is not None


async def test_think_in_tool_call_handler_entry_turn_strike_one_nudges_and_names_the_tool() -> None:
    engine = _watchdog_engine()
    handler = engine._make_think_in_tool_call_handler(
        agent_name="problem_solver", is_entry_turn=True
    )

    decision = await handler("run_subagent")

    assert decision.retry is True
    assert decision.message is not None
    assert decision.message.role == "user"
    assert "run_subagent" in decision.message.content
    assert "not allowed to think inside a tool call" in decision.message.content
    assert engine._think_tag_streak is True
    role, content, entry_agent, kind, detail = engine._transient.appended[0]
    assert (role, entry_agent, kind) == ("user", "problem_solver", "nudge")
    assert detail["source"] == "think_in_tool_call"
    assert "run_subagent" in detail["ui_text"]


async def test_think_in_tool_call_handler_entry_turn_strike_two_goes_critical() -> None:
    engine = _watchdog_engine()
    handler = engine._make_think_in_tool_call_handler(
        agent_name="problem_solver", is_entry_turn=True
    )

    first = await handler("run_subagent")
    second = await handler("run_subagent")
    third = await handler("run_subagent")

    assert [first.retry, second.retry, third.retry] == [True, False, False]
    assert len(engine._transient.appended) == 1
    assert len(engine._emitters.think_in_tool_call_critical_messages) == 2
    assert engine._think_tag_streak is True


async def test_think_in_tool_call_streak_is_dedicated_from_the_others() -> None:
    """Must not combine with _stuck_streak/_cycle_streak/_tool_call_cycle_streak
    to trip an unrelated escalation."""
    engine = _watchdog_engine(autonomous=True)
    stall_handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    think_tag_handler = engine._make_think_in_tool_call_handler(
        agent_name="problem_solver", is_entry_turn=True
    )

    stall_decision = await stall_handler(
        TurnSignal(text="", thinking_text="", stop_reason="end_turn")
    )
    think_tag_decision = await think_tag_handler("run_subagent")

    assert stall_decision.retry is True
    assert think_tag_decision.retry is True  # not already critical -- separate streak
    assert engine._stuck_streak is True
    assert engine._think_tag_streak is True
    assert engine._emitters.critical_messages == []
    assert engine._emitters.think_in_tool_call_critical_messages == []


async def test_think_in_tool_call_streak_clears_on_a_genuine_response() -> None:
    engine = _watchdog_engine(autonomous=True)
    think_tag_handler = engine._make_think_in_tool_call_handler(
        agent_name="problem_solver", is_entry_turn=True
    )
    stall_handler = engine._make_stall_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )

    await think_tag_handler("run_subagent")
    assert engine._think_tag_streak is True

    healthy = TurnSignal(
        text="all done, see summary above", thinking_text="", stop_reason="end_turn"
    )
    await stall_handler(healthy)

    assert engine._think_tag_streak is False


async def test_think_in_tool_call_handler_subagent_scope_capped_then_silent() -> None:
    engine = _watchdog_engine()
    handler = engine._make_think_in_tool_call_handler(
        agent_name="investigator", is_entry_turn=False, subsession_id="sub-1"
    )

    decisions = [await handler("run_command") for _ in range(_MAX_CONSECUTIVE_NUDGES + 1)]

    assert [d.retry for d in decisions] == [True] * _MAX_CONSECUTIVE_NUDGES + [False]
    assert len(engine._transient.appended_sub) == _MAX_CONSECUTIVE_NUDGES
    assert engine._emitters.think_in_tool_call_critical_messages == []


# ---------------------------------------------------------------------------
# _make_tool_call_cyclic_handler -- the on_tool_call_cyclic closure
# (doc/STUCK_DETECTION.md §2.10)
# ---------------------------------------------------------------------------


def test_make_tool_call_cyclic_handler_returns_none_when_settings_off() -> None:
    engine = _watchdog_engine(settings={"stuck_detection": {"active": "off"}})
    handler = engine._make_tool_call_cyclic_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is None


def test_make_tool_call_cyclic_handler_returns_none_for_cloud_when_local_only() -> None:
    engine = _watchdog_engine()  # default active="local_only"
    handler = engine._make_tool_call_cyclic_handler(
        agent_name="problem_solver", routing=_CLOUD_ROUTING, is_entry_turn=True
    )
    assert handler is None


def test_make_tool_call_cyclic_handler_returns_callable_for_subagent_by_default() -> None:
    """Default scope ("top_level_and_subagents") watches sub-agent tool calls
    too — changed 2026-08-31 after a sub-agent's return_result call went
    unwatched and ballooned a `options` array to 1000+ repeated "n/a"
    elements (doc/STUCK_DETECTION.md §2.10)."""
    engine = _watchdog_engine(autonomous=True)  # default settings, no override
    handler = engine._make_tool_call_cyclic_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )
    assert handler is not None


async def test_tool_call_cyclic_handler_entry_turn_strike_one_notices_and_sets_streak() -> None:
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_tool_call_cyclic_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is not None

    decision = await handler('{"instructions": "do the thing do the thing do the thing"}')

    assert decision.retry is True
    assert decision.message is not None
    assert decision.message.role == "assistant"
    assert "repetitive loop" in decision.message.content.lower()
    assert engine._tool_call_cycle_streak is True
    role, content, entry_agent, kind, detail = engine._transient.appended[0]
    assert (role, content, entry_agent, kind) == (
        "assistant",
        decision.message.content,
        "problem_solver",
        "nudge",
    )
    assert detail["source"] == "tool_call_cyclic"
    assert engine._emitters.nudges == [
        (decision.message.content, ["tool_call_cyclic"], "auto", "tool_call_cyclic")
    ]


async def test_tool_call_cyclic_handler_entry_turn_strike_two_goes_critical() -> None:
    engine = _watchdog_engine(autonomous=True)
    handler = engine._make_tool_call_cyclic_handler(
        agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
    )
    assert handler is not None

    first = await handler("loop take 1")
    second = await handler("loop take 2")
    third = await handler("loop take 3")

    assert [first.retry, second.retry, third.retry] == [True, False, False]
    assert len(engine._transient.appended) == 1
    assert len(engine._emitters.tool_call_cyclic_critical_messages) == 2
    assert engine._tool_call_cycle_streak is True


async def test_tool_call_cyclic_handler_subagent_scope_capped_then_silent() -> None:
    engine = _watchdog_engine(
        autonomous=True,
        settings={"stuck_detection": {"active": "local_only", "scope": "top_level_and_subagents"}},
    )
    handler = engine._make_tool_call_cyclic_handler(
        agent_name="investigator",
        routing=_LOCAL_ROUTING,
        is_entry_turn=False,
        subsession_id="sub-1",
    )
    assert handler is not None

    decisions = [await handler(f"loop {i}") for i in range(_MAX_CONSECUTIVE_NUDGES + 1)]

    assert [d.retry for d in decisions] == [True] * _MAX_CONSECUTIVE_NUDGES + [False]
    assert len(engine._transient.appended_sub) == _MAX_CONSECUTIVE_NUDGES
    assert engine._emitters.tool_call_cyclic_critical_messages == []


# ---------------------------------------------------------------------------
# End-to-end: a real _run_agent_turn loop that actually stalls, then recovers
# ---------------------------------------------------------------------------


def _agent_turn_kwargs(engine: WorkflowEngine, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        llm=_FakeLLM(),
        routing=_LOCAL_ROUTING,
        model="model-x",
        system_prompt="sys",
        messages=[Message(role="user", content="solve the 1BRC challenge")],
        tools=[],
        tool_dispatch=None,
        stream_id="stream-1",
        agent_name="problem_solver",
        on_stall=engine._make_stall_handler(
            agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
        ),
        on_tool_calls=engine._make_progress_handler(is_entry_turn=True),
    )
    base.update(overrides)
    return base


async def test_run_agent_turn_end_to_end_autonomous_recovers_from_a_stall() -> None:
    """The exact shape of the traced failure (session 1784394478): a round
    that ends with no tool call and no text. In autonomous mode the watchdog
    must catch it, inject the nudge, and the loop must actually go around
    again — not just report that it *would* retry."""
    stuck_round = [TurnEnd(usage=_usage(), stop_reason="end_turn")]  # no TokenDelta at all
    recovered_round = [
        TokenDelta(text="Found it — build.sh compiles create_measurements.cpp."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError("no tool call in either round")

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(engine, tool_dispatch=tool_dispatch)
    )

    # The loop actually went around a second time — this is the crux of the
    # "does it catch it" question, not just a unit-level assertion.
    assert len(gateway.calls) == 2
    assert messages[-1].content == "Found it — build.sh compiles create_measurements.cpp."
    # messages[0] = the original prompt, [1] = the stuck round's own "(no
    # text)" placeholder (still recorded, honestly), [2] = the nudge, [3] =
    # the recovered final response.
    assert messages[1].content == "(no text)"
    assert (
        messages[2].role,
        messages[2].content,
    ) == (
        "user",
        "You stopped before finishing the task, without producing a final response "
        "or calling a tool. Continue from exactly where you left off.",
    )
    assert engine._transient.appended[0][3] == "nudge"
    assert engine._emitters.nudges[0][2] == "auto"


async def test_run_agent_turn_end_to_end_nudge_is_not_flushed_twice() -> None:
    """Regression test for a pre-existing double-persist bug: WatchdogMixin.
    _persist_nudge already writes the nudge message directly (kind-tagged) via
    TransientStore.append_message, bypassing `persisted_upto`. _run_agent_turn
    used to then re-persist that same message a second time, untagged, via its
    own generic `persist` callback (since `persisted_upto` never learned about
    the closure's direct write) -- this test drives a *real* `persist=`
    callback (every other end-to-end test in this file leaves it `None`,
    which is exactly why this had no coverage) through the nudge-retry path
    and asserts the nudge text appears exactly once, via the tagged direct
    write only."""
    stuck_round = [TurnEnd(usage=_usage(), stop_reason="end_turn")]
    recovered_round = [
        TokenDelta(text="Found it."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    engine = _watchdog_engine(autonomous=True, gateway=gateway)
    persisted: list[Message] = []

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine, tool_dispatch=tool_dispatch, persist=lambda batch: persisted.extend(batch)
        )
    )

    assert len(engine._transient.appended) == 1
    assert engine._transient.appended[0][3] == "nudge"
    nudge_text = engine._transient.appended[0][1]
    assert [m for m in persisted if m.content == nudge_text] == []
    # The round's own placeholder and the recovered final response still go
    # through the generic path, exactly once each.
    assert [m.content for m in persisted] == ["(no text)", "Found it."]


async def test_run_agent_turn_end_to_end_healthy_completion_never_touches_watchdog() -> None:
    """Control case: a normal, non-empty completion must not trigger anything
    — proves the watchdog doesn't false-positive on an ordinary turn end."""
    healthy_round = [
        TokenDelta(text="Benchmarked at 1.9s, wrote report.md. Done."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([healthy_round])
    engine = _watchdog_engine(autonomous=True, gateway=gateway)
    del engine._registry  # would blow up if the watchdog touched display_name

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(engine, tool_dispatch=tool_dispatch)
    )

    assert len(gateway.calls) == 1
    assert messages[-1].content == "Benchmarked at 1.9s, wrote report.md. Done."
    assert engine._transient.appended == []
    assert engine._emitters.nudges == []


async def test_run_agent_turn_end_to_end_truncated_generation_recovers_too() -> None:
    """The second red flag, driven through the real loop: a call cut off by
    the output-token cap (max_tokens), not just an empty response."""
    truncated_round = [
        TokenDelta(text="Running the benchmark now, this will take about"),
        TurnEnd(usage=_usage(), stop_reason="max_tokens"),
    ]
    recovered_round = [
        TokenDelta(text="...2 minutes. Done, see report.md."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([truncated_round, recovered_round])
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(engine, tool_dispatch=tool_dispatch)
    )

    assert len(gateway.calls) == 2
    assert messages[-1].content == "...2 minutes. Done, see report.md."
    assert engine._transient.appended[0][4]["reasons"] == ["truncated_generation"]


async def test_run_agent_turn_end_to_end_interactive_defers_and_ends_the_turn_idle() -> None:
    """Interactive, non-auto: the real loop must end the turn normally (no
    second gateway call) and leave remediation to the background watcher —
    proving the "session looks idle" guarantee end to end, not just in the
    closure unit tests above."""
    stuck_round = [TurnEnd(usage=_usage(), stop_reason="end_turn")]
    gateway = _FakeGateway([stuck_round])
    engine = _watchdog_engine(autonomous=False, gateway=gateway, gate_answers=["unstick"])

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(engine, tool_dispatch=tool_dispatch)
    )

    assert len(gateway.calls) == 1  # the turn ended, it did not loop
    assert messages[-1].content == "(no text)"
    assert engine._stuck_watchdog_task is not None

    engine._stuck_watchdog_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await engine._stuck_watchdog_task


# ---------------------------------------------------------------------------
# End-to-end: the mid-stream cyclic-thinking detector (doc/STUCK_DETECTION.md §2.7)
# ---------------------------------------------------------------------------

# Verified in test_cyclic_thinking.py to fire on the 3rd repeat -- reused here
# rather than a fresh string, so this end-to-end test rides an already-proven
# exact-repeat case instead of hoping a new one happens to clear the bar.
_LOOP_BLOCK = "The reasoning loop keeps repeating here in exactly this way!\n"


def _thinking_deltas(text: str, chunk_size: int = 20) -> list[object]:
    """Split *text* into several ThinkingDelta events (mirrors real sub-token
    streaming granularity -- detection must not depend on whole-line-sized
    deltas)."""
    return [ThinkingDelta(text=text[i : i + chunk_size]) for i in range(0, len(text), chunk_size)]


async def test_run_agent_turn_end_to_end_cyclic_thinking_aborts_and_recovers() -> None:
    """The concrete motivating case: a thinking block degenerates into the
    same lines over and over. The abort must happen mid-stream (proven by
    the trailing filler event never being consumed, not just by cancel()
    being called), and the turn must actually go around again afterward."""
    loop_text = _LOOP_BLOCK * 4
    stuck_round = [
        *_thinking_deltas(loop_text),
        ThinkingDelta(text="this trailing content must never be consumed"),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    recovered_round = [
        TokenDelta(text="Found a different approach — implementing it now."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError("no tool call in either round")

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_cyclic_thinking=engine._make_cyclic_thinking_handler(
                agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
            ),
        )
    )

    assert fake_llm.cancel_calls == ["stream-1"]
    # Consumed strictly fewer scripted events than the batch holds -- proves
    # the async-for actually broke early, not merely that cancel() fired.
    assert gateway.consumed[0] < len(stuck_round)
    assert len(gateway.calls) == 2  # round 2 (the recovery) actually happened
    assert messages[-1].content == "Found a different approach — implementing it now."
    assert engine._transient.appended[0][3] == "nudge"
    assert any(n[3] == "cyclic_thinking" for n in engine._emitters.nudges)
    assert engine._emitters.critical_messages == []  # ordinary watchdog untouched
    assert engine._cycle_streak is False  # cleared by the recovered round


async def test_run_agent_turn_end_to_end_second_cyclic_hit_ends_turn_critical() -> None:
    loop_text = _LOOP_BLOCK * 4
    stuck_round = [*_thinking_deltas(loop_text), TurnEnd(usage=_usage(), stop_reason="end_turn")]
    gateway = _FakeGateway([list(stuck_round), list(stuck_round)])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_cyclic_thinking=engine._make_cyclic_thinking_handler(
                agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
            ),
        )
    )

    assert len(gateway.calls) == 2  # ended after the second hit, no third round
    assert fake_llm.cancel_calls == ["stream-1", "stream-1"]
    assert len(engine._emitters.cyclic_critical_messages) == 1
    assert engine._cycle_streak is True
    # The round's own message-construction code is untouched by cyclic-abort
    # routing: the repeated thinking content is still honestly recorded
    # (as a thinking+text content-block list), same as any other round.
    assert isinstance(messages[-1].content, list)


async def test_run_agent_turn_ordinary_stall_unaffected_by_cyclic_thinking_wiring() -> None:
    """An ordinary (non-cyclic) empty-final-turn stall must still route
    through on_stall/detect_red_flags unaffected when on_cyclic_thinking is
    also wired in -- the two mechanisms must not cross-contaminate."""
    stuck_round = [TurnEnd(usage=_usage(), stop_reason="end_turn")]  # no thinking, no text
    recovered_round = [
        TokenDelta(text="Done, see the report."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_cyclic_thinking=engine._make_cyclic_thinking_handler(
                agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
            ),
        )
    )

    assert fake_llm.cancel_calls == []  # never triggered -- not a cyclic-thinking round
    assert engine._transient.appended[0][3] == "nudge"  # ordinary nudge path
    assert engine._transient.appended[0][4]["source"] == "stall"
    assert all(n[3] != "cyclic_thinking" for n in engine._emitters.nudges)
    assert messages[-1].content == "Done, see the report."


# ---------------------------------------------------------------------------
# End-to-end: the mid-stream think-in-tool-call detector (doc/STUCK_DETECTION.md §2.9)
# ---------------------------------------------------------------------------


def _tool_call_arg_deltas(
    text: str, tool_name: str = "run_subagent", chunk_size: int = 5
) -> list[object]:
    """Split *text* into several ToolCallArgDelta events (mirrors real
    sub-token streaming granularity)."""
    return [
        ToolCallArgDelta(tool_name=tool_name, text=text[i : i + chunk_size])
        for i in range(0, len(text), chunk_size)
    ]


async def test_run_agent_turn_end_to_end_think_in_tool_call_aborts_and_recovers() -> None:
    """The exact shape of the motivating incident: a <think> tag appears
    inside a run_subagent call's arguments. The abort must happen mid-stream
    (proven by the trailing filler event never being consumed), and the turn
    must actually go around again afterward."""
    stuck_round = [
        *_tool_call_arg_deltas('{"task": "<think>plan this out</think> build it"}'),
        ToolCallArgDelta(tool_name="run_subagent", text="this must never be consumed"),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    recovered_round = [
        TokenDelta(text="Found a different approach — implementing it now."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError("no tool call in either round")

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_think_in_tool_call=engine._make_think_in_tool_call_handler(
                agent_name="problem_solver", is_entry_turn=True
            ),
        )
    )

    assert fake_llm.cancel_calls == ["stream-1"]
    assert gateway.consumed[0] < len(stuck_round)
    assert len(gateway.calls) == 2  # round 2 (the recovery) actually happened
    assert messages[-1].content == "Found a different approach — implementing it now."
    assert engine._transient.appended[0][3] == "nudge"
    assert engine._transient.appended[0][4]["source"] == "think_in_tool_call"
    assert "run_subagent" in engine._transient.appended[0][1]
    assert engine._emitters.critical_messages == []
    assert engine._think_tag_streak is False  # cleared by the recovered round


async def test_run_agent_turn_end_to_end_second_think_in_tool_call_hit_ends_turn_critical() -> None:
    stuck_round = [
        *_tool_call_arg_deltas('{"task": "<think>plan</think>"}'),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([list(stuck_round), list(stuck_round)])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_think_in_tool_call=engine._make_think_in_tool_call_handler(
                agent_name="problem_solver", is_entry_turn=True
            ),
        )
    )

    assert len(gateway.calls) == 2  # ended after the second hit, no third round
    assert fake_llm.cancel_calls == ["stream-1", "stream-1"]
    assert len(engine._emitters.think_in_tool_call_critical_messages) == 1
    assert engine._think_tag_streak is True


async def test_run_agent_turn_end_to_end_think_in_tool_call_fires_with_stuck_detection_off() -> (
    None
):
    """Proves the always-on gating decision end to end, not just in the
    closure unit tests: turning stuck_detection off entirely must not
    suppress this detector."""
    stuck_round = [
        *_tool_call_arg_deltas('{"task": "<think>plan</think>"}'),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    recovered_round = [
        TokenDelta(text="Done."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(
        autonomous=True, gateway=gateway, settings={"stuck_detection": {"active": "off"}}
    )

    async def tool_dispatch(*a, **k):
        raise AssertionError

    await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_think_in_tool_call=engine._make_think_in_tool_call_handler(
                agent_name="problem_solver", is_entry_turn=True
            ),
        )
    )

    assert fake_llm.cancel_calls == ["stream-1"]
    assert engine._transient.appended[0][4]["source"] == "think_in_tool_call"


# ---------------------------------------------------------------------------
# End-to-end: the mid-stream tool-call-argument cyclic detector
# (doc/STUCK_DETECTION.md §2.10)
# ---------------------------------------------------------------------------

_TOOL_CALL_LOOP_BLOCK = '"do the exact same repetitive thing over and over," '


async def test_run_agent_turn_end_to_end_tool_call_cyclic_aborts_and_recovers() -> None:
    loop_text = _TOOL_CALL_LOOP_BLOCK * 4
    stuck_round = [
        *_tool_call_arg_deltas(loop_text),
        ToolCallArgDelta(tool_name="run_subagent", text="this must never be consumed"),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    recovered_round = [
        TokenDelta(text="Found a different approach — implementing it now."),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round, recovered_round])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError("no tool call in either round")

    messages, _files = await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_tool_call_cyclic=engine._make_tool_call_cyclic_handler(
                agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
            ),
        )
    )

    assert fake_llm.cancel_calls == ["stream-1"]
    assert gateway.consumed[0] < len(stuck_round)
    assert len(gateway.calls) == 2
    assert messages[-1].content == "Found a different approach — implementing it now."
    assert engine._transient.appended[0][3] == "nudge"
    assert engine._transient.appended[0][4]["source"] == "tool_call_cyclic"
    assert engine._tool_call_cycle_streak is False  # cleared by the recovered round


async def test_run_agent_turn_end_to_end_think_tag_takes_priority_over_repetition() -> None:
    """When both detectors are wired and the streamed text would trip both
    (a repeated block that also contains a stray <think> tag), the
    think-tag abort wins -- it is checked first on every fragment."""
    loop_text = "<think>" + (_TOOL_CALL_LOOP_BLOCK * 4)
    stuck_round = [
        *_tool_call_arg_deltas(loop_text),
        TurnEnd(usage=_usage(), stop_reason="end_turn"),
    ]
    gateway = _FakeGateway([stuck_round])
    fake_llm = _FakeLLM()
    engine = _watchdog_engine(autonomous=True, gateway=gateway)

    async def tool_dispatch(*a, **k):
        raise AssertionError

    await engine._run_agent_turn(
        **_agent_turn_kwargs(
            engine,
            llm=fake_llm,
            tool_dispatch=tool_dispatch,
            on_think_in_tool_call=engine._make_think_in_tool_call_handler(
                agent_name="problem_solver", is_entry_turn=True
            ),
            on_tool_call_cyclic=engine._make_tool_call_cyclic_handler(
                agent_name="problem_solver", routing=_LOCAL_ROUTING, is_entry_turn=True
            ),
        )
    )

    assert engine._transient.appended[0][4]["source"] == "think_in_tool_call"
    assert engine._think_tag_streak is True
    assert engine._tool_call_cycle_streak is False
