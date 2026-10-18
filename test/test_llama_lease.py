"""Behavioral tests for :class:`kodo.llms.LlamaLease` and the gateway's use of it.

The lease keeps the one managed llama-server on a single command line while
any session's turn holds it (doc/SESSION_LLM_BINDING.md §3): same command
lines share it, a different one waits until every holder released, FIFO.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from kodo.common import Envelope
from kodo.llms import LlamaLease, LLMGateway, LLMRouting
from kodo.llms._interface import LLMPlugin, StreamEvent, TokenDelta, TurnEnd, Usage

_X = ("llama-server", "--model", "x.gguf")
_Y = ("llama-server", "--model", "y.gguf")
_USAGE = Usage(
    input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0, model="m"
)


async def _settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# LlamaLease
# ---------------------------------------------------------------------------


async def test_first_holder_takes_the_lease_without_waiting() -> None:
    lease = LlamaLease()

    waited = await lease.acquire("a", _X)

    assert waited is False
    assert lease.holders == {"a"}
    assert lease.command == _X


async def test_same_command_line_is_shared() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)

    waited = await lease.acquire("b", _X)

    assert waited is False
    assert lease.holders == {"a", "b"}


async def test_holder_asking_again_for_its_command_line_does_not_wait() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)

    assert await lease.acquire("a", _X) is False
    assert lease.holders == {"a"}


async def test_sole_holder_switches_command_line_at_once() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)

    assert await lease.acquire("a", _Y) is False
    assert lease.command == _Y


async def test_different_command_line_waits_for_every_holder() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)
    await lease.acquire("b", _X)
    notices: list[str] = []

    async def _notice() -> None:
        notices.append("waiting")

    waiter = asyncio.create_task(lease.acquire("c", _Y, _notice))
    await _settle()
    assert not waiter.done()
    assert notices == ["waiting"]

    lease.release("a")
    await _settle()
    assert not waiter.done()

    lease.release("b")
    await _settle()
    assert waiter.result() is True
    assert lease.holders == {"c"}
    assert lease.command == _Y


async def test_same_command_line_queues_behind_a_waiting_swap() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)
    swap = asyncio.create_task(lease.acquire("b", _Y))
    await _settle()

    late = asyncio.create_task(lease.acquire("c", _X))
    await _settle()
    assert not late.done()  # does not starve the swap

    lease.release("a")
    await _settle()
    assert swap.done() and not late.done()
    assert lease.holders == {"b"}

    lease.release("b")
    await _settle()
    assert late.done()
    assert lease.command == _X


async def test_waiters_on_the_same_command_line_are_admitted_together() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)
    first = asyncio.create_task(lease.acquire("b", _Y))
    second = asyncio.create_task(lease.acquire("c", _Y))
    await _settle()

    lease.release("a")
    await _settle()

    assert first.done() and second.done()
    assert lease.holders == {"b", "c"}


async def test_a_holder_that_shares_and_wants_another_line_gives_its_hold_up() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)
    await lease.acquire("b", _X)

    switch = asyncio.create_task(lease.acquire("a", _Y))
    await _settle()
    assert not switch.done()
    assert lease.holders == {"b"}

    lease.release("b")
    await _settle()
    assert switch.done()
    assert lease.holders == {"a"}
    assert lease.command == _Y


async def test_a_cancelled_waiter_leaves_the_queue() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)
    waiter = asyncio.create_task(lease.acquire("b", _Y))
    await _settle()

    waiter.cancel()
    await _settle()
    lease.release("a")
    await _settle()

    assert lease.holders == frozenset()
    assert lease.command is None
    assert await lease.acquire("c", _X) is False


async def test_releasing_a_non_holder_is_a_no_op() -> None:
    lease = LlamaLease()
    await lease.acquire("a", _X)

    lease.release("nobody")

    assert lease.holders == {"a"}


# ---------------------------------------------------------------------------
# Through the gateway
# ---------------------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def send(self, env: Envelope) -> None:
        self.events.append(dict(env.payload))

    @property
    def reasons(self) -> list[object]:
        return [e.get("reason") for e in self.events if e.get("waiting")]


class _Plugin(LLMPlugin):
    def __init__(self, label: str, started: list[str]) -> None:
        self.__label = label
        self.__started = started

    @property
    def name(self) -> str:
        return self.__label

    @property
    def supported_models(self) -> list[str]:
        return ["fake"]

    async def stream_query(self, **kwargs: object) -> AsyncIterator[StreamEvent]:  # type: ignore[override]
        self.__started.append(self.__label)
        yield TokenDelta(text="hi")
        yield TurnEnd(usage=_USAGE, stop_reason="end_turn")

    async def cancel(self, stream_id: str) -> None:
        return None


async def _drain(gen: AsyncIterator[StreamEvent]) -> None:
    async for _ in gen:
        pass


def _routing(command: tuple[str, ...] | None, holder: str) -> LLMRouting:
    return LLMRouting(residence="local", launch=command, holder=holder)


async def test_a_session_holds_llama_after_its_request_until_released() -> None:
    gateway = LLMGateway(cloud_concurrency=lambda: 2)
    started: list[str] = []
    await _drain(
        gateway.stream_query(
            routing=_routing(_X, "s1"), plugin=_Plugin("s1", started), sink=_Sink()
        )
    )
    assert gateway.local_holders == {"s1"}

    sink = _Sink()
    other = asyncio.create_task(
        _drain(
            gateway.stream_query(
                routing=_routing(_Y, "s2"), plugin=_Plugin("s2", started), sink=sink
            )
        )
    )
    await _settle()
    assert started == ["s1"]
    assert sink.reasons == ["model_swap"]

    gateway.release_local("s1")
    await other
    assert started == ["s1", "s2"]
    assert sink.events[-1]["waiting"] is False


async def test_requests_without_a_command_line_never_take_the_lease() -> None:
    gateway = LLMGateway(cloud_concurrency=lambda: 2)
    started: list[str] = []
    await _drain(
        gateway.stream_query(
            routing=_routing(_X, "s1"), plugin=_Plugin("s1", started), sink=_Sink()
        )
    )

    # A self-hosted session: no managed command line, nothing to wait for.
    await _drain(
        gateway.stream_query(
            routing=_routing(None, "s2"), plugin=_Plugin("s2", started), sink=_Sink()
        )
    )

    assert started == ["s1", "s2"]
    assert gateway.local_holders == {"s1"}


async def test_acquire_local_waits_like_a_request() -> None:
    gateway = LLMGateway(cloud_concurrency=lambda: 2)
    await gateway.acquire_local("s1", _X)
    sink = _Sink()

    start = asyncio.create_task(gateway.acquire_local("llama.start", _Y, sink=sink))
    await _settle()
    assert not start.done()
    assert sink.reasons == ["model_swap"]

    gateway.release_local("s1")
    await start
    assert gateway.local_holders == {"llama.start"}


@pytest.mark.parametrize("command", [_X, _Y])
async def test_same_command_line_runs_side_by_side_in_the_lease(
    command: tuple[str, ...],
) -> None:
    gateway = LLMGateway(cloud_concurrency=lambda: 2)
    await gateway.acquire_local("s1", command)

    await gateway.acquire_local("s2", command)

    assert gateway.local_holders == {"s1", "s2"}
