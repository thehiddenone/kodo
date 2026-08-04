"""Session opening greeting (engine-driven, fired once per brand-new session).

:class:`SessionGreeter` replaces the WebView's own previously-hardcoded
empty-state placeholder ("Hello there. I'm Kodo. Ready to build something
awesome.") with a short, varied greeting written by
:func:`kodo.titling.generate_greeting` — the same dedicated Qwen3-0.6B
llama-server :mod:`._titling` uses, a different (input-free, randomly themed)
prompt. Fired once, fire-and-forget, from :meth:`WorkflowEngine.start`'s
brand-new-session branch — never for a resumed session. If the titler isn't
up (not installed, still starting, download in progress, ...) or the
completion call fails, falls back to a fixed default line rather than
leaving the session's feed empty.
"""

from __future__ import annotations

import asyncio
import logging

from kodo.titling import generate_greeting

from ._events import EngineEmitters

_log = logging.getLogger(__name__)

# Shown when the titler is unavailable for any reason — the same line
# kodo-vsix's WebView used to hardcode as its empty-state placeholder before
# this feature, so a fresh install with llama.cpp not yet set up still reads
# as a deliberate greeting rather than a degraded one.
_DEFAULT_GREETING = "Hello there. I'm Kodo. Ready to build something awesome."

# Defensive clamp on raw model output — a greeting is meant to be a couple of
# short sentences; anything beyond this is almost certainly the model
# rambling rather than a longer greeting worth keeping in full.
_MAX_GREETING_LEN = 400


class SessionGreeter:
    """Writes and pushes a brand-new session's opening greeting, once."""

    def __init__(self, *, emitters: EngineEmitters) -> None:
        self._emitters = emitters
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Fire-and-forget: schedule greeting generation for this session.

        Never awaited by the caller (:meth:`WorkflowEngine.start`'s
        brand-new-session branch) — the greeting must never delay
        ``hello.ack`` or anything else in the handshake. Idempotent per
        engine instance: a second call while the first is still in flight is
        a no-op, though in practice this is only ever called once, right
        when the session is created.
        """
        if self._task is not None and not self._task.done():
            _log.info("SessionGreeter.start: skipped, a greeting generation is already in flight")
            return
        _log.info("SessionGreeter.start: scheduling greeting generation")
        self._task = asyncio.create_task(self._generate_and_emit())

    async def _generate_and_emit(self) -> None:
        try:
            raw = await generate_greeting()
        except Exception:
            _log.exception(
                "SessionGreeter: generate_greeting() raised; falling back to the default greeting"
            )
            raw = None
        else:
            if raw is None:
                _log.info(
                    "SessionGreeter: generate_greeting() returned None (titler server "
                    "unavailable — see kodo.titling logs above for why) — falling back to the "
                    "default greeting"
                )
            else:
                _log.info("SessionGreeter: titler raw greeting = %r", raw)

        text = self._sanitize(raw) if raw else None
        if raw and not text:
            _log.info(
                "SessionGreeter: raw greeting sanitized to nothing — falling back to the "
                "default greeting"
            )
        _log.info("SessionGreeter: emitting greeting (source=%s)", "titler" if text else "default")
        await self._emitters.emit_greeting(text or _DEFAULT_GREETING)

    @staticmethod
    def _sanitize(raw: str) -> str | None:
        """Light cleanup of raw model output — not a word-clamp like titles.

        A greeting is meant to be read as prose, so this only strips
        incidental wrapping (surrounding whitespace/quotes the model
        sometimes adds despite being told not to) and defensively clamps an
        implausibly long response, rather than rewriting punctuation/spacing
        the way title sanitization does.
        """
        text = raw.strip().strip("\"'").strip()
        if not text:
            return None
        if len(text) > _MAX_GREETING_LEN:
            text = text[:_MAX_GREETING_LEN].rstrip()
        return text or None
