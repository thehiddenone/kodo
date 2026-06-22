"""Shared LLM gateway — the single point through which every session's LLM
requests are scheduled.

Sessions never call :meth:`LLMPlugin.stream_query` directly; they go through one
process-wide :class:`LLMGateway`, which mimics the plugin's streaming facade but
adds concurrency control, fair FIFO queueing, and HTTP-429 backoff.  The plugins
stay stateless one-shot facades — **all** queue/gate/throttle policy lives here.

Routing is by **feed**:

* ``local`` — every local plugin (any model) shares **one** serial feed
  (``max_slots == 1``), so the single local llama-server is never asked to serve
  two requests at once.
* ``cloud:<vendor>`` — one feed per cloud vendor with a live, configurable
  concurrency limit (default 2).  When a vendor returns 429 the offending request
  is re-queued with an exponential, vendor-stateful delay (1, 2, 4, 8 … minutes;
  reset to the base on any success).

A feed is a **delay-aware FIFO admission controller**: a request honors its
``ready_at`` delay first, then competes for a slot strictly in arrival order.

The gateway is the only component that constructs and emits the
``llm.waiting`` envelopes (queued + throttled indicators).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from kodo.common import Envelope
from kodo.transport import EVT_LLM_WAITING

from ._interface import LLMPlugin, RateLimited, StreamEvent

__all__ = ["EventSink", "LLMGateway", "LLMRouting", "RateLimited"]

_log = logging.getLogger(__name__)

_LOCAL_FEED = "local"
_BASE_BACKOFF_SECONDS = 60.0
_MAX_BACKOFF_SECONDS = 3600.0


class EventSink(Protocol):
    """Anything the gateway can push a server→client event envelope through."""

    async def send(self, env: Envelope) -> None: ...


@dataclass(frozen=True)
class LLMRouting:
    """Where an LLM request should be scheduled.

    Attributes:
        residence: ``"local"`` or ``"cloud"``.
        vendor: Cloud vendor key (e.g. ``"anthropic"``) — selects the per-vendor
            feed.  Ignored for local requests (all local share one feed).
    """

    residence: str
    vendor: str | None = None


class _Feed:
    """A delay-aware FIFO admission controller with vendor-stateful backoff.

    Admission is two-stage: a request first honors its ``ready_at`` delay (an
    awaited sleep), then competes for one of ``max_slots`` slots strictly in
    arrival order.  ``now``/``sleep`` are injected so tests can drive a virtual
    clock.
    """

    def __init__(
        self,
        max_slots: Callable[[], int],
        *,
        base_backoff: float,
        max_backoff: float,
        now: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self.__max_slots = max_slots
        self.__base = base_backoff
        self.__cap = max_backoff
        self.__now = now
        self.__sleep = sleep
        self.__active = 0
        self.__seq = 0
        self.__waiters: list[int] = []
        self.__cond = asyncio.Condition()
        self.__backoff = base_backoff

    @property
    def active(self) -> int:
        """Number of requests currently holding a slot (for tests/inspection)."""
        return self.__active

    @property
    def current_backoff(self) -> float:
        """The delay (seconds) the next 429 in this feed would impose."""
        return self.__backoff

    def would_block(self, ready_at: float) -> bool:
        """Whether a request arriving now with ``ready_at`` would have to wait."""
        return (
            (ready_at - self.__now()) > 0
            or self.__active >= self.__max_slots()
            or len(self.__waiters) > 0
        )

    async def acquire(self, ready_at: float) -> None:
        """Wait out any delay, then take a slot in strict FIFO order."""
        delay = ready_at - self.__now()
        if delay > 0:
            await self.__sleep(delay)

        async with self.__cond:
            self.__seq += 1
            seq = self.__seq
            self.__waiters.append(seq)
            try:
                while not (self.__active < self.__max_slots() and self.__waiters[0] == seq):
                    await self.__cond.wait()
            except BaseException:
                self.__waiters.remove(seq)
                self.__cond.notify_all()
                raise
            self.__waiters.remove(seq)
            self.__active += 1
            # Wake the next head so multiple free slots admit without a release.
            self.__cond.notify_all()

    async def release(self) -> None:
        """Free a slot and wake the next waiter."""
        async with self.__cond:
            self.__active -= 1
            self.__cond.notify_all()

    def reset_backoff(self) -> None:
        """Reset the vendor backoff after a successful request."""
        self.__backoff = self.__base

    def bump_backoff(self, retry_after: float | None) -> float:
        """Return the delay to impose for a 429 and double the running backoff.

        Args:
            retry_after: Server-advised delay, used verbatim for this attempt if
                present; otherwise the current running backoff is used.

        Returns:
            float: Seconds to delay this request's re-queue.
        """
        delay = retry_after if retry_after is not None else self.__backoff
        self.__backoff = min(self.__backoff * 2, self.__cap)
        return delay


class LLMGateway:
    """Process-wide scheduler in front of every :class:`LLMPlugin`.

    Args:
        cloud_concurrency: Returns the live max concurrent requests per cloud
            vendor (read fresh on each admission so settings changes apply).
        base_backoff: Base 429 delay in seconds (default 60).
        max_backoff: Cap for the exponential 429 delay in seconds.
        now / sleep: Clock seams for deterministic tests.
    """

    def __init__(
        self,
        *,
        cloud_concurrency: Callable[[], int],
        base_backoff: float = _BASE_BACKOFF_SECONDS,
        max_backoff: float = _MAX_BACKOFF_SECONDS,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.__cloud_concurrency = cloud_concurrency
        self.__base = base_backoff
        self.__cap = max_backoff
        self.__now = now
        self.__sleep = sleep
        self.__feeds: dict[str, _Feed] = {}

    async def stream_query(
        self,
        *,
        routing: LLMRouting,
        plugin: LLMPlugin,
        sink: EventSink,
        **stream_kwargs: object,
    ) -> AsyncIterator[StreamEvent]:
        """Schedule and run ``plugin.stream_query(**stream_kwargs)`` through the
        appropriate feed, yielding its events.

        Emits ``llm.waiting`` while queued (``reason:"queued"``) or throttled
        (``reason:"throttled"`` with ``retry_in_seconds``); clears it once the
        request is admitted.  A 429 (``RateLimited``) raised before any event is
        yielded re-queues the request with an exponential vendor delay; a 429
        mid-stream is surfaced (a partial stream is not restarted).

        Args:
            routing: Feed selector (local, or cloud:<vendor>).
            plugin: The stateless plugin facade to drive.
            sink: Where ``llm.waiting`` events are pushed for this session.
            stream_kwargs: Forwarded verbatim to ``plugin.stream_query``.

        Yields:
            StreamEvent: Events from the plugin, in order.
        """
        feed = self.__feed_for(routing)
        ready_at = self.__now()
        waiting = False

        while True:
            if feed.would_block(ready_at) and not waiting:
                await self.__emit(sink, waiting=True, reason="queued")
                waiting = True

            await feed.acquire(ready_at)

            if waiting:
                await self.__emit(sink, waiting=False)
                waiting = False

            yielded = False
            try:
                async for event in plugin.stream_query(**stream_kwargs):  # type: ignore[arg-type]
                    yielded = True
                    yield event
            except RateLimited as exc:
                await feed.release()
                if yielded:
                    # A partial stream cannot be safely restarted — surface it.
                    raise
                delay = feed.bump_backoff(exc.retry_after)
                _log.warning("429 from feed %s — re-queuing in %.0fs", self.__key(routing), delay)
                await self.__emit(sink, waiting=True, reason="throttled", retry_in_seconds=delay)
                waiting = True
                ready_at = self.__now() + delay
                continue
            except BaseException:
                await feed.release()
                raise
            else:
                feed.reset_backoff()
                await feed.release()
                return

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def __key(routing: LLMRouting) -> str:
        if routing.residence == "local":
            return _LOCAL_FEED
        return f"cloud:{routing.vendor or 'unknown'}"

    def __feed_for(self, routing: LLMRouting) -> _Feed:
        key = self.__key(routing)
        feed = self.__feeds.get(key)
        if feed is None:
            max_slots: Callable[[], int] = (
                (lambda: 1) if routing.residence == "local" else self.__cloud_concurrency
            )
            feed = _Feed(
                max_slots,
                base_backoff=self.__base,
                max_backoff=self.__cap,
                now=self.__now,
                sleep=self.__sleep,
            )
            self.__feeds[key] = feed
        return feed

    @staticmethod
    async def __emit(
        sink: EventSink,
        *,
        waiting: bool,
        reason: str | None = None,
        retry_in_seconds: float | None = None,
    ) -> None:
        payload: dict[str, object] = {"waiting": waiting}
        if reason is not None:
            payload["reason"] = reason
        if retry_in_seconds is not None:
            payload["retry_in_seconds"] = retry_in_seconds
        await sink.send(Envelope.make_event(EVT_LLM_WAITING, payload))
