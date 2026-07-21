"""Context compaction (in-place; see doc/STATE_AND_LIFECYCLE.md §4.5).

:class:`ContextCompactor` owns the live context gauge (measured token count,
the compaction-in-flight flag, and the registry key of the model that owns
the live context) and the compaction runs themselves. The live main message
history stays engine-owned — the compactor reaches back through the narrow
:class:`CompactorHost` protocol to read and reset it, and to run the silent
summarisation call on the engine's LLM plumbing.

The live main context is measured in tokens after every entry-agent turn;
once it reaches ``_COMPACTION_THRESHOLD`` of the current model's context
window (the per-model ``context_window`` in the LLM registry, resolved via
:meth:`ContextCompactor.context_limit`) the engine runs the ``compactor``
sub-agent to summarise the context and reset it in place. The user can also
trigger this manually while idle (``compact.now``). A model switch to a
smaller window can trigger it immediately (``handle_config_changed``).
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from kodo.common import Envelope, MessageSink
from kodo.llms import (
    LLMPlugin,
    LLMRouting,
    Message,
    get_context_window,
    strip_kodo_callouts,
)
from kodo.project import kodo_user_dir
from kodo.state import TransientStore
from kodo.subagents import AgentRegistry, SubAgent
from kodo.transport import EVT_CONTEXT_COMPACTED

from .._session import SessionState
from ._events import EngineEmitters
from ._shared import _COMPACTOR_AGENT_NAME

_log = logging.getLogger(__name__)

_COMPACTION_THRESHOLD = 0.9

# How much of a compaction summary travels in the ``context.compacted`` event as
# a feed-divider excerpt (the full summary lives in the session.jsonl marker).
_COMPACTION_EXCERPT_LEN = 280


def compaction_context_message(summary: str) -> Message:
    """Build the synthetic user message that replaces a compacted context.

    Used both when compaction happens live and when a resumed session is
    rebuilt from its latest ``compaction`` marker (see
    :meth:`~._history.HistoryProjector.load_main_messages`), so the in-memory
    context is identical in both paths.
    """
    return Message(
        role="user",
        content=(
            "The conversation so far has been compacted to stay within the "
            "context limit. The following is a summary of everything that "
            "happened before this point; treat it as your working memory and "
            "continue seamlessly from it:\n\n" + summary
        ),
    )


def estimate_tokens(messages: list[Message]) -> int:
    """Rough token estimate (~4 chars/token) for messages with no live usage.

    Used only to seed the gauge immediately after a compaction (or on resume,
    before the next real turn supplies a measured count).
    """
    chars = 0
    for msg in messages:
        content = msg.content
        chars += (
            len(content)
            if isinstance(content, str)
            else len(json.dumps(content, ensure_ascii=False))
        )
    return max(1, chars // 4)


def render_transcript(messages: list[Message]) -> str:
    """Flatten a message list to a plain-text transcript for summarisation.

    Tool-use/`tool_result`/thinking blocks are rendered as labelled lines so
    the compactor sees the whole exchange as data without needing the tool
    schemas that a structured replay would require.
    """
    out: list[str] = []
    for msg in messages:
        content = msg.content
        header = f"## {msg.role.upper()}"
        is_assistant = msg.role == "assistant"
        if isinstance(content, str):
            text = strip_kodo_callouts(content) if is_assistant else content
            out.append(f"{header}\n{text}")
            continue
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = str(block.get("text", ""))
                # One-way notifications to the user; never replayed as context.
                parts.append(strip_kodo_callouts(text) if is_assistant else text)
            elif btype == "thinking":
                parts.append(f"[thinking] {block.get('thinking', '')}")
            elif btype == "tool_use":
                args = json.dumps(block.get("input", {}), ensure_ascii=False)
                parts.append(f"[tool_use {block.get('name', '')}] {args}")
            elif btype == "tool_result":
                raw = block.get("content")
                body = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
                parts.append(f"[tool_result] {body}")
        out.append(f"{header}\n" + "\n".join(parts))
    return "\n\n".join(out)


class CompactorHost(Protocol):
    """What the compactor needs back from the engine.

    The live main message history and the LLM plumbing are engine-owned;
    everything else the compactor uses is injected as a plain dependency.
    """

    _main_messages: list[Message]

    def _agent_available(self, name: str) -> bool: ...

    def _resolve_model_key(self, capability: str) -> str: ...

    def _entry_capability(self) -> str: ...

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


class ContextCompactor:
    """Owns the live context gauge and drives in-place compaction runs."""

    def __init__(
        self,
        host: CompactorHost,
        *,
        registry: AgentRegistry,
        transient: TransientStore,
        sink: MessageSink,
        session: SessionState,
        emitters: EngineEmitters,
    ) -> None:
        self._host = host
        self._registry = registry
        self._transient = transient
        self._sink = sink
        self._session = session
        self._emitters = emitters
        # Measured token size of the live main context (last entry-agent turn's
        # input + cache + output, or an estimate immediately after a compaction).
        self._context_tokens = 0
        # True while a compaction run is in flight (disables the manual trigger
        # and drives the "Compacting context…" indicator).
        self._compacting = False
        # Registry key of the model the entry agent last ran on (the model that
        # owns the live main context). Used to detect a model switch and, when
        # the new model has a smaller context window, compact with this (old)
        # model first.
        self._active_model_key: str | None = None

    @property
    def context_tokens(self) -> int:
        """Measured token size of the live main context."""
        return self._context_tokens

    @context_tokens.setter
    def context_tokens(self, value: int) -> None:
        self._context_tokens = value

    def note_active_model(self, model_key: str) -> None:
        """Record the registry key of the model now owning the main context."""
        self._active_model_key = model_key

    def context_limit(self) -> int:
        """Token budget for the main context = current model's context window.

        Resolved from the entry-agent model selected in settings (see
        the engine's ``_resolve_model_key``) via the per-model
        ``context_window`` in the LLM registry. This is *not* session-specific:
        switching the model mid-session changes the limit, and the gauge/auto-
        compaction threshold follow it on the next stats emission (or
        immediately, via ``handle_config_changed``).
        """
        model_key = self._host._resolve_model_key(self._host._entry_capability())
        return get_context_window(model_key, kodo_user_dir())

    def can_compact(self) -> bool:
        """True when a manual compaction would be honoured right now.

        Mirrors the worker-side guard so the client can enable/disable its
        "Compact now" button from the pushed stats: the entry agent must be idle
        (the last turn ended and no new one started), a compaction must not be in
        flight, there must be measured context, and the ``compactor`` agent must
        be registered.
        """
        return (
            self._session.phase == "awaiting_user"
            and not self._compacting
            and bool(self._host._main_messages)
            and self._context_tokens > 0
            and self._host._agent_available(_COMPACTOR_AGENT_NAME)
        )

    def context_stats_payload(self) -> dict[str, object]:
        """The ``context.stats`` event payload (consumed by the emitters)."""
        limit = self.context_limit()
        current = self._context_tokens
        percent = round(100.0 * current / limit, 1) if limit > 0 else 0.0
        return {
            "current_tokens": current,
            "limit_tokens": limit,
            "percent": percent,
            "can_compact": self.can_compact(),
        }

    async def maybe_auto_compact(self) -> None:
        """Auto-compact when the just-measured context crosses the threshold.

        Called at the end of every main entry-agent turn (after the LLM has
        responded). One pass is enough — compaction collapses the context far
        below the threshold — so this never loops.
        """
        if self._compacting:
            return
        limit = self.context_limit()
        if self._context_tokens >= _COMPACTION_THRESHOLD * limit:
            _log.info(
                "Context at %d/%d tokens (≥%d%%) — auto-compacting",
                self._context_tokens,
                limit,
                int(_COMPACTION_THRESHOLD * 100),
            )
            await self._run_compaction("auto")

    async def run_manual_compaction(self) -> None:
        """Honour a queued ``compact.now`` request, if currently compactable."""
        if not self.can_compact():
            _log.info("compact.now ignored — not in a compactable state")
            return
        await self._run_compaction("manual")

    async def handle_config_changed(self) -> None:
        """Worker-side handler for a settings change (e.g. a model switch).

        Detects whether the entry-agent model changed. If it shrank below the live
        context size, compact with the *outgoing* model first (so the switch only
        takes effect on a context that fits the new window); then record the new
        model and re-emit the context gauge (the limit may have moved either way).
        """
        new_key = self._host._resolve_model_key(self._host._entry_capability())
        old_key = self._active_model_key
        if old_key is not None and new_key != old_key:
            new_limit = get_context_window(new_key, kodo_user_dir())
            if self._context_tokens > new_limit and self.can_compact():
                _log.info(
                    "Model switch %s → %s shrinks context window to %d < %d live tokens "
                    "— compacting with the outgoing model first",
                    old_key,
                    new_key,
                    new_limit,
                    self._context_tokens,
                )
                await self._run_compaction("model_switch", force_model_key=old_key)
        self._active_model_key = new_key
        await self._emitters.emit_context_stats()

    async def _run_compaction(self, reason: str, force_model_key: str | None = None) -> None:
        """Summarise the live main context with the compactor and reset it.

        The full ``session.jsonl`` is preserved as audit history: this appends a
        ``compaction`` marker carrying the summary, then resets the live LLM
        context to a single synthetic block holding that summary. On resume,
        :meth:`~._history.HistoryProjector.load_main_messages` rebuilds the
        context from the latest marker onward (summary + any later messages), so
        the pre-compaction transcript is never resent to the model. ``reason``
        is ``"auto"``, ``"manual"``, or ``"model_switch"``.

        Args:
            reason: Why the compaction ran (recorded on the marker).
            force_model_key: When set, the summarisation call runs on this exact
                model rather than the one currently selected in settings — used
                for a model switch so the *outgoing* model compacts before the
                switch takes effect.
        """
        if not self._host._main_messages or not self._host._agent_available(_COMPACTOR_AGENT_NAME):
            return

        self._compacting = True
        await self._emitters.emit_context_compacting(True)
        await self._emitters.emit_context_stats()  # reflect can_compact=False while running
        tokens_before = self._context_tokens
        summary: str | None = None
        try:
            summary = await self._generate_compaction_summary(force_model_key=force_model_key)
        except Exception:
            _log.exception("Compaction summary generation failed; context unchanged")
        finally:
            self._compacting = False
            await self._emitters.emit_context_compacting(False)

        if not summary:
            await self._emitters.emit_context_stats()
            return

        context_msg = compaction_context_message(summary)
        tokens_after = estimate_tokens([context_msg])
        self._transient.append_marker(
            {
                "type": "compaction",
                "summary": summary,
                "reason": reason,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            }
        )
        self._host._main_messages = [context_msg]
        self._context_tokens = tokens_after

        await self._sink.send(
            Envelope.make_event(
                EVT_CONTEXT_COMPACTED,
                {
                    "summary_excerpt": summary[:_COMPACTION_EXCERPT_LEN],
                    # Full summary = the exact context the conversation continues
                    # from; the client reveals it in the collapsible divider.
                    "summary": summary,
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                },
            )
        )
        await self._emitters.emit_context_stats()
        _log.info("Context compacted (%s): ~%d → ~%d tokens", reason, tokens_before, tokens_after)

    async def _generate_compaction_summary(self, force_model_key: str | None = None) -> str | None:
        """Run one silent LLM call producing a compact briefing of the context.

        The current main message list is rendered to a plain-text transcript and
        handed to the ``compactor`` sub-agent as a single user message; the model
        gets no tools. Like the titler, this streams nothing to the feed — only
        the summary text is collected and the call's USD cost folded into the
        running total.

        Args:
            force_model_key: When set, run on this exact model instead of the one
                resolved from current settings (see :meth:`_run_compaction`).
        """
        agent = self._registry.get(_COMPACTOR_AGENT_NAME)
        plugin, model_id, routing = await self._host._resolve_plugin(
            agent.capability, force_model_key=force_model_key
        )
        transcript = render_transcript(self._host._main_messages)
        messages: list[Message] = [
            Message(role="user", content=f"Conversation transcript to compact:\n\n{transcript}")
        ]
        result, text = await self._host._run_silent_return_turn(
            routing, plugin, model_id, agent, messages
        )
        if result is not None:
            summary = result.get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
        return text.strip() or None
