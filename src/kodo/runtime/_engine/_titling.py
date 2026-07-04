"""Session titling (engine-driven, invisible to the user).

:class:`SessionTitler` names a session from its first prompt by driving the
``session_titler`` sub-agent directly (never via the tool surface). The
titler session is silent: no streaming, state, or agent events are emitted —
only its USD cost is folded into the running session total through the
engine's silent-turn plumbing, reached via the :class:`TitlerHost` protocol.
"""

from __future__ import annotations

import logging
from typing import Protocol

from kodo.common import Envelope, MessageSink
from kodo.llms import LLMPlugin, LLMRouting, Message
from kodo.state import TransientStore
from kodo.subagents import AgentRegistry, SubAgent
from kodo.transport import EVT_SESSION_NAME

from ._events import EngineEmitters
from ._shared import _SESSION_TITLER_AGENT_NAME

_log = logging.getLogger(__name__)

# Maximum length of a generated session title, in characters.
_MAX_TITLE_LEN = 60
# A usable title must name the subject in at least this many words. Weak titler
# models sometimes collapse to a single bare token (e.g. the implementation
# language, "python"); such answers are rejected and re-generated once.
_MIN_TITLE_WORDS = 2
_MAX_TITLE_WORDS = 8


class TitlerHost(Protocol):
    """What the titler needs back from the engine (LLM plumbing + identity)."""

    _orch_session_id: str

    def _agent_available(self, name: str) -> bool: ...

    async def _resolve_plugin(
        self, capability: str, force_model_key: str | None = None
    ) -> tuple[LLMPlugin, str, LLMRouting]: ...

    async def _run_silent_return_turn(
        self,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
    ) -> tuple[dict[str, object] | None, str]: ...


class SessionTitler:
    """Names an unnamed session from its first prompt, silently."""

    def __init__(
        self,
        host: TitlerHost,
        *,
        registry: AgentRegistry,
        transient: TransientStore,
        sink: MessageSink,
        emitters: EngineEmitters,
    ) -> None:
        self._host = host
        self._registry = registry
        self._transient = transient
        self._sink = sink
        self._emitters = emitters

    async def maybe_generate_session_title(self, text: str) -> None:
        """Name the session from its first prompt, if it is still unnamed.

        Runs the ``session_titler`` sub-agent directly (never via the tool
        surface), persists the result to ``meta.json``, and pushes it to the
        client so the editor tab can be renamed. The titler session is silent:
        no streaming, state, or agent events are emitted — only its USD cost is
        folded into the running session total. Any failure is swallowed so the
        user's prompt is never blocked by titling.
        """
        if not text.strip():
            return
        if self._transient.is_session_named:
            return
        if not self._host._agent_available(_SESSION_TITLER_AGENT_NAME):
            return

        # Tell the client a (silent) naming call is in flight so it can show a
        # "Naming session …" indicator — otherwise the titling round-trip looks
        # like an unexplained stall before the main agent starts streaming.
        await self._emitters.emit_session_naming(True)
        try:
            title = await self._generate_session_title(text)
        except Exception:
            _log.exception("Session title generation failed; leaving session unnamed")
            return
        finally:
            await self._emitters.emit_session_naming(False)

        if not title:
            return

        self._transient.set_session_name(title)
        await self._sink.send(
            Envelope.make_event(
                EVT_SESSION_NAME,
                {"session_id": self._host._orch_session_id, "name": title},
            )
        )
        _log.info("Session %s named %r", self._host._orch_session_id, title)

    async def _generate_session_title(self, text: str) -> str | None:
        """Run a silent LLM call to produce a session title from *text*.

        Does not forward any stream/thinking events to the client; only the
        title text is collected. The call's USD cost is added to the running
        cumulative total and pushed as a cost-only ``usage.update`` (no
        ``last_call_tokens``, so it adds no entry to the session feed).

        Weak titler models occasionally ignore the rules and emit a degenerate
        answer (a single bare token such as the implementation language). The
        sanitized result is validated against :meth:`_is_acceptable_title`; on
        rejection we re-prompt once with a corrective nudge appended to the
        conversation, then give up (returning ``None`` leaves the session
        unnamed so the next prompt can try again).
        """
        agent = self._registry.get(_SESSION_TITLER_AGENT_NAME)
        plugin, model_id, routing = await self._host._resolve_plugin(agent.capability)

        messages: list[Message] = [Message(role="user", content=text)]
        for _attempt in range(2):
            raw = await self._run_titler_turn(routing, plugin, model_id, agent, messages)
            title = self._sanitize_title(raw)
            if self._is_acceptable_title(title):
                return title
            # Show the model its own rejected answer and ask for a real title.
            messages.append(Message(role="assistant", content=raw))
            messages.append(
                Message(
                    role="user",
                    content=(
                        "That is not a usable title. It must be 2 to 6 words in "
                        "Title Case naming the subject of the request — not the "
                        "programming language, not a single bare word. Output "
                        "only the corrected title."
                    ),
                )
            )

        # Both attempts failed validation; better to leave it unnamed than to
        # commit a degenerate title.
        _log.info("Session titler produced no acceptable title after retry")
        return None

    async def _run_titler_turn(
        self,
        routing: LLMRouting,
        plugin: LLMPlugin,
        model_id: str,
        agent: SubAgent,
        messages: list[Message],
    ) -> str:
        """One silent titler LLM turn; returns the title (via return_result) or text."""
        result, text = await self._host._run_silent_return_turn(
            routing, plugin, model_id, agent, messages
        )
        if result is not None:
            title = result.get("title")
            if isinstance(title, str) and title.strip():
                return title
        return text

    @staticmethod
    def _is_acceptable_title(title: str | None) -> bool:
        """Reject degenerate titler output that slipped past sanitizing.

        Enforces the word-count band the prompt asks for (a single bare token
        such as ``python`` is the canonical failure). Title Case, length, and
        formatting are already handled by :meth:`_sanitize_title`.
        """
        if not title:
            return False
        words = title.split()
        return _MIN_TITLE_WORDS <= len(words) <= _MAX_TITLE_WORDS

    @staticmethod
    def _sanitize_title(raw: str) -> str | None:
        """Reduce raw model output to a single clean title line.

        Takes the first non-empty line, strips wrapping quotes and a leading
        ``Title:`` label, collapses whitespace, and clamps the length. Returns
        ``None`` if nothing usable remains.
        """
        line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        if not line:
            return None
        if ":" in line:
            head, _, tail = line.partition(":")
            if head.strip().lower() in ("title", "session", "session title"):
                line = tail.strip()
        line = line.strip().strip("\"'`").strip()
        line = " ".join(line.split())
        if not line:
            return None
        if len(line) > _MAX_TITLE_LEN:
            line = line[:_MAX_TITLE_LEN].rstrip()
        return line or None
