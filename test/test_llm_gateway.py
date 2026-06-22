"""Behavioral tests for :class:`kodo.llms.LLMGateway`.

Deterministic and network-free.  ``FakePlugin`` blocks on an explicit release
event so admission order is observable; a ``ManualClock`` drives the delay-aware
queue / 429 backoff without real time passing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from kodo.common import Envelope
from kodo.llms import LLMGateway, LLMRouting, RateLimited
from kodo.llms._interface import LLMPlugin, StreamEvent, TokenDelta, TurnEnd, Usage

LOCAL = LLMRouting(residence="local")
CLOUD_A = LLMRouting(residence="cloud", vendor="anthropic")
CLOUD_B = LLMRouting(residence="cloud", vendor="openai")

_USAGE = Usage(
    input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0, model="m"
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingSink:
    """Captures ``llm.waiting`` event payloads pushed by the gateway."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def send(self, env: Envelope) -> None:
        self.events.append(dict(env.payload))

    @property
    def reasons(self) -> list[object]:
        return [e.get("reason") for e in self.events if e.get("waiting")]


class FakePlugin(LLMPlugin):
    """A scriptable plugin: optionally fails with 429 N times, then streams.

    On admission it appends ``label`` to ``started`` and (if a ``release`` event
    is given) blocks until that event is set, so tests can hold a slot open.
    """

    def __init__(
        self,
        label: str,
        started: list[str],
        *,
        release: asyncio.Event | None = None,
        fail_times: int = 0,
        error: Exception | None = None,
        payload: list[StreamEvent] | None = None,
    ) -> None:
        self.__label = label
        self.__started = started
        self.__release = release
        self.__fail_times = fail_times
        self.__error = error
        self.__payload = payload

    @property
    def name(self) -> str:
        return self.__label

    @property
    def supported_models(self) -> list[str]:
        return ["fake"]

    async def stream_query(self, **kwargs: object) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        if self.__fail_times > 0:
            self.__fail_times -= 1
            raise RateLimited()
        self.__started.append(self.__label)
        if self.__payload is not None:
            for ev in self.__payload:
                yield ev
            return
        yield TokenDelta(text="hi")
        if self.__release is not None:
            await self.__release.wait()
        if self.__error is not None:
            raise self.__error
        yield TurnEnd(usage=_USAGE, stop_reason="end_turn")

    async def cancel(self, stream_id: str) -> None:
        return None


class ManualClock:
    """A virtual clock: ``sleep`` blocks until ``advance`` passes the wake time."""

    def __init__(self) -> None:
        self.t = 0.0
        self.__sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            return
        fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()
        self.__sleepers.append((self.t + delay, fut))
        await fut

    def advance(self, dt: float) -> None:
        self.t += dt
        for wake_at, fut in list(self.__sleepers):
            if wake_at <= self.t and not fut.done():
                fut.set_result(None)
                self.__sleepers.remove((wake_at, fut))


async def _drain(gen: AsyncIterator[StreamEvent], out: list[StreamEvent]) -> None:
    async for ev in gen:
        out.append(ev)


async def _settle() -> None:
    # Let pending tasks make progress; a few hops cover chained awaits.
    for _ in range(8):
        await asyncio.sleep(0)


def _gateway(concurrency: int = 2, clock: ManualClock | None = None) -> LLMGateway:
    if clock is None:
        return LLMGateway(cloud_concurrency=lambda: concurrency)
    return LLMGateway(cloud_concurrency=lambda: concurrency, now=clock.now, sleep=clock.sleep)


# ---------------------------------------------------------------------------
# 1 & 2 — local serial gate, shared across models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_serializes_and_emits_waiting() -> None:
    gw = _gateway()
    started: list[str] = []
    rel_a = asyncio.Event()
    sink_a, sink_b = RecordingSink(), RecordingSink()

    a = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=LOCAL, plugin=FakePlugin("A", started, release=rel_a), sink=sink_a
            ),
            [],
        )
    )
    await _settle()
    assert started == ["A"]  # A admitted immediately, holding the only local slot

    b = asyncio.create_task(
        _drain(gw.stream_query(routing=LOCAL, plugin=FakePlugin("B", started), sink=sink_b), [])
    )
    await _settle()
    assert started == ["A"]  # B is queued behind A
    assert sink_b.reasons == ["queued"]
    assert sink_a.events == []  # A never waited

    rel_a.set()
    await asyncio.gather(a, b)
    assert started == ["A", "B"]
    assert sink_b.events[-1]["waiting"] is False  # B's wait cleared on admission
    assert sink_b.events[-1]["type"] == "llm.waiting"


