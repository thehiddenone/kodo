"""Bridge botocore's blocking ``EventStream`` onto an ``async for``.

Every other cloud plugin here talks to an SDK that is natively async
(``openai.AsyncOpenAI``, ``anthropic.AsyncAnthropic``), so its streaming loop
is just ``async for chunk in response``. boto3 has no async client: there is
no ``converse``/``converse_stream`` on ``aioboto3`` either, and boto3 is
already a declared dependency of kodo, so wrapping the blocking client is the
right trade rather than adding a second AWS SDK.

``client.converse_stream(...)`` blocks until the response headers arrive and
then hands back a botocore ``EventStream`` whose iteration blocks on the
socket. Both halves therefore run on a worker thread
(``loop.run_in_executor``), which hands each decoded event back to the event
loop through an :class:`asyncio.Queue` via ``call_soon_threadsafe``. The queue
is deliberately unbounded: Converse events are individually tiny (a token
delta, a few tool-argument characters) and the consumer drains as fast as the
producer fills, so the backpressure a bounded queue would buy costs a
``QueueFull`` failure mode for nothing.

**Cancellation** (``LLMPlugin.cancel``'s within-one-second contract) is
handled two ways at once, because the worker spends most of its life blocked
in a socket read where a flag check can't reach it:

* the consumer races each ``queue.get()`` against the caller's cancel event,
  so the *caller* stops immediately rather than after the next event; and
* teardown closes the underlying ``EventStream``, which unblocks that socket
  read so the worker thread actually exits instead of leaking for the
  remainder of the generation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any, cast

from ._retry import translate_botocore_error

__all__ = ["aiter_converse_stream"]

_log = logging.getLogger(__name__)

#: Queue sentinel marking "the worker thread has finished, for any reason".
_DONE = object()


def _close_quietly(stream: Any) -> None:
    """Close a botocore ``EventStream``, swallowing whatever closing raises.

    Called during teardown, where the stream may already be exhausted, already
    closed, or mid-error — none of which should mask the reason we're tearing
    down.
    """
    try:
        stream.close()
    except Exception as exc:  # noqa: BLE001 — teardown must not raise
        _log.debug("Ignoring error while closing Bedrock event stream: %s", exc)


async def aiter_converse_stream(
    client: Any,
    request: dict[str, object],
    cancel_event: asyncio.Event,
) -> AsyncGenerator[dict[str, Any], None]:
    """Yield ``converse_stream`` events off a worker thread.

    Args:
        client (Any): A boto3 ``bedrock-runtime`` client. Untyped because
            boto3 ships no type information; see ``pyproject.toml``'s mypy
            override.
        request (dict[str, object]): Keyword arguments for
            ``client.converse_stream``.
        cancel_event (asyncio.Event): Set by :meth:`~kodo.llms.bedrock.
            BedrockPlugin.cancel` to abort; the iteration ends promptly and
            the underlying HTTP stream is closed.

    Yields:
        dict[str, Any]: One decoded Converse stream event
        (``messageStart``/``contentBlockStart``/``contentBlockDelta``/
        ``contentBlockStop``/``messageStop``/``metadata``).

    Raises:
        BedrockStatusError: Whatever :func:`~kodo.llms.bedrock._retry.
            translate_botocore_error` classified the failure as — raised on
            the *caller's* task, not swallowed on the worker thread.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    stream_holder: list[Any] = []

    def worker() -> None:
        try:
            response = client.converse_stream(**request)
            stream = response["stream"]
            stream_holder.append(stream)
            for event in stream:
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's task
            loop.call_soon_threadsafe(queue.put_nowait, translate_botocore_error(exc))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    # Fire-and-forget: the thread ends on its own once the stream is
    # exhausted or closed. There is no way to interrupt a thread already
    # blocked in a socket read, which is why teardown closes the stream.
    loop.run_in_executor(None, worker)
    cancel_waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait(
                {getter, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter not in done:
                getter.cancel()
                return
            item = getter.result()
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield cast(dict[str, Any], item)
    finally:
        cancel_waiter.cancel()
        # Closing the stream is what actually unblocks the worker's socket
        # read; without it a cancelled turn would leave the thread parked
        # until the model finished generating.
        if stream_holder:
            _close_quietly(stream_holder[0])
