"""Entry-agent runs and the generic agent turn (single LLM call + tool loop).

The two top-level entry agents share one agent-agnostic main message history
(``_main_messages``); switching workflow mode only swaps the system prompt
and tool set, so the conversation continues seamlessly across a mode change.

``_run_agent_turn`` flushes the turn's message prefix (including the
spawning assistant message) to ``session.jsonl`` BEFORE dispatching *any*
tool call — see the flush note on that method. That means a crash (or a
client-visible side effect a tool triggers mid-dispatch, e.g. a
workspace-folder reload) always leaves a dangling ``tool_use`` on disk.
Cold-restart resume (:mod:`._resume`) only safely *re-dispatches* a dangling
call from its redispatch allow-list; any other dangling tool call is
reported back to the model as interrupted rather than re-executed, since
re-running an arbitrary tool (a shell command, a file write, ...) could
duplicate its side effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from kodo.common import Envelope
from kodo.llms import (
    LLMPlugin,
    LLMRouting,
    Message,
    ThinkingDelta,
    ThinkingSignature,
    TokenDelta,
    ToolCallEvent,
    ToolCallLogger,
    ToolSpec,
    TurnEnd,
)
from kodo.state import render_tool_call_markdown
from kodo.tools import ToolDispatcher, tools_for_agent
from kodo.toolspecs import build_detail_rows, normalize_output, tool_result_succeeded
from kodo.transport import (
    EVT_AGENT_TOOL_CALL_DETAIL,
    EVT_AGENT_TOOL_CALL_PREP,
    EVT_LLM_TURN_START,
    EVT_TOOL_INCOMPLIANT,
    EVT_USER_ATTACHMENTS,
)

from .._attachments import MAX_ATTACHMENTS, AttachmentError, inject_attachments, load_attachment
from .._checkpoints import CheckpointRef
from ._checkpointing import _GUIDED_STATE_TOOLS
from ._proto import EngineHost
from ._shared import _GUIDE_AGENT_NAME, _PROBLEM_SOLVER_AGENT_NAME, _SPECS_BY_NAME

_log = logging.getLogger(__name__)


class TurnLoopMixin:
    """Drives entry-agent prompts and the shared LLM turn/tool loop."""

    # ------------------------------------------------------------------
    # Guide LLM loop
    # ------------------------------------------------------------------

    async def _run_guide_with_input(
        self: EngineHost, text: str, attachments: list[str] | None = None
    ) -> None:
        await self._run_entry_agent(_GUIDE_AGENT_NAME, text, attachments)

    # ------------------------------------------------------------------
    # Problem Solver LLM loop (standalone, outside the Kodo pipeline)
    # ------------------------------------------------------------------

    async def _run_problem_solver_with_input(
        self: EngineHost, text: str, attachments: list[str] | None = None
    ) -> None:
        """Drive the standalone Problem Solver agent for one user prompt.

        Shares the agent-agnostic main history with the Guide (see
        :meth:`_run_entry_agent`): switching to Problem Solving only swaps the
        system prompt and tools, so the conversation continues across the mode
        change and — unlike before — Problem Solver turns now persist to
        ``session.jsonl``.
        """
        await self._run_entry_agent(_PROBLEM_SOLVER_AGENT_NAME, text, attachments)

    async def _run_entry_agent(
        self: EngineHost, agent_name: str, text: str, attachments: list[str] | None = None
    ) -> None:
        """Drive a top-level entry agent (Guide or Problem Solver).

        Both entry agents share one agent-agnostic main message history
        (``_main_messages``) persisted to ``session.jsonl``; the only per-mode
        difference is the system prompt and tool set. The seed user prompt is
        persisted immediately; the agent's own turns persist incrementally
        through :meth:`_run_agent_turn` (the spawning-tool prefix is flushed
        before any sub-agent dispatch so an interrupted sub-agent can resume).

        Prompt attachments are resolved here: each source file is read, copied
        into the session, and *injected* into the in-memory user message (so the
        LLM sees the content), while ``session.jsonl`` persists only the clean
        prompt plus links to the stored copies — see :meth:`_store_attachments`.
        """
        agent = self._registry.get(agent_name, self._session.effective_autonomous)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)
        # Remember the model that owns this main context, so a later model switch
        # can detect a shrink and compact with this model first.
        self._compactor.note_active_model(self._resolve_model_key(agent.capability))

        stored, errors = await self._store_attachments(attachments or [])
        for message in errors:
            await self._emitters.emit_error(message, recoverable=True)

        if text or stored:
            llm_text = inject_attachments(text, [(s["name"], s["content"]) for s in stored])
            self._main_messages = self._main_messages + [Message(role="user", content=llm_text)]
            self._transient.append_message(
                "user",
                text,
                entry_agent=agent_name,
                attachments=[{"name": s["name"], "stored": s["stored"]} for s in stored],
            )
            # Always echo the authoritative stored set when the user staged
            # anything — even an empty set (every file failed validation) — so
            # the client retargets the optimistically-rendered chips to the
            # stored copies, or clears them.
            if attachments:
                await self._sink.send(
                    Envelope.make_event(
                        EVT_USER_ATTACHMENTS,
                        {
                            "attachments": [
                                {
                                    "name": s["name"],
                                    "path": self._transient.attachment_abs_path(s["stored"]),
                                }
                                for s in stored
                            ]
                        },
                    )
                )

        self._session.phase = "running"
        self._session.agent = agent_name
        await self._emitters.emit_state()
        await self._emitters.emit_agent_started(agent_name)

        dispatcher = self._make_dispatcher(agent_name, self._orch_session_id)
        stream_id = uuid.uuid4().hex
        self._main_messages, _ = await self._run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=self._main_messages,
            tools=tools_for_agent(agent.tools),
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=agent_name,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=self._persist_main_messages(agent_name),
            flush_before_dispatch=True,
            track_context=True,
        )
        await self._sink.send(Envelope.make_stream_end(stream_id))
        await self._emitters.emit_agent_finished(agent_name)

        if self._session.phase != "done":
            self._session.phase = "awaiting_user"
        self._session.agent = None
        await self._emitters.emit_state()
        await self._compactor.maybe_auto_compact()

    async def _store_attachments(
        self: EngineHost, paths: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Validate, copy into the session, and link the prompt's attachments.

        Each source path is read + validated (text-only, per-file and combined
        size caps, at most :data:`MAX_ATTACHMENTS`) and, on success, copied into
        the session's ``attachments/`` directory. The original may have changed
        or vanished since the user staged it, so this server-side read is the
        authoritative gate; a rejected file is skipped and its reason returned
        as a user-facing error (the rest of the prompt still proceeds).

        Returns:
            tuple: ``(stored, errors)`` where each ``stored`` item is
            ``{"name", "stored", "content"}`` (``stored`` is the session-relative
            link, ``content`` is kept only for in-memory injection) and
            ``errors`` is a list of human-readable rejection messages.
        """
        stored: list[dict[str, str]] = []
        errors: list[str] = []
        running_total = 0
        for path in paths:
            if len(stored) >= MAX_ATTACHMENTS:
                errors.append(
                    f"At most {MAX_ATTACHMENTS} files can be attached; the rest were skipped."
                )
                break
            try:
                loaded = load_attachment(path, running_total=running_total)
            except AttachmentError as exc:
                errors.append(str(exc))
                continue
            rel = self._transient.store_attachment(loaded.name, loaded.content)
            if rel is None:
                errors.append(f'Attached file "{loaded.name}" could not be saved and was skipped.')
                continue
            running_total += loaded.size
            stored.append({"name": loaded.name, "stored": rel, "content": loaded.content})
        return stored, errors

    def _persist_main_messages(
        self: EngineHost, entry_agent: str
    ) -> Callable[[list[Message]], None]:
        """Return a persist hook that appends main messages to ``session.jsonl``."""

        def _persist(batch: list[Message]) -> None:
            for msg in batch:
                self._transient.append_message(msg.role, msg.content, entry_agent=entry_agent)

        return _persist

    # ------------------------------------------------------------------
    # Generic agent turn (single LLM call + tool loop)
    # ------------------------------------------------------------------

    async def _run_agent_turn(
        self: EngineHost,
        llm: LLMPlugin,
        routing: LLMRouting,
        model: str,
        system_prompt: str,
        messages: list[Message],
        tools: list[ToolSpec],
        tool_dispatch: Callable[[str, dict[str, object], str], Awaitable[str]],
        stream_id: str,
        agent_name: str = _GUIDE_AGENT_NAME,
        stop_after_tools: Callable[[], bool] | None = None,
        persist: Callable[[list[Message]], None] | None = None,
        flush_before_dispatch: bool = False,
        track_context: bool = False,
    ) -> tuple[list[Message], list[Path]]:
        """Run one LLM turn with tool-use loop until the model stops calling tools.

        Args:
            llm: LLM plugin to use for this turn.
            model: Model identifier string passed to the plugin.
            system_prompt: The agent's system prompt.
            messages: Current message history.
            tools: Tool specs exposed to the model.
            tool_dispatch: Async function dispatching tool calls to handlers.
            stream_id: Stream identifier for token events.
            agent_name: Agent name used in usage records.
            stop_after_tools: When provided and returns ``True`` after a tool
                batch, the loop exits without calling the LLM again.
            persist: When provided, called with each batch of newly appended
                messages so they can be durably logged (main ``session.jsonl``
                or a subsession file). Messages already present on entry are
                assumed already persisted and never re-emitted. The turn always
                flushes after every tool-result batch, so a completed tool call
                is durable at each turn boundary.
            flush_before_dispatch: When ``True`` (the main entry-agent turn),
                also flush the not-yet-persisted prefix — including the assistant
                message carrying the ``tool_use`` — *before* dispatching any
                tool, so the persisted transcript is never behind an in-flight
                tool call. This makes the main turn resilient to a crash or a
                client-visible side effect (e.g. ``create_new_project`` firing a
                workspace-folder reload) a tool triggers mid-dispatch; resume
                (:meth:`~._resume.ResumeMixin._resume_main_turn`) then
                re-dispatches a dangling spawn via the replay ledger and stubs
                any other dangling call as interrupted. Left ``False`` for
                sub-agent subsessions: their resume re-decides an interrupted
                leaf tool via the LLM, which requires the log to end at a clean
                turn boundary (a user ``tool_result``), so they must *not*
                flush a bare ``tool_use``. The added latency is negligible next
                to the LLM round-trip.
            track_context: When ``True`` (the shared main entry-agent turn), the
                measured prompt+output token total of each LLM call updates the
                live context gauge (the compactor's ``context_tokens``) and is
                pushed to the client. Sub-agent/titler turns leave it ``False``
                — only the main context counts toward the compaction threshold.

        Returns:
            tuple[list[Message], list[Path]]: Updated messages and (unused) files.
        """
        files_written: list[Path] = []
        tool_desc = {t.name: t.user_description for t in tools}
        tool_logger = ToolCallLogger(self._llm_logs_dir())
        persisted_upto = len(messages)

        def _flush() -> None:
            nonlocal persisted_upto
            if persist is not None and len(messages) > persisted_upto:
                persist(messages[persisted_upto:])
                persisted_upto = len(messages)

        while True:
            call_start_dt = datetime.now(tz=UTC)
            call_start = call_start_dt.isoformat()
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            thinking_signature: str | None = None
            tool_calls: list[ToolCallEvent] = []
            turn_end: TurnEnd | None = None

            await self._sink.send(
                Envelope.make_event(EVT_LLM_TURN_START, {"agent": agent_name, "model": model})
            )

            try:
                async for event in self._gateway.stream_query(
                    routing=routing,
                    plugin=llm,
                    sink=self._sink,
                    stream_id=stream_id,
                    model=model,
                    system=system_prompt,
                    messages=messages,
                    tools=tools,
                    cache_breakpoints=[0],
                ):
                    await self._emitters.handle_stream_event(event, stream_id)
                    if isinstance(event, TokenDelta):
                        text_parts.append(event.text)
                    elif isinstance(event, ThinkingDelta):
                        thinking_parts.append(event.text)
                    elif isinstance(event, ThinkingSignature):
                        thinking_signature = event.signature
                    elif isinstance(event, ToolCallEvent):
                        tool_calls.append(event)
                    elif isinstance(event, TurnEnd):
                        turn_end = event
            except asyncio.CancelledError:
                # The user clicked Stop mid-stream (see ``stop``). Whatever
                # text/thinking/tool_use had arrived so far lives only in the
                # local accumulators above and would vanish silently when this
                # coroutine unwinds, so fold it into a real (possibly partial)
                # assistant message and persist it now — same durability a
                # normal turn boundary gets — before letting the cancellation
                # continue propagating. Only for the shared main-turn call
                # (``track_context``): a subsession must leave its log ending
                # on a clean tool_result boundary (see ``flush_before_dispatch``
                # note above), so it does not get a dangling partial reply.
                await self._sink.send(Envelope.make_stream_end(stream_id))
                if track_context:
                    partial = self._partial_assistant_message(
                        text_parts, thinking_parts, thinking_signature, tool_calls
                    )
                    if partial is not None:
                        messages = messages + [partial]
                        _flush()
                    self._main_messages = messages
                raise
            except Exception:
                await self._sink.send(Envelope.make_stream_end(stream_id))
                raise

            if turn_end is not None:
                self._emitters.add_cost(turn_end.usage.usd_cost)
                call_end_dt = datetime.now(tz=UTC)
                duration_seconds = (call_end_dt - call_start_dt).total_seconds()
                await self._emitters.emit_usage(turn_end, model, duration_seconds)
                await self._transient.write_agent_record(
                    agent_name,
                    {
                        "call_start": call_start,
                        "call_end": call_end_dt.isoformat(),
                        "model": model,
                        "input_tokens": turn_end.usage.input_tokens,
                        "output_tokens": turn_end.usage.output_tokens,
                        "cache_write_tokens": turn_end.usage.cache_write_tokens,
                        "cache_read_tokens": turn_end.usage.cache_read_tokens,
                        "usd_cost": turn_end.usage.usd_cost,
                        "cumulative_usd": self._emitters.cumulative_usd,
                        "stop_reason": turn_end.stop_reason,
                    },
                )
                if track_context:
                    usage = turn_end.usage
                    # The whole prompt that was sent (uncached input + both cache
                    # tiers) plus the output the model just appended ≈ what the
                    # next call will carry as context.
                    self._compactor.context_tokens = (
                        usage.input_tokens
                        + usage.cache_read_tokens
                        + usage.cache_write_tokens
                        + usage.output_tokens
                    )
                    await self._emitters.emit_context_stats()

            thinking_text = "".join(thinking_parts)

            if not tool_calls:
                if thinking_text:
                    messages = messages + [
                        Message(
                            role="assistant",
                            content=[
                                self._thinking_block(thinking_text, thinking_signature),
                                {"type": "text", "text": "".join(text_parts) or "(no text)"},
                            ],
                        )
                    ]
                else:
                    messages = messages + [
                        Message(role="assistant", content="".join(text_parts) or "(no text)")
                    ]
                _flush()
                if track_context:
                    self._main_messages = messages
                break

            assistant_content: list[dict[str, object]] = []
            if thinking_text:
                assistant_content.append(self._thinking_block(thinking_text, thinking_signature))
            if text_parts:
                assistant_content.append({"type": "text", "text": "".join(text_parts)})
            for tc in tool_calls:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tc.tool_use_id,
                        "name": tc.tool_name,
                        "input": tc.tool_input,
                    }
                )
            messages = messages + [Message(role="assistant", content=assistant_content)]

            # Main turn only: persist the assistant message BEFORE dispatching
            # ANY tool, not just a sub-agent spawn. A tool's side effects (a
            # shell command, a workspace-folder change that reloads the client,
            # ...) can land before its result comes back, so the durable record
            # must not lag behind dispatch. Subsessions skip this — see the
            # flush_before_dispatch note above.
            if flush_before_dispatch:
                _flush()
                # Keep the in-memory main history exactly in sync with what is
                # now durable, so a cancellation during dispatch below (the
                # user clicking Stop while a tool is running) leaves
                # ``_main_messages`` — not just session.jsonl — ending on
                # this tool_use, which is what ``_has_dangling_tool_use``
                # (called from ``stop`` right after) reads.
                if track_context:
                    self._main_messages = messages

            calls = [(tc.tool_use_id, tc.tool_name, tc.tool_input) for tc in tool_calls]
            tool_results = await self._dispatch_tool_calls(
                calls, tool_dispatch, tool_desc, tool_logger, agent_name
            )
            messages = messages + [Message(role="user", content=tool_results)]

            # Persist the results too, so a durable transcript never trails a
            # completed tool call either.
            _flush()
            if track_context:
                self._main_messages = messages

            if stop_after_tools is not None and stop_after_tools():
                break

        return messages, files_written

    @staticmethod
    def _thinking_block(thinking: str, signature: str | None) -> dict[str, object]:
        """Build a persisted ``thinking`` content block for an assistant message.

        ``signature`` is Anthropic's per-block signature, required for Claude to
        accept the block back in a later request; llama.cpp never supplies one,
        so the field is simply omitted (see ``_drop_unsigned_thinking`` in
        ``kodo.llms.anthropic._cache``, which strips signature-less thinking
        blocks before they reach a Claude call).
        """
        block: dict[str, object] = {"type": "thinking", "thinking": thinking}
        if signature is not None:
            block["signature"] = signature
        return block

    def _partial_assistant_message(
        self: EngineHost,
        text_parts: list[str],
        thinking_parts: list[str],
        thinking_signature: str | None,
        tool_calls: list[ToolCallEvent],
    ) -> Message | None:
        """Build an assistant message from a stream cut short by Stop.

        Mirrors the normal end-of-turn construction (same method, just reached
        via ``CancelledError`` instead of a clean ``async for`` exit), so a
        turn interrupted mid-stream persists exactly what the client already
        rendered — no more, no less. Returns ``None`` if nothing had arrived
        yet (Stop raced the very start of the call), so the caller adds
        nothing rather than persisting an empty placeholder.
        """
        thinking_text = "".join(thinking_parts)
        text = "".join(text_parts)
        if not thinking_text and not text and not tool_calls:
            return None
        content: list[dict[str, object]] = []
        if thinking_text:
            content.append(self._thinking_block(thinking_text, thinking_signature))
        if text:
            content.append({"type": "text", "text": text})
        for tc in tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.tool_use_id,
                    "name": tc.tool_name,
                    "input": tc.tool_input,
                }
            )
        return Message(role="assistant", content=content)

    async def _dispatch_tool_calls(
        self: EngineHost,
        calls: list[tuple[str, str, dict[str, object]]],
        tool_dispatch: Callable[[str, dict[str, object], str], Awaitable[str]],
        tool_desc: dict[str, str],
        tool_logger: ToolCallLogger,
        agent_name: str,
    ) -> list[dict[str, object]]:
        """Dispatch a batch of ``(tool_use_id, name, input)`` calls in order.

        Shared by the live turn loop and the crash-resume path (which replays
        the tool calls recorded in a persisted assistant message).

        Returns:
            list[dict[str, object]]: ``tool_result`` content blocks, in order.
        """
        tool_results: list[dict[str, object]] = []
        for tool_use_id, tool_name, tool_input in calls:
            # ask_user never gets the generic tool-call card: its handler fires
            # a prompt.question request (carrying this tool_use_id) that the
            # client renders as the interactive question panel instead.
            if tool_name != "ask_user":
                payload: dict[str, object] = {
                    "tool_name": tool_name,
                    "description": tool_desc.get(tool_name, ""),
                    "tool_call_id": tool_use_id,
                }
                # run_command carries a mandatory timeout; surface it so the
                # client can render a "waiting for tool output" progress bar
                # that fills over the timeout window while the command runs.
                if tool_name == "run_command":
                    payload["timeout_seconds"] = tool_input.get("timeout")
                await self._sink.send(Envelope.make_event(EVT_AGENT_TOOL_CALL_PREP, payload))
            tc_n = tool_logger.log_invocation(tool_name, tool_input)
            # Snapshot the pre-mutation baseline of any root this tool is about
            # to write to, so the post-dispatch commit records the change as its
            # own checkpoint (see CheckpointCoordinator.prepare / .commit).
            ck_paths = await self._checkpoints.prepare(tool_name, tool_input)
            result_text = await tool_dispatch(tool_name, tool_input, tool_use_id)
            tool_logger.log_result(tool_name, tc_n, result_text)
            checkpoint = await self._checkpoints.commit(tool_name, tool_input, ck_paths)
            content = await self._finalize_tool_result(
                tool_use_id, tool_name, tool_input, result_text, checkpoint, agent_name
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            )
        return tool_results

    async def _finalize_tool_result(
        self: EngineHost,
        tool_use_id: str,
        tool_name: str,
        tool_input: dict[str, object],
        result_text: str,
        checkpoint: CheckpointRef | None = None,
        agent_name: str = _GUIDE_AGENT_NAME,
    ) -> str:
        """Normalize a tool result to its schema; persist and surface its detail.

        Returns the JSON string handed back to the LLM as the ``tool_result``
        content. The engine owns the injected ``schema_compliance`` flag (added
        by :func:`~kodo.toolspecs.normalize_output`). The full input + output is
        persisted as a Markdown doc keyed by ``tool_use_id``, and the
        customer-visible projection is pushed to the client via
        :data:`EVT_AGENT_TOOL_CALL_DETAIL`; non-compliant output additionally
        emits :data:`EVT_TOOL_INCOMPLIANT` so the VSIX can warn the user.

        ``checkpoint`` (when a file-mutating tool produced a mirror commit) is
        surfaced two ways: its ``sha`` is injected into the LLM-visible result
        (declared as ``checkpoint_sha`` in each mutating tool's output schema),
        and the full ``{root, sha, parent}`` rides the detail event out-of-band
        so the WebView can render the undo / rollback controls. The same
        ``checkpoint`` also drives the coordinator's ``record_guided_revision``
        (a tracked document's ``new_revision`` jsonl entry), and a successful
        ``document_feedback`` call with ``accept: true`` drives
        ``_finalize_document`` (the accept/review flow) — both below, after the
        client has already seen this call's own detail event.

        A tool with no matching spec (none today) passes through unchanged.
        """
        spec = _SPECS_BY_NAME.get(tool_name)
        if spec is None:
            return result_text
        try:
            raw: object = json.loads(result_text)
        except json.JSONDecodeError:
            raw = {"result": result_text}

        # A tool may smuggle a before/after diff out-of-band via an
        # undeclared "diff" key (see EditFileTool). Pop it BEFORE
        # normalize_output: it's never part of any output_schema, and leaving
        # it in would make every such call look non-compliant (extra
        # undeclared field) and leak file content into the LLM-visible result.
        diff_raw = raw.pop("diff", None) if isinstance(raw, dict) else None

        # Inject the checkpoint SHA into the result so the agent sees which
        # commit captured its change. checkpoint_sha is a declared (optional)
        # output_schema field, so normalize_output keeps it and compliance holds.
        if checkpoint is not None and isinstance(raw, dict):
            raw["checkpoint_sha"] = checkpoint.sha
            raw["checkpoint_root"] = checkpoint.root

        output, compliant = normalize_output(spec.output_schema, raw)
        content = json.dumps(output)

        markdown = render_tool_call_markdown(
            name=spec.name,
            external_name=spec.external_name,
            user_description=spec.user_description,
            security_label=spec.security_impact.label,
            compliant=compliant,
            tool_input=tool_input,
            output=output,
        )
        doc_path = self._transient.write_tool_call(tool_use_id, markdown)

        diff_detail: dict[str, object] | None = None
        if isinstance(diff_raw, dict):
            diff_detail = self._transient.write_diff_files(
                tool_use_id,
                label=str(diff_raw.get("label", "")),
                filename=str(diff_raw.get("filename", "")),
                old_content=str(diff_raw.get("old_content", "")),
                new_content=str(diff_raw.get("new_content", "")),
            )

        checkpoint_detail: dict[str, object] | None = None
        if checkpoint is not None:
            state = await self._checkpoints.mirrors.state_for(checkpoint.root)
            index = state.index_of(checkpoint.sha)
            checkpoint_detail = {
                "root": checkpoint.root,
                "sha": checkpoint.sha,
                "parent": checkpoint.parent,
                "index": index if index is not None else state.current_index,
                "undone": state.entries[index].undone if index is not None else False,
                "current_index": state.current_index,
            }

        # ask_user has no tool-call card to attach detail to — the client's
        # question panel freezes itself with the confirmed answers, and history
        # rebuild re-derives the frozen panel from the persisted call + result.
        if tool_name != "ask_user":
            await self._sink.send(
                Envelope.make_event(
                    EVT_AGENT_TOOL_CALL_DETAIL,
                    {
                        "tool_call_id": tool_use_id,
                        "file": str(doc_path) if doc_path is not None else None,
                        "rows": build_detail_rows(spec, tool_input, output),
                        "schema_compliance": compliant,
                        "success": tool_result_succeeded(output),
                        "diff": diff_detail,
                        "checkpoint": checkpoint_detail,
                    },
                )
            )
        # A new tool-call commit becomes the tip (its own index == current_index,
        # so its own RollbackBox stays hidden), but it advances current_index past
        # every *earlier* entry. Those earlier tool-call cards carry a now-stale
        # current_index from when they were the tip, so without this push their
        # "Rollback to this state" link would never appear. Broadcasting the full
        # state lets the webview refresh current_index on every card for the root.
        if checkpoint is not None:
            await self._checkpoints.push_state(
                checkpoint.root, await self._checkpoints.mirrors.state_for(checkpoint.root)
            )

        if not compliant:
            await self._sink.send(
                Envelope.make_event(
                    EVT_TOOL_INCOMPLIANT,
                    {
                        "tool_name": spec.name,
                        "external_name": spec.external_name,
                        "user_description": spec.user_description,
                    },
                )
            )

        if checkpoint is not None and tool_name in _GUIDED_STATE_TOOLS:
            await self._checkpoints.record_guided_revision(
                tool_name, tool_input, checkpoint, agent_name
            )
        elif tool_name == "document_feedback" and output.get("status") == "recorded":
            path = str(tool_input.get("path", ""))
            if bool(tool_input.get("accept", False)) and path:
                await self._finalize_document(path)

        return content

    # ------------------------------------------------------------------
    # ToolDispatcher factory
    # ------------------------------------------------------------------

    def _make_dispatcher(self: EngineHost, agent_name: str, session_id: str) -> ToolDispatcher:
        """Build a per-run tool dispatcher for *agent_name*.

        ``mode``/``project_root`` are read live from session/current-project
        state (not snapshotted) — same reasoning as ``effective_autonomous``:
        a single dispatcher serves the whole prompt.
        """
        spec = self._registry.spec_for(agent_name)
        return ToolDispatcher(
            resolver=self._make_resolver(),
            gate=self._gate,
            security=self._security,
            session=self._session,
            services=self._services,
            agent_name=agent_name,
            session_id=session_id,
            mode=self._session.effective_workflow_mode,
            project_root=(Path(self._current_project["root"]) if self._current_project else None),
            root_paths=self._root_paths(),
            util_paths=self._util_paths(),
            output_schema=spec.output_schema if spec is not None else None,
        )