@pytest.mark.asyncio
async def test_all_local_models_share_one_queue() -> None:
    gw = _gateway()
    started: list[str] = []
    rel = asyncio.Event()
    # Different "models" via kwargs, but both are local → one shared feed.
    a = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=LOCAL,
                plugin=FakePlugin("m1", started, release=rel),
                sink=RecordingSink(),
                model="m1",
            ),
            [],
        )
    )
    await _settle()
    b = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=LOCAL, plugin=FakePlugin("m2", started), sink=RecordingSink(), model="m2"
            ),
            [],
        )
    )
    await _settle()
    assert started == ["m1"]  # serialized despite different models
    rel.set()
    await asyncio.gather(a, b)
    assert started == ["m1", "m2"]


# ---------------------------------------------------------------------------
# 3, 4, 5 — cloud concurrency, per-vendor isolation, configurable limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_concurrency_two() -> None:
    gw = _gateway(concurrency=2)
    started: list[str] = []
    rels = [asyncio.Event() for _ in range(3)]
    tasks = []
    for i in range(3):
        tasks.append(
            asyncio.create_task(
                _drain(
                    gw.stream_query(
                        routing=CLOUD_A,
                        plugin=FakePlugin(f"c{i}", started, release=rels[i]),
                        sink=RecordingSink(),
                    ),
                    [],
                )
            )
        )
        await _settle()
    assert started == ["c0", "c1"]  # two admitted, third queued
    rels[0].set()
    await _settle()
    assert started == ["c0", "c1", "c2"]  # freed slot admits the third
    for r in rels:
        r.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_per_vendor_isolation() -> None:
    gw = _gateway(concurrency=1)
    started: list[str] = []
    rel_a, rel_b = asyncio.Event(), asyncio.Event()
    a = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=CLOUD_A,
                plugin=FakePlugin("A", started, release=rel_a),
                sink=RecordingSink(),
            ),
            [],
        )
    )
    await _settle()
    b = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=CLOUD_B,
                plugin=FakePlugin("B", started, release=rel_b),
                sink=RecordingSink(),
            ),
            [],
        )
    )
    await _settle()
    assert started == ["A", "B"]  # different vendors run concurrently at limit 1
    rel_a.set()
    rel_b.set()
    await asyncio.gather(a, b)


@pytest.mark.asyncio
async def test_configurable_limit_live() -> None:
    limit = {"n": 1}
    gw = LLMGateway(cloud_concurrency=lambda: limit["n"])
    started: list[str] = []
    rels = [asyncio.Event() for _ in range(3)]
    t0 = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=CLOUD_A,
                plugin=FakePlugin("a0", started, release=rels[0]),
                sink=RecordingSink(),
            ),
            [],
        )
    )
    await _settle()
    t1 = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=CLOUD_A,
                plugin=FakePlugin("a1", started, release=rels[1]),
                sink=RecordingSink(),
            ),
            [],
        )
    )
    await _settle()
    assert started == ["a0"]  # limit 1 serializes
    limit["n"] = 3  # raise the limit live
    rels[0].set()
    await _settle()
    assert "a1" in started
    for r in rels:
        r.set()
    await asyncio.gather(t0, t1)


# ---------------------------------------------------------------------------
# 6 — 429 backoff + delay-aware queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_requeues_with_delay_and_backoff() -> None:
    clock = ManualClock()
    gw = _gateway(concurrency=2, clock=clock)
    started: list[str] = []
    sink = RecordingSink()
    out: list[StreamEvent] = []
    plugin = FakePlugin("R", started, fail_times=1)  # 429 once, then succeeds

    task = asyncio.create_task(
        _drain(gw.stream_query(routing=CLOUD_A, plugin=plugin, sink=sink), out)
    )
    await _settle()

    # The 429 fired, a throttled event was emitted with the base 60s delay, and
    # the request is now parked in the delay queue — not yet retried.
    throttled = [e for e in sink.events if e.get("reason") == "throttled"]
    assert throttled and throttled[0]["retry_in_seconds"] == 60.0
    assert started == []
    assert not task.done()

    clock.advance(59)
    await _settle()
    assert started == []  # still inside the delay window

    clock.advance(1)
    await _settle()
    await task
    assert started == ["R"]  # admitted and succeeded after the delay elapsed
    assert any(isinstance(ev, TurnEnd) for ev in out)


