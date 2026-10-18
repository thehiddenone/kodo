"""Turn-long holds on the one managed llama-server's command line.

Several sessions can be bound to different local LLMs, but kodo runs one
managed llama-server (doc/SESSION_LLM_BINDING.md §3). A session that makes a
local request holds the server's command line until its turn ends; a session
that needs a *different* command line waits until every holder has released,
then becomes the first holder of the new one — so a model swap never lands in
the middle of another session's turn (D4). A same-command request that
arrives while a swap is waiting queues behind it, so a busy model cannot
starve the swap.

Purely an ordering primitive: it never starts or stops llama-server. The
:class:`~kodo.llms.LLMGateway` acquires it before a local request competes
for the local feed, and the actual restart happens later, inside the plugin,
when :func:`kodo.llms.llamacpp.ensure_llama_running` sees the new command
line.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["LlamaLease"]


@dataclass
class _Waiter:
    holder: str
    command: tuple[str, ...]
    granted: asyncio.Future[None]


class LlamaLease:
    """FIFO, command-line-keyed holds on the managed llama-server."""

    __command: tuple[str, ...] | None
    __holders: set[str]
    __waiters: deque[_Waiter]

    def __init__(self) -> None:
        """Start with no holder and no waiter."""
        self.__command = None
        self.__holders = set()
        self.__waiters = deque()

    @property
    def command(self) -> tuple[str, ...] | None:
        """The command line the current holders run on; ``None`` when nobody holds it."""
        return self.__command if self.__holders else None

    @property
    def holders(self) -> frozenset[str]:
        """Who holds the lease right now."""
        return frozenset(self.__holders)

    async def acquire(
        self,
        holder: str,
        command: tuple[str, ...],
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Hold *command* for *holder*, waiting while others hold a different one.

        Re-entrant: a holder already on *command* returns at once. A holder
        that is the only one and asks for another command line switches
        without waiting; one that shares the lease gives its hold up and
        waits like anybody else.

        Args:
            holder: Who holds it — a session id, or a one-off caller's tag.
            command: The llama-server argv the holder needs.
            on_wait: Awaited once, right before the caller starts waiting.

        Returns:
            bool: ``True`` when the caller had to wait.
        """
        if holder in self.__holders:
            if command == self.__command:
                return False
            if self.__holders == {holder}:
                self.__command = command
                return False
            self.__holders.discard(holder)
            self.__wake()
        if not self.__waiters and (not self.__holders or command == self.__command):
            self.__command = command
            self.__holders.add(holder)
            return False

        waiter = _Waiter(holder, command, asyncio.get_running_loop().create_future())
        self.__waiters.append(waiter)
        if on_wait is not None:
            await on_wait()
        try:
            await waiter.granted
        except asyncio.CancelledError:
            if waiter in self.__waiters:
                self.__waiters.remove(waiter)
            elif waiter.granted.done() and not waiter.granted.cancelled():
                # Granted in the same tick the caller was cancelled: give it back.
                self.release(holder)
            self.__wake()
            raise
        return True

    def release(self, holder: str) -> None:
        """Drop *holder*'s hold (no-op when it holds nothing) and admit whoever waits next.

        Args:
            holder: The holder passed to :meth:`acquire`.
        """
        self.__holders.discard(holder)
        self.__wake()

    def __wake(self) -> None:
        if self.__holders:
            return
        while self.__waiters and self.__waiters[0].granted.done():
            self.__waiters.popleft()
        if not self.__waiters:
            return
        self.__command = self.__waiters[0].command
        for waiter in list(self.__waiters):
            if waiter.command != self.__command or waiter.granted.done():
                continue
            self.__waiters.remove(waiter)
            self.__holders.add(waiter.holder)
            waiter.granted.set_result(None)
