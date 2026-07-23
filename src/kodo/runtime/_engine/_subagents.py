"""Sub-agent dispatch: gated spawns, subsessions, and Author/Critic rounds.

Spawn permission is **not** wired to any one agent — there is no "only the
Guide spawns" assumption. Each agent declares the sub-agents it may spawn in
its frontmatter ``subagents:`` allow-list; the engine-driven agents
(:data:`~._shared._DIRECT_ONLY_AGENTS`) are never spawnable by anyone.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from kodo.common import Envelope
from kodo.guided_state import read_status
from kodo.llms import Message
from kodo.subagents import AgentLoadError
from kodo.tools import ProjectPathResolver, tools_for_agent
from kodo.toolspecs import SCHEMA_COMPLIANCE_KEY
from kodo.transport import (
    EVT_REVIEW_STARTED,
    EVT_REVIEW_VERDICT,
    EVT_SUBSESSION_ENDED,
    EVT_SUBSESSION_STARTED,
)

from ._proto import EngineHost
from ._shared import (
    _DEPSMGR_AGENT_NAME,
    _DIRECT_ONLY_AGENTS,
    _GUIDE_AGENT_NAME,
    _WEB_SEARCH_AGENT_NAME,
)

# Default web_search timeout when the tool's caller omits `timeout`, and the
# hard cap the tool itself already enforces before this is ever reached.
_DEFAULT_WEB_SEARCH_TIMEOUT_S = 180.0
_MAX_WEB_SEARCH_TIMEOUT_S = 600.0

_log = logging.getLogger(__name__)


class SubagentMixin:
    """Gated sub-agent spawns, subsession lifecycle, and Author/Critic."""

    # Declared so the `= None` write in _spawn_subagent doesn't let mypy infer
    # a bare-None class attribute conflicting with the EngineHost/_core one.
    _replay_subsessions: list[dict[str, object]] | None

    # ------------------------------------------------------------------
    # Subagent dispatch
    # ------------------------------------------------------------------

    def _assert_can_spawn(self: EngineHost, caller: str, *names: str) -> None:
        """Gate a spawn: ``caller`` must be allowed to invoke every name in *names*.

        Permission is **not** wired to any one agent — there is no "only the
        Guide spawns" assumption. Each agent declares the sub-agents it may
        spawn in its frontmatter ``subagents:`` allow-list (see
        :meth:`AgentRegistry.allowed_subagents`); any agent that also holds a
        spawning tool can drive them. ``_DIRECT_ONLY_AGENTS`` (engine-driven
        agents such as the session titler) are never spawnable by anyone.

        Raises:
            PermissionError: ``caller`` may not spawn one of *names* — surfaced to
                the calling LLM as the tool's ``{"error": ...}`` result.
        """
        allowed = self._registry.allowed_subagents(caller)
        for name in names:
            if name in _DIRECT_ONLY_AGENTS:
                raise PermissionError(
                    f"{name!r} is engine-driven only and cannot be spawned as a sub-agent."
                )
            if name not in allowed:
                permitted = ", ".join(sorted(allowed)) or "(none)"
                raise PermissionError(
                    f"Agent {caller!r} is not permitted to spawn sub-agent {name!r}. "
                    f"Permitted sub-agents: {permitted}."
                )

    async def _run_subagent(
        self: EngineHost, caller: str, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Gate a caller's sub-agent spawn, then run it.

        Args:
            caller: Agent making the call (the running agent — not assumed to be
                the Guide). Its frontmatter allow-list gates the spawn.
            name: Sub-agent name from the registry.
            task_input: Structured task, conforming to the sub-agent's
                ``input_schema``.

        Returns:
            dict: The sub-agent's structured result (its ``output_schema``).

        Raises:
            PermissionError: ``caller`` is not permitted to spawn ``name``.
        """
        self._assert_can_spawn(caller, name)
        return await self._spawn_subagent(name, task_input)

    async def _run_dependency_manager(
        self: EngineHost, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Spawn the dependency-management sub-agent for the ``toolchain_deps`` tool.

        Ungated by design: the tool's possession is the authorization, so the
        fixed ``toolchain_depsmgr`` agent is driven straight through
        :meth:`_spawn_subagent` without an allow-list check and without sitting
        in any caller's ``subagents:`` roster — keeping every dependency change on
        the single ``toolchain_deps`` path (which alone knows how to translate a
        missing ``DEPENDENCIES.md`` into a remediation message).

        Args:
            task_input: Structured task conforming to ``toolchain_depsmgr``'s
                ``input_schema``.

        Returns:
            dict: The sub-agent's ``output_schema`` result.
        """
        return await self._spawn_subagent(_DEPSMGR_AGENT_NAME, task_input)

    async def _run_web_search_agent(
        self: EngineHost, task_input: dict[str, object], tool_call_id: str
    ) -> dict[str, object]:
        """Run the ``web_search`` agent for the ``web_search`` tool (doc/WEB_SEARCH.md).

        Ungated by design (holding ``web_search`` is the authorization,
        mirroring :meth:`_run_dependency_manager`) — but unlike the depsmgr it
        is *not* a subsession: ``web_search`` is typically called from the
        investigator, itself a sub-agent, and subsessions do not nest.
        Instead the agent drives its own multi-round research loop via
        :meth:`_run_silent_tool_loop_turn`: no feed events or subsession
        markers, only its USD cost folded into the session total.

        ``task_input["timeout"]`` (already clamped to
        :data:`_MAX_WEB_SEARCH_TIMEOUT_S` by the tool) bounds the run; it is
        re-clamped here too so this method stays safe for any other caller.
        On a timeout with no usable result, a fallback ``{themes: [], note}``
        is synthesized rather than raising — ``web_search`` never errors the
        calling agent's turn.

        Every round in which the agent produces free text is streamed live to
        the client as ``web_search.note`` (``tool_call_id`` correlates it with
        the ``web_search`` call's own tool-call card) and buffered; once the
        run ends the full buffer is written to a best-effort sidecar file
        (:meth:`~kodo.state.TransientStore.write_web_search_notes`) so
        ``session.history`` can replay it into the "Web Search" block on
        reload. Nothing here touches ``session.jsonl``/the subsession log, so
        a crash mid-run just loses whatever wasn't written yet — acceptable,
        since this narration is a visibility aid, not part of the agent's own
        conversation (doc/WEB_SEARCH.md §6).

        Args:
            task_input: ``{query, max_themes, timeout}`` per the sub-agent's
                ``input_schema``.
            tool_call_id: The ``web_search`` tool_use block id (the calling
                agent's ``ToolContext.current_tool_use_id``), correlating the
                live notes and their sidecar file with that call's card.

        Returns:
            dict: ``{"themes": [...], "note": "..."}``.
        """
        agent = self._registry.get(_WEB_SEARCH_AGENT_NAME)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)

        timeout_raw = task_input.get("timeout")
        timeout = (
            min(float(timeout_raw), _MAX_WEB_SEARCH_TIMEOUT_S)
            if isinstance(timeout_raw, (int, float)) and timeout_raw > 0
            else _DEFAULT_WEB_SEARCH_TIMEOUT_S
        )
        deadline = time.time() + timeout

        session_id = f"web-search-{uuid.uuid4().hex}"
        dispatcher = self._make_dispatcher(_WEB_SEARCH_AGENT_NAME, session_id, deadline=deadline)
        messages: list[Message] = [
            Message(role="user", content=self._render_task_input(task_input))
        ]

        notes: list[str] = []

        async def _on_round_text(text: str) -> None:
            notes.append(text)
            await self._emitters.emit_web_search_note(tool_call_id, text)

        try:
            result = await self._run_silent_tool_loop_turn(
                routing,
                plugin,
                model_id,
                agent,
                messages,
                dispatcher,
                deadline,
                on_round_text=_on_round_text,
            )
        finally:
            if notes:
                self._transient.write_web_search_notes(tool_call_id, notes)

        if result is not None:
            themes = result.get("themes")
            note = result.get("note")
            return {
                "themes": themes if isinstance(themes, list) else [],
                "note": note if isinstance(note, str) else "",
            }
        _log.info("web_search agent produced no result within its time budget")
        return {"themes": [], "note": "Search timed out before a report could be produced."}

    @staticmethod
    def _render_task_input(task_input: dict[str, object]) -> str:
        """Render a structured ``task_input`` to the user turn the sub-agent reads.

        The instructions become the heading; every other field is listed under
        ``## Inputs`` (lists comma-joined). This is what the LLM sees; the UI
        renders the same task as a distinct *task brief* entry (see the
        ``subagent_task`` entry kind), not as a user prompt bubble.
        """
        if not task_input:
            return "(no task)"
        lines: list[str] = []
        instructions = task_input.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            lines.append("# Task\n\n" + instructions.strip())
        others = {k: v for k, v in task_input.items() if k != "instructions"}
        if others:
            input_lines = ["## Inputs"]
            for key, value in others.items():
                if isinstance(value, list):
                    rendered = ", ".join(str(x) for x in value) if value else "(none)"
                else:
                    rendered = str(value)
                input_lines.append(f"- {key}: {rendered}")
            lines.append("\n".join(input_lines))
        return "\n\n".join(lines) or "(no task)"

    async def _spawn_subagent(
        self: EngineHost, name: str, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Invoke a leaf sub-agent and return its structured result.

        The ungated spawn primitive: callers that have already passed the
        permission gate (:meth:`_run_subagent`, or
        :meth:`_run_author_critic_iteration` which gates both names up front)
        drive a subsession through here.

        Args:
            name: Sub-agent name from the registry.
            task_input: Structured task conforming to the sub-agent's input schema.

        Returns:
            dict: The structured result the sub-agent returned via ``return_result``.
        """
        if name in _DIRECT_ONLY_AGENTS:
            _log.warning("spawn_subagent: %r is engine-driven only and cannot be invoked", name)
            return {}

        # During a crash-resume replay, each run_subagent call consumes the next
        # subsession marker recorded before the crash instead of starting fresh.
        # An exhausted/empty ledger means no marker was recorded for this call
        # (crash landed before the subsession opened) — fall through to a fresh run.
        if self._replay_subsessions:
            return await self._replay_next_subsession(name)
        self._replay_subsessions = None

        subsession_id = uuid.uuid4().hex
        seed_content = self._render_task_input(task_input)
        await self._open_subsession(name, subsession_id, seed_content)

        seed = Message(role="user", content=seed_content)
        # Persisted/displayed as a distinct task brief, not a user prompt bubble.
        self._transient.append_subsession_message(
            subsession_id, seed.role, seed.content, kind="subagent_task"
        )

        output = await self._drive_subsession(name, subsession_id, [seed])
        await self._close_subsession(name, subsession_id, output)
        return output

    async def _drive_subsession(
        self: EngineHost, name: str, subsession_id: str, messages: list[Message]
    ) -> dict[str, object]:
        """Run a sub-agent's isolated turn loop and return its structured result.

        Used for both a fresh subsession and a resumed one (``messages`` already
        rehydrated from the subsession log). Sub-agent messages persist into the
        subsession file at every turn boundary so the run is resumable mid-flight.
        The structured result is whatever the agent passed to ``return_result``
        (validated against its output schema); if it never called it, a bare
        ``{schema_compliance: False}`` fallback is synthesized — there is no
        artifact index to recover a partial result from, so the caller (e.g.
        ``_run_author_critic_iteration``) just sees an empty result and treats
        it as if nothing happened.
        """
        agent = self._registry.get(name, self._session.effective_autonomous)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)
        dispatcher = self._make_dispatcher(name, subsession_id)
        leaf_tools = tools_for_agent(agent.tools)

        self._session.phase = "running"
        self._session.agent = name
        await self._emitters.emit_state()

        stream_id = uuid.uuid4().hex
        await self._emitters.emit_agent_started(name)

        def _persist(batch: list[Message]) -> None:
            for msg in batch:
                self._transient.append_subsession_message(subsession_id, msg.role, msg.content)

        await self._run_agent_turn(
            llm=plugin,
            routing=routing,
            model=model_id,
            system_prompt=agent.system_prompt,
            messages=messages,
            tools=leaf_tools,
            tool_dispatch=dispatcher.dispatch,
            stream_id=stream_id,
            agent_name=name,
            stop_after_tools=lambda: dispatcher.stop_requested,
            persist=_persist,
            on_stall=self._make_stall_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_cyclic_thinking=self._make_cyclic_thinking_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
        )

        # Safety net for a final round with zero deltas — see the matching
        # comment in ``_turns.py``'s entry-turn caller.
        self._session.awaiting_first_chunk = False
        await self._sink.send(Envelope.make_stream_end(stream_id))
        await self._emitters.emit_agent_finished(name)
        output = dispatcher.returned_output
        if output is None:
            _log.warning(
                "subsession %s (%s) ended without return_result; synthesizing fallback",
                subsession_id,
                name,
            )
            output = {SCHEMA_COMPLIANCE_KEY: False}
        _log.info(
            "subsession completed: name=%s id=%s keys=%s",
            name,
            subsession_id,
            sorted(output.keys()),
        )
        return output

    async def _open_subsession(
        self: EngineHost, name: str, subsession_id: str, task_content: str = ""
    ) -> None:
        """Record a subsession takeover: marker, active pointer, and UI divider.

        ``task_content`` is the rendered task brief; it rides the live
        ``subsession.started`` event so the client can show the same task-brief
        card it reconstructs from the seed message on reload.
        """
        display_name = self._display_name(name)
        parent_display = self._display_name(self._session.agent or _GUIDE_AGENT_NAME)
        self._transient.append_marker(
            {
                "type": "subsession_start",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
            }
        )
        self._transient.update(
            active_subsession={
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
            }
        )
        await self._sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_STARTED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "task": task_content,
                },
            )
        )

    async def _close_subsession(
        self: EngineHost, name: str, subsession_id: str, output: dict[str, object]
    ) -> None:
        """Record a subsession handing control back: marker, clear pointer, divider.

        ``output`` is the sub-agent's structured result; it is stored on the
        ``subsession_end`` marker so a crash-resume replay can return it verbatim.
        """
        display_name = self._display_name(name)
        parent_display = self._display_name(self._session.agent or _GUIDE_AGENT_NAME)
        # A sub-agent "failed" when it did not return a schema-compliant result
        # (e.g. it ended without calling return_result, so the engine synthesized
        # the {schema_compliance: False} fallback). The flag drives the red
        # <kodo_crit> handback callout in the WebView instead of the green <kodo>.
        failed = output.get(SCHEMA_COMPLIANCE_KEY) is False
        self._transient.append_marker(
            {
                "type": "subsession_end",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
                "failed": failed,
                "result": dict(output),
            }
        )
        self._transient.update(active_subsession=None)
        await self._sink.send(
            Envelope.make_event(
                EVT_SUBSESSION_ENDED,
                {
                    "subsession_id": subsession_id,
                    "agent": name,
                    "display_name": display_name,
                    "parent_display_name": parent_display,
                    "failed": failed,
                },
            )
        )

    async def _replay_next_subsession(self: EngineHost, name: str) -> dict[str, object]:
        """Consume the next pre-crash subsession marker during resume replay.

        Completed subsessions return their stored structured result immediately
        (the files they wrote are already on disk). The single active
        (un-closed) subsession is rehydrated from its log and driven to
        completion live; once consumed, replay mode ends.
        """
        assert self._replay_subsessions
        rec = self._replay_subsessions.pop(0)
        subsession_id = str(rec["subsession_id"])
        if not self._replay_subsessions:
            self._replay_subsessions = None
        if rec.get("completed"):
            _log.info(
                "Replay: subsession %s already complete; returning stored result", subsession_id
            )
            result = rec.get("result", {})
            return result if isinstance(result, dict) else {}

        _log.info("Replay: resuming active subsession %s (%s)", subsession_id, name)
        rehydrated = [
            Message(role=str(m["role"]), content=m["content"])  # type: ignore[arg-type]
            for m in self._transient.read_subsession_messages(subsession_id)
        ]
        output = await self._drive_subsession(name, subsession_id, rehydrated)
        await self._close_subsession(name, subsession_id, output)
        return output

    def _display_name(self: EngineHost, agent_name: str) -> str:
        """User-friendly name for an agent (frontmatter ``display_name`` or derived)."""
        try:
            return self._registry.get(agent_name).display_name or agent_name
        except AgentLoadError:
            return agent_name

    # ------------------------------------------------------------------
    # Author/Critic iteration
    # ------------------------------------------------------------------

    async def _run_author_critic_iteration(
        self: EngineHost,
        caller: str,
        author_name: str,
        critic_name: str,
        path: str,
        input_paths: dict[str, str],
        instructions: str,
        for_revision: bool,
    ) -> dict[str, object]:
        """Execute one Author/Critic round over a real file.

        Args:
            caller: Agent making the call. Its frontmatter allow-list must permit
                spawning both ``author_name`` and ``critic_name``; both are gated
                up front so the inner spawns can use the ungated primitive.
            author_name: Author sub-agent name.
            critic_name: Critic sub-agent name.
            path: The file to revise (required when ``for_revision``); ignored on
                a fresh round, where the Author chooses its own path.
            input_paths: Named collection of context files for the Author.
            instructions: What the Author should do this round.
            for_revision: True when ``path`` already exists and this round
                revises it.

        Returns:
            dict: ``{path, status, concerns}`` — read from the target file's
            jsonl evolution log after the Critic's ``document_feedback`` call.
            The jsonl, not the Critic's ``return_result``, is authoritative
            (the current state of a file is its log's last entry).

        Raises:
            PermissionError: ``caller`` may not spawn the author or the critic.
        """
        self._assert_can_spawn(caller, author_name, critic_name)
        author_task: dict[str, object] = {
            "instructions": instructions,
            "input_paths": input_paths,
            "for_revision_path": path if for_revision else None,
        }
        author_output = await self._spawn_subagent(author_name, author_task)
        primary_raw = author_output.get("primary_path")
        primary_path = str(primary_raw) if isinstance(primary_raw, str) and primary_raw else path

        if not primary_path:
            _log.warning("run_author_critic_iteration: %s produced no primary_path", author_name)
            return {"path": "", "status": "pending_review", "concerns": []}

        await self._sink.send(
            Envelope.make_event(
                EVT_REVIEW_STARTED,
                {
                    "reviewer_name": critic_name,
                    "target_filename": primary_path,
                    "target_type": "document",
                },
            )
        )

        critic_task: dict[str, object] = {
            "instructions": f"Review {primary_path}.",
            "input_paths": {"target": primary_path},
        }
        await self._spawn_subagent(critic_name, critic_task)

        project_root = self._require_layout().root
        resolved = ProjectPathResolver(project_root).resolve(primary_path)
        status_entry = await asyncio.to_thread(read_status, resolved, project_root)
        status = str(status_entry["status"]) if status_entry else "pending_review"
        concerns_raw = status_entry.get("concerns") if status_entry else None
        concerns = (
            [c for c in concerns_raw if isinstance(c, dict)]
            if isinstance(concerns_raw, list)
            else []
        )

        await self._sink.send(
            Envelope.make_event(
                EVT_REVIEW_VERDICT,
                {
                    "reviewer_name": critic_name,
                    "target_filename": primary_path,
                    "verdict": status,
                    "concern_count": len(concerns),
                },
            )
        )

        return {"path": primary_path, "status": status, "concerns": concerns}