@pytest.mark.asyncio
async def test_consecutive_429_doubles_then_resets() -> None:
    clock = ManualClock()
    gw = _gateway(concurrency=2, clock=clock)
    started: list[str] = []
    sink = RecordingSink()
    plugin = FakePlugin("R", started, fail_times=3)  # 60, 120, 240 then success

    task = asyncio.create_task(
        _drain(gw.stream_query(routing=CLOUD_A, plugin=plugin, sink=sink), [])
    )
    for _ in range(3):
        await _settle()
        clock.advance(10_000)  # blow past whatever the current delay is
    await _settle()
    await task

    delays = [e["retry_in_seconds"] for e in sink.events if e.get("reason") == "throttled"]
    assert delays == [60.0, 120.0, 240.0]

    # Backoff reset after the success: a fresh failing call sees the base again.
    sink2 = RecordingSink()
    plugin2 = FakePlugin("R2", started, fail_times=1)
    task2 = asyncio.create_task(
        _drain(gw.stream_query(routing=CLOUD_A, plugin=plugin2, sink=sink2), [])
    )
    await _settle()
    clock.advance(10_000)
    await _settle()
    await task2
    d2 = [e["retry_in_seconds"] for e in sink2.events if e.get("reason") == "throttled"]
    assert d2 == [60.0]


# ---------------------------------------------------------------------------
# 7, 8, 9, 10 — invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_spurious_wait_when_free() -> None:
    gw = _gateway()
    sink = RecordingSink()
    started: list[str] = []
    await _drain(gw.stream_query(routing=CLOUD_A, plugin=FakePlugin("x", started), sink=sink), [])
    assert sink.events == []  # never had to wait
    assert started == ["x"]


@pytest.mark.asyncio
async def test_slot_released_on_error() -> None:
    gw = _gateway(concurrency=1)
    started: list[str] = []
    boom = RuntimeError("boom")
    rel = asyncio.Event()
    rel.set()
    with pytest.raises(RuntimeError):
        await _drain(
            gw.stream_query(
                routing=CLOUD_A,
                plugin=FakePlugin("e", started, release=rel, error=boom),
                sink=RecordingSink(),
            ),
            [],
        )
    # Slot was released despite the error — a subsequent call runs without waiting.
    sink = RecordingSink()
    await _drain(gw.stream_query(routing=CLOUD_A, plugin=FakePlugin("ok", started), sink=sink), [])
    assert started == ["e", "ok"]
    assert sink.events == []


@pytest.mark.asyncio
async def test_streaming_passthrough_in_order() -> None:
    gw = _gateway()
    payload = [
        TokenDelta(text="a"),
        TokenDelta(text="b"),
        TurnEnd(usage=_USAGE, stop_reason="end_turn"),
    ]
    out: list[StreamEvent] = []
    await _drain(
        gw.stream_query(
            routing=CLOUD_A, plugin=FakePlugin("p", [], payload=payload), sink=RecordingSink()
        ),
        out,
    )
    assert out == payload


@pytest.mark.asyncio
async def test_cancelled_queued_call_frees_the_feed() -> None:
    gw = _gateway(concurrency=1)
    started: list[str] = []
    rel_a = asyncio.Event()
    a = asyncio.create_task(
        _drain(
            gw.stream_query(
                routing=CLOUD_A,
                plugin=FakePlugin("A", started, release=rel_a),
                sink=RecordingSink(),
            ),
            [],
        )
    )
    await _settle()
    b = asyncio.create_task(
        _drain(
            gw.stream_query(routing=CLOUD_A, plugin=FakePlugin("B", started), sink=RecordingSink()),
            [],
        )
    )
    await _settle()
    assert started == ["A"]  # B queued

    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b
    await _settle()

    # C must not be blocked by B's abandoned queue slot.
    rel_a.set()
    await a
    sink_c = RecordingSink()
    await _drain(gw.stream_query(routing=CLOUD_A, plugin=FakePlugin("C", started), sink=sink_c), [])
    assert started == ["A", "C"]
    assert sink_c.events == []
