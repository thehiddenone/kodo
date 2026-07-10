"""Session titling (engine-driven, invisible to the user).

:class:`SessionTitler` names a session from its first prompt using a small
local CPU summarization model (:mod:`kodo.titling`), run in a background
thread and fired-and-forgotten from the queue worker (:mod:`._worker`) so the
main agent's turn never waits on it. This replaced the old ``session_titler``
sub-agent — a full LLM turn that took 10-15s — because titling only needs a
short extractive summary, not a chat model. The titler is still silent: no
streaming or agent events are emitted for it, only the eventual
``session.naming``/``session.name`` events and the session's ``meta.json``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Protocol

from kodo.common import Envelope, MessageSink
from kodo.state import TransientStore
from kodo.titling import generate_title
from kodo.transport import EVT_SESSION_NAME

from ._events import EngineEmitters

_log = logging.getLogger(__name__)

# Maximum length of a generated session title, in characters.
_MAX_TITLE_LEN = 60
# A usable title must name the subject in at least this many words. The
# summarizer occasionally collapses to a single bare token; such answers are
# rejected outright rather than retried — generation is deterministic
# (do_sample=False), so retrying with the same input would just repeat it.
# The next user prompt gets its own attempt instead.
_MIN_TITLE_WORDS = 2
_MAX_TITLE_WORDS = 8

# Short first prompts (greetings, bare commands like "fix this" or "add a
# login page") aren't worth a trip through the LLM summarizer, which just
# echoes them back anyway. At or below this word count, the title is instead
# the prompt's own leading words — see _SHORT_TITLE_WORDS. Only prompts longer
# than this go through the summarizer.
_SHORT_PROMPT_MAX_WORDS = 10
# How many leading words of a short prompt (see _SHORT_PROMPT_MAX_WORDS) to
# use verbatim as its title.
_SHORT_TITLE_WORDS = 5

# Anything that isn't a letter or digit is treated as a word separator and
# dropped — titles must be pure alphanumeric words with a single space
# between them (no punctuation, quotes, or other special characters).
_NON_ALNUM_RUN_RE = re.compile(r"[^0-9A-Za-z]+")


class TitlerHost(Protocol):
    """What the titler needs back from the engine: session identity only."""

    _orch_session_id: str


class SessionTitler:
    """Names an unnamed session from its first prompt, silently and async."""

    def __init__(
        self,
        host: TitlerHost,
        *,
        transient: TransientStore,
        sink: MessageSink,
        emitters: EngineEmitters,
    ) -> None:
        self._host = host
        self._transient = transient
        self._sink = sink
        self._emitters = emitters
        self._naming_task: asyncio.Task[None] | None = None

    def maybe_generate_session_title(self, text: str) -> None:
        """Fire-and-forget: schedule title generation for *text*.

        A prompt of ``_SHORT_PROMPT_MAX_WORDS`` words or fewer is titled
        directly from its own leading words (see ``_SHORT_TITLE_WORDS``) —
        deterministic and instant, no LLM involved. A longer prompt goes
        through the local summarizer in a worker thread instead; the queue
        worker (``._worker``) never awaits either path, unlike the old
        full-subagent titler. Safe to call once per queued prompt: it is a
        no-op for blank/whitespace-only text, a no-op once the session is
        already named, and a no-op while a previous call is still in flight
        (guards against a second prompt racing the first title generation
        before it lands).
        """
        words = text.split()
        if not words:
            return
        if self._transient.is_session_named:
            return
        if self._naming_task is not None and not self._naming_task.done():
            return
        if len(words) <= _SHORT_PROMPT_MAX_WORDS:
            self._naming_task = asyncio.create_task(self._report_short_title(words))
        else:
            self._naming_task = asyncio.create_task(self._generate_and_report(text))

    async def _report_short_title(self, words: list[str]) -> None:
        """Title a short prompt from its own leading words, skipping the LLM.

        Deterministic and instant, so unlike :meth:`_generate_and_report` this
        never toggles the ``session.naming`` indicator — there's no
        noticeable wall-clock gap to signal. Every word band/acceptability
        check in :meth:`_is_acceptable_title` exists to catch degenerate
        *model* output; a prompt's own words need no such gate.
        """
        title = self._leading_words_title(words)
        if not title:
            return
        await self._apply_title(title)

    async def _generate_and_report(self, text: str) -> None:
        """Generate, sanitize, persist, and push a title for *text*.

        If the summarizer errors out or produces a degenerate title, falls
        back to *text*'s own leading words (the same shortcut
        :meth:`_report_short_title` uses for short prompts) rather than
        leaving the session unnamed. Only if that fallback also yields
        nothing (e.g. blank text) does the session stay unnamed for the next
        prompt to try again.
        """
        await self._emitters.emit_session_naming(True)
        try:
            raw = await asyncio.to_thread(generate_title, text)
        except Exception:
            _log.exception("Session title generation failed; falling back to leading words")
            raw = None
        finally:
            await self._emitters.emit_session_naming(False)

        title = self._sanitize_title(raw) if raw else None
        if not title or not self._is_acceptable_title(title):
            _log.info(
                "Titler produced no acceptable title for session %s; falling back to leading words",
                self._host._orch_session_id,
            )
            title = self._leading_words_title(text.split())
            if not title:
                return

        await self._apply_title(title)

    @classmethod
    def _leading_words_title(cls, words: list[str]) -> str | None:
        """Sanitize the first ``_SHORT_TITLE_WORDS`` of *words* into a title."""
        return cls._sanitize_title(" ".join(words[:_SHORT_TITLE_WORDS]))

    async def _apply_title(self, title: str) -> None:
        """Persist *title* and push it over the wire as ``session.name``."""
        self._transient.set_session_name(title)
        await self._sink.send(
            Envelope.make_event(
                EVT_SESSION_NAME,
                {"session_id": self._host._orch_session_id, "name": title},
            )
        )
        _log.info("Session %s named %r", self._host._orch_session_id, title)

    @staticmethod
    def _is_acceptable_title(title: str | None) -> bool:
        """Reject degenerate titler output that slipped past sanitizing.

        Enforces the word-count band a usable title should fall in (a single
        bare token is the canonical failure). Casing, length, and formatting
        are already handled by :meth:`_sanitize_title`.
        """
        if not title:
            return False
        words = title.split()
        return _MIN_TITLE_WORDS <= len(words) <= _MAX_TITLE_WORDS

    @staticmethod
    def _sanitize_title(raw: str) -> str | None:
        """Reduce raw model output to a single clean, Title Case line.

        Takes the first non-empty line, strips every non-alphanumeric
        character (punctuation, quotes, symbols — each run collapses to a
        single word separator, so no character other than a letter or digit
        ever survives), clamps to the first ``_MAX_TITLE_WORDS`` words, Title
        Cases each word (uppercasing only its first letter — this leaves
        existing internal casing such as acronyms alone, unlike
        ``str.title()``), and clamps the character length. The result is
        always alphanumeric words separated by exactly one space.

        The word clamp is load-bearing: the summarizer emits a full echo of the
        prompt (often 10-15 words), and clamping by characters alone left a
        title that :meth:`_is_acceptable_title` then rejected for exceeding the
        word band — so good titles were silently dropped. Clamping to the word
        budget *here* also trims the model's degenerate tail (e.g. a repeated
        "... terminal UI UI") off the end of an otherwise good title.
        """
        line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        if not line:
            return None
        words = [w for w in _NON_ALNUM_RUN_RE.sub(" ", line).split() if w]
        if not words:
            return None
        words = words[:_MAX_TITLE_WORDS]
        line = " ".join(w[:1].upper() + w[1:] for w in words)
        if len(line) > _MAX_TITLE_LEN:
            line = line[:_MAX_TITLE_LEN].rstrip()
        return line or None
