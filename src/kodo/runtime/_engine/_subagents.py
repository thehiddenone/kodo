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
from pathlib import Path

from kodo.common import Envelope
from kodo.findings import STATE_OUTSTANDING, RoundSummary, apply_findings, read_findings
from kodo.llms import Message
from kodo.subagents import AgentLoadError
from kodo.tools import document_status, root_for
from kodo.toolspecs import MAX_ROUNDS_DEFAULT, SCHEMA_COMPLIANCE_KEY
from kodo.transport import (
    EVT_REVIEW_STARTED,
    EVT_REVIEW_VERDICT,
    EVT_SUBSESSION_ENDED,
    EVT_SUBSESSION_STARTED,
)

from .._agenttools import agent_tool_specs
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

# Author/critic rounds one ``run_subagent_<author>`` call may spend. The default
# is what the Guide's prompt used to tell it to budget by hand; the hard cap
# bounds a caller that asks for an absurd number, so a single tool call can never
# turn into an unbounded spend. A loop that is not converging usually stops well
# before either (see ``_run_review_loop``'s ``not_converging`` branch).
_DEFAULT_MAX_REVIEW_ROUNDS = MAX_ROUNDS_DEFAULT
_MAX_REVIEW_ROUNDS = 10

# Closing note appended to every schema-bearing spawn's rendered Input
# Parameters section (see ``_render_task_input``). This is the sub-agent's only
# remaining prose explanation of `return_result` beyond the tool's own
# `description` (`kodo.toolspecs._return_result._DESCRIPTION`) — the registry
# used to restate it inside a `## Your Task Contract` system-prompt section,
# which is gone; this per-call section is where it lives now.
_RETURN_RESULT_REMINDER = (
    "When you finish, call `return_result` exactly once. Its `result` "
    "parameter declares the exact shape you must produce — read it there and "
    "follow it exactly."
)

_log = logging.getLogger(__name__)


def _escalation_reason(output: dict[str, object]) -> str:
    """The blocker a sub-agent escalated in *output*, or ``""`` when it did not.

    A sub-agent escalates through its own ``return_result`` (there is no
    ``escalate_blocker`` tool any more — see ``specs/_shapes.py``), and the
    signal is a **non-empty ``reason``**: the field is optional and nullable, so
    a normal result simply omits it. Emptiness is what's tested, not presence —
    ``normalize_output`` backfills missing fields with ``""``, and a model that
    volunteers ``reason: ""`` on a good result is not escalating either.
    """
    reason = output.get("reason")
    return reason.strip() if isinstance(reason, str) else ""


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
        self: EngineHost,
        caller: str,
        name: str,
        task_input: dict[str, object],
        max_rounds: int | None = None,
    ) -> dict[str, object]:
        """Gate a caller's sub-agent spawn, then run it — with its critic, if any.

        Two shapes, chosen by the *callee's* own frontmatter, never by the
        caller: a sub-agent with no ``critic:`` runs once and returns its
        result; a sub-agent that declares one runs the entire author→critic
        loop (:meth:`_run_review_loop`) and returns its result plus a ``review``
        block. The caller never names a critic and never iterates by hand.

        Args:
            caller: Agent making the call (the running agent — not assumed to be
                the Guide). Its frontmatter allow-list gates the spawn.
            name: Sub-agent name from the registry.
            task_input: Structured task, conforming to the sub-agent's
                ``input_schema``.
            max_rounds: Caller's cap on author/critic rounds, or ``None`` for
                :data:`_DEFAULT_MAX_REVIEW_ROUNDS`. Ignored when the sub-agent
                has no critic.

        Returns:
            dict: The sub-agent's structured result (its ``output_schema``),
            plus ``review`` when a critic loop ran.

        Raises:
            PermissionError: ``caller`` is not permitted to spawn ``name`` (or,
                for a reviewed sub-agent, its critic).
        """
        self._assert_can_spawn(caller, name)
        critic = self._critic_for(name)
        if not critic:
            return await self._spawn_subagent(name, task_input)
        # The critic is spawned by the engine, but on this caller's behalf, so
        # it is gated against the same allow-list — a caller may not reach a
        # sub-agent it was never granted just because an author points at it.
        self._assert_can_spawn(caller, critic)
        return await self._run_review_loop(name, critic, task_input, max_rounds)

    def _critic_for(self: EngineHost, name: str) -> str:
        """The critic paired with sub-agent *name*, or ``""`` when it has none."""
        try:
            return self._registry.get(name).critic
        except AgentLoadError:
            return ""

    async def _run_review_loop(
        self: EngineHost,
        author_name: str,
        critic_name: str,
        task_input: dict[str, object],
        max_rounds: int | None,
    ) -> dict[str, object]:
        """Drive author→critic rounds until the file is settled or the budget ends.

        One round is: spawn the author, hand its ``primary_path`` to the critic,
        let the engine apply the critic's findings to that document's
        session-scoped backlog (:meth:`_record_findings`, which also fires the
        user's acceptance gate once nothing is outstanding), then read the file's
        status back — the *stores* are authoritative, not the critic's return
        value, because the user's own review decision lands there too and can
        turn an accepted file back into one needing revision.

        The loop stops on any of four things, reported as ``review.outcome``:

        - ``accepted`` — the round left nothing outstanding and the acceptance
          flow settled the document (``accepted``/``pending_acceptance``).
        - ``escalated`` — the author returned a non-empty ``reason``: it hit a
          blocker it cannot defensibly resolve. The critic is **not** spawned
          and no further round is spent — no amount of revision fixes a blocker
          whose resolution lives outside the author (an unreconcilable
          contradiction, insufficient inputs, an exhausted cap). The escalation
          rides back on the result for the caller to act on.
        - ``max_rounds`` — the budget ran out with findings outstanding.
        - ``not_converging`` — a round closed nothing and opened nothing
          (:attr:`~kodo.findings.RoundSummary.stalled`). Stateful findings make
          this an exact no-progress signal rather than the old
          "the concern count failed to drop" arithmetic, which also fired on a
          round that fixed two problems and found two others.

        Every round sends the caller's original ``instructions`` **unchanged**,
        with ``for_revision_path`` pointing at the file from round two onward.
        Outstanding findings are no longer rendered into the task: both halves
        read them through ``get_findings`` (doc/FINDINGS.md), which is what makes
        a first pass and a tenth identical for the agents.

        Returns:
            dict: The last round's author output, plus the ``review`` block
            declared by ``run_subagent_<author>``'s output schema.
        """
        budget = max_rounds if isinstance(max_rounds, int) and max_rounds > 0 else None
        budget = min(budget or _DEFAULT_MAX_REVIEW_ROUNDS, _MAX_REVIEW_ROUNDS)
        path = str(task_input.get("for_revision_path") or "")

        author_output: dict[str, object] = {}
        summary = RoundSummary(outstanding=0, opened=0, closed=0)
        status = "pending_review"
        outcome = "not_reviewed"
        rounds = 0

        for _round in range(budget):
            rounds += 1
            round_task = dict(task_input)
            if path:
                round_task["for_revision_path"] = path
            author_output = await self._spawn_subagent(author_name, round_task, path)

            # An escalation ends the loop where it stands: the author is telling
            # its caller the blocker is not one more revision away, so sending it
            # to the critic would only spend a round producing findings nobody
            # can act on. The result carries reason/summary/options straight back.
            if _escalation_reason(author_output):
                outcome = "escalated"
                break

            primary_raw = author_output.get("primary_path")
            path = str(primary_raw) if isinstance(primary_raw, str) and primary_raw else path
            if not path:
                _log.warning("run_subagent: %s produced no primary_path", author_name)
                outcome = "not_reviewed"
                break

            status, summary = await self._run_review_round(critic_name, path)

            if status in ("accepted", "pending_acceptance"):
                outcome = "accepted"
                break
            # A round that closed nothing and opened nothing is stalled: another
            # pass is unlikely to converge, and the caller can act on the
            # outstanding backlog now rather than in five rounds.
            if summary.stalled:
                outcome = "not_converging"
                break
            outcome = "max_rounds"

        _log.info(
            "review loop finished: author=%s critic=%s rounds=%d outcome=%s status=%s "
            "outstanding=%d",
            author_name,
            critic_name,
            rounds,
            outcome,
            status,
            summary.outstanding,
        )
        return {
            **author_output,
            "review": {
                "status": status,
                "outcome": outcome,
                "rounds": rounds,
                "outstanding": summary.outstanding,
            },
        }

    async def _run_review_round(
        self: EngineHost, critic_name: str, path: str
    ) -> tuple[str, RoundSummary]:
        """Spawn *critic_name* against *path*; return its ``(status, summary)``.

        The status is read back from the document's two stores rather than from
        the critic's own return value: :meth:`_record_findings` has already
        applied the round's findings, and once nothing is outstanding it also ran
        the user's acceptance gate, whose decision is the later event and
        therefore the real current state of the file.
        """
        await self._sink.send(
            Envelope.make_event(
                EVT_REVIEW_STARTED,
                {
                    "reviewer_name": critic_name,
                    "target_filename": path,
                    "target_type": "document",
                },
            )
        )
        before = await self._findings_snapshot(path)
        await self._spawn_subagent(
            critic_name,
            {"instructions": f"Review {path}.", "input_paths": {"target": path}},
            path,
        )
        after = await self._findings_snapshot(path)
        summary = RoundSummary(
            outstanding=sum(1 for state in after.values() if state == STATE_OUTSTANDING),
            opened=sum(1 for finding_id in after if finding_id not in before),
            closed=sum(
                1
                for finding_id, state in after.items()
                if state != STATE_OUTSTANDING and before.get(finding_id) == STATE_OUTSTANDING
            ),
        )

        status = await self._document_status(path)

        await self._sink.send(
            Envelope.make_event(
                EVT_REVIEW_VERDICT,
                {
                    "reviewer_name": critic_name,
                    "target_filename": path,
                    "verdict": status,
                    "outstanding": summary.outstanding,
                    "opened": summary.opened,
                    "closed": summary.closed,
                },
            )
        )
        return status, summary

    def _findings_dir(self: EngineHost) -> Path | None:
        """This session's ``findings/`` directory, or ``None`` before one is attached.

        The single place the session-scoped findings root is derived
        (doc/FINDINGS.md §2). Note it cannot be derived from a tool's
        ``ToolContext.session_id``: inside a sub-agent run that field holds the
        *subsession* id, which is why the engine injects the directory instead.
        """
        try:
            return self._transient.session_dir / "findings"
        except (AssertionError, AttributeError):
            # No session attached yet (``session_dir`` asserts), or a bare test
            # host with no store at all. Either way there is no backlog to read,
            # and every caller already treats ``None`` as "empty".
            return None

    async def _findings_snapshot(self: EngineHost, path: str) -> dict[str, str]:
        """``{finding id: state}`` for *path* right now, or ``{}`` with no store.

        The round's ``opened``/``closed`` deltas are computed by diffing this
        before and after the critic's subsession rather than by threading a
        return value out of :meth:`_record_findings` — which runs several frames
        down inside :meth:`_drive_subsession`, and does not run at all for a
        completed subsession replayed from the ledger.
        """
        findings_dir = self._findings_dir()
        if findings_dir is None:
            return {}
        findings = await asyncio.to_thread(read_findings, findings_dir, path)
        return {f["id"]: f["state"] for f in findings}

    async def _document_status(self: EngineHost, path: str) -> str:
        """Merge *path*'s project evolution log with its session findings backlog.

        Both halves are needed since findings left ``guided_state``; the merge
        itself lives once, in :func:`kodo.tools.document_status`
        (doc/FINDINGS.md §6). An unresolvable or unbound path reads as
        ``pending_review`` — "not settled" — which is what the loop treats it as
        anyway.
        """
        try:
            resolved = self._make_resolver(self._orch_session_id).resolve(path)
        except PermissionError:
            _log.warning("review round: %r cannot be resolved", path)
            return "pending_review"
        owning_root = root_for(self._root_paths(), resolved)
        if owning_root is None:
            _log.warning("review round: %r is not under any bound root", path)
            return "pending_review"
        return await asyncio.to_thread(
            document_status, resolved, Path(owning_root.path), self._findings_dir(), path
        )

    async def _record_findings(self: EngineHost, reviewer: str, output: dict[str, object]) -> None:
        """Apply a finished critic's findings to the reviewed document's backlog.

        The engine-side half of a critic round: create the new findings, patch
        the ones it updated, close the round with a ``review_round`` entry — then,
        when nothing is left outstanding, drive the acceptance flow
        (:meth:`~._core.EngineCore._finalize_document`), which auto-accepts in
        autonomous mode or under Edit Control *Allow All* and otherwise asks the
        user to sign off.

        There is no ``accept`` field to consult: the verdict is *derived* from
        the resulting backlog, so a critic cannot report a pass while leaving
        problems open (doc/FINDINGS.md §3).

        Called from :meth:`_drive_subsession` for every agent whose frontmatter
        declares ``role: critic``, so it applies to a critic reached through a
        review loop *and* to one resumed mid-flight after a crash, but never to
        a completed subsession being replayed from the ledger (that one returns
        its stored result without re-running, and its entries are already on
        disk).

        A malformed verdict is logged and dropped rather than raised: the loop
        reads the stores for the real status, and a critic that failed to report
        leaves the backlog untouched, which the loop reads as a stalled round.
        """
        path = str(output.get("path", ""))
        if not path:
            _log.warning("critic %s returned no path; nothing recorded", reviewer)
            return
        raw = output.get("findings")
        updates = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
        findings_dir = self._findings_dir()
        if findings_dir is None:
            _log.warning("critic %s reported on %r with no session store attached", reviewer, path)
            return
        try:
            summary = await asyncio.to_thread(
                apply_findings,
                findings_dir,
                path,
                reviewer=reviewer,
                updates=updates,
            )
        except ValueError as exc:
            _log.info("critic %s findings on %r not recorded: %s", reviewer, path, exc)
            return
        _log.info(
            "critic %s reviewed %s: outstanding=%d opened=%d closed=%d",
            reviewer,
            path,
            summary.outstanding,
            summary.opened,
            summary.closed,
        )
        if summary.outstanding == 0:
            await self._finalize_document(path)

    async def _run_dependency_manager(
        self: EngineHost, task_input: dict[str, object]
    ) -> dict[str, object]:
        """Spawn the dependency-management sub-agent for the ``toolchain_deps`` tool.

        Ungated by design: the tool's possession is the authorization, so the
        fixed ``toolchain_depsmgr`` agent is driven straight through
        :meth:`_spawn_subagent` without an allow-list check and without sitting
        in any caller's ``subagents:`` allow-list — keeping every dependency change on
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
        web_search_spec = self._registry.spec_for(_WEB_SEARCH_AGENT_NAME)
        messages: list[Message] = [
            Message(
                role="user",
                content=self._render_task_input(
                    task_input, web_search_spec.input_schema if web_search_spec else None
                ),
            )
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
    def _render_param_value(value: object, indent: str = "") -> str:
        """Pretty-print one Input Parameters value as markdown, recursing into containers.

        A scalar renders inline. A flat list of scalars renders comma-joined
        (unchanged from before). A dict, or a list containing one, renders as a
        nested bullet list instead of a Python repr — task inputs regularly
        carry a labeled path collection (``input_paths: {"target": "..."}``),
        and a smaller local model reads that far more reliably as markdown
        bullets than as ``{'target': '...'}``. ``None`` (an omitted optional
        field, e.g. ``for_revision_path`` on a first round) renders as
        ``(none)`` rather than the Python literal ``None``.
        """
        if value is None:
            return "(none)"
        if isinstance(value, dict):
            if not value:
                return "(none)"
            return "\n" + "\n".join(
                f"{indent}  - **{k}**: {SubagentMixin._render_param_value(v, indent + '  ')}"
                for k, v in value.items()
            )
        if isinstance(value, list):
            if not value:
                return "(none)"
            if all(not isinstance(v, (dict, list)) for v in value):
                return ", ".join(str(v) for v in value)
            return "\n" + "\n".join(
                f"{indent}  - {SubagentMixin._render_param_value(v, indent + '  ')}" for v in value
            )
        return str(value)

    @staticmethod
    def _render_task_input(
        task_input: dict[str, object], input_schema: dict[str, object] | None = None
    ) -> str:
        """Render a structured ``task_input`` to the user turn the sub-agent reads.

        ``instructions`` becomes the ``# Task`` heading. Every other field the
        sub-agent was actually given is pretty-printed under a trailing
        ``## Input Parameters`` section — the last section of this message, and
        (since a local model's chat template concatenates system prompt and
        first user turn into one flat string) the last section of the whole
        prompt the agent's own ``.md`` file promises it. This is also the only
        place the sub-agent still sees a description of `return_result`'s job:
        the registry no longer restates the input schema in the system prompt
        (see ``AgentRegistry``'s dropped ``## Your Task Contract``), so this
        per-call section is what replaces it, populated with real values
        instead of a schema.

        Args:
            task_input: The concrete task this call is spawning the sub-agent
                with.
            input_schema: The sub-agent's declared ``input_schema`` (from its
                ``SubAgentSpec``), used only to (a) order fields the way the
                spec declares them and (b) pull each field's ``description`` so
                a smaller local model doesn't have to infer what a bare value
                means. ``None`` for a sub-agent with no spec (should not happen
                for a real spawn, but degrades gracefully — no descriptions, no
                `return_result` reminder, caller-supplied field order).

        This is what the LLM sees; the UI renders the same task as a distinct
        *task brief* entry (see the ``subagent_task`` entry kind), not as a
        user prompt bubble.
        """
        if not task_input:
            return "(no task)"
        lines: list[str] = []
        instructions = task_input.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            lines.append("# Task\n\n" + instructions.strip())
        others = {k: v for k, v in task_input.items() if k != "instructions"}
        properties_raw = input_schema.get("properties") if isinstance(input_schema, dict) else None
        properties: dict[str, object] = properties_raw if isinstance(properties_raw, dict) else {}
        ordered_keys = [k for k in properties if k in others]
        ordered_keys += [k for k in others if k not in ordered_keys]

        if input_schema is not None:
            param_lines = ["## Input Parameters"]
            if ordered_keys:
                for key in ordered_keys:
                    prop = properties.get(key)
                    description = prop.get("description") if isinstance(prop, dict) else None
                    label = f"**{key}**"
                    if isinstance(description, str) and description.strip():
                        label += f" ({description.strip()})"
                    rendered = SubagentMixin._render_param_value(others[key])
                    param_lines.append(f"- {label}: {rendered}")
            else:
                param_lines.append("(no parameters beyond the task above)")
            param_lines.append("")
            param_lines.append(_RETURN_RESULT_REMINDER)
            lines.append("\n".join(param_lines))
        elif others:
            # Defensive fallback for a sub-agent spawned with no spec on record
            # (should not happen in practice — every real spawn target is
            # schema-bearing). No descriptions, no return_result reminder.
            param_lines = ["## Input Parameters"]
            for key in ordered_keys:
                param_lines.append(f"- **{key}**: {SubagentMixin._render_param_value(others[key])}")
            lines.append("\n".join(param_lines))
        return "\n\n".join(lines) or "(no task)"

    async def _spawn_subagent(
        self: EngineHost, name: str, task_input: dict[str, object], findings_path: str = ""
    ) -> dict[str, object]:
        """Invoke a leaf sub-agent and return its structured result.

        The ungated spawn primitive: callers that have already passed the
        permission gate (:meth:`_run_subagent`, or
        :meth:`_run_review_loop`, whose names were gated up front)
        drive a subsession through here.

        Args:
            name: Sub-agent name from the registry.
            task_input: Structured task conforming to the sub-agent's input schema.
            findings_path: The document this run's review round targets, which
                binds ``get_findings``' auto-scope for the whole subsession
                (doc/FINDINGS.md §3). Empty for any spawn that is not part of a
                review round, and for an author's first pass — the tool then
                answers with an empty list rather than an error.

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
            return await self._replay_next_subsession(name, findings_path)
        self._replay_subsessions = None

        subsession_id = uuid.uuid4().hex
        spec = self._registry.spec_for(name)
        seed_content = self._render_task_input(task_input, spec.input_schema if spec else None)
        await self._open_subsession(name, subsession_id, seed_content)

        seed = Message(role="user", content=seed_content)
        # Persisted/displayed as a distinct task brief, not a user prompt bubble.
        self._transient.append_subsession_message(
            subsession_id, seed.role, seed.content, kind="subagent_task"
        )

        output = await self._drive_subsession(name, subsession_id, [seed], findings_path)
        await self._close_subsession(name, subsession_id, output)
        return output

    async def _drive_subsession(
        self: EngineHost,
        name: str,
        subsession_id: str,
        messages: list[Message],
        findings_path: str = "",
    ) -> dict[str, object]:
        """Run a sub-agent's isolated turn loop and return its structured result.

        Used for both a fresh subsession and a resumed one (``messages`` already
        rehydrated from the subsession log). Sub-agent messages persist into the
        subsession file at every turn boundary so the run is resumable mid-flight.
        The structured result is whatever the agent passed to ``return_result``
        (validated against its output schema); if it never called it, a bare
        ``{schema_compliance: False}`` fallback is synthesized — there is no
        artifact index to recover a partial result from, so the caller (e.g.
        ``_run_review_loop``) just sees an empty result and treats
        it as if nothing happened.
        """
        agent = self._registry.get(name, self._session.effective_autonomous)
        plugin, model_id, routing = await self._resolve_plugin(agent.capability)
        dispatcher = self._make_dispatcher(name, subsession_id, findings_path=findings_path)
        leaf_tools = agent_tool_specs(self._registry, agent)

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
            subsession_model_key=model_id,
            on_stall=self._make_stall_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
                dispatcher=dispatcher,
            ),
            on_cyclic_thinking=self._make_cyclic_thinking_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_think_in_tool_call=self._make_think_in_tool_call_handler(
                agent_name=name,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_tool_call_cyclic=self._make_tool_call_cyclic_handler(
                agent_name=name,
                routing=routing,
                is_entry_turn=False,
                subsession_id=subsession_id,
            ),
            on_repeated_tool_calls=self._make_repeated_tool_call_handler(
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
        # A critic's result is not a value handed back to whoever spawned it —
        # it is a set of findings against a file, and the session's findings
        # backlog is where they belong. Recording them here (rather than in the
        # critic's own toolset, as the retired ``document_feedback`` did) keeps
        # every spawn path covered: a review loop, and a critic subsession
        # resumed mid-flight after a crash. Gated on the callee's explicit
        # ``role: critic``, never inferred from the result's shape.
        if agent.is_critic:
            await self._record_findings(name, output)
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
        self._compactor.clear_subsession_context()
        await self._emitters.emit_context_stats()
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

    async def _abort_active_subsession(self: EngineHost) -> None:
        """Close out a subsession a user Stop left open mid-run.

        ``_spawn_subagent`` awaits :meth:`_drive_subsession` then
        :meth:`_close_subsession` as two sequential, unguarded calls (no
        ``try/finally``): when :meth:`~._core.WorkflowEngine.stop` cancels the
        worker task, the cancellation unwinds through ``_drive_subsession``
        and skips ``_close_subsession`` entirely, leaving
        ``_transient.active_subsession`` set, the compactor's subsession gauge
        stale, and the client's collapsible block permanently unclosed (no
        ``subsession_end`` marker/``EVT_SUBSESSION_ENDED`` ever arrives) — see
        ``stop()``, which calls this right after folding the cancellation into
        ``session.jsonl`` and before flipping ``phase`` to ``"stopped"``, so
        the client's ``subsession_ended`` handling lands before its
        ``interrupted`` one.

        Reads the closing agent/display names from ``active_subsession``
        itself (captured correctly at :meth:`_open_subsession` time) rather
        than recomputing from ``self._session.agent`` the way
        ``_close_subsession`` does — by now that field holds the *sub-agent's*
        own name, not its parent's, because ``_drive_subsession`` overwrote it
        and nothing ever restores it before a Stop can land.

        The handback is always marked ``failed`` — a user-initiated Stop is
        neither the clean finish nor the schema-compliance failure the
        ``failed`` flag otherwise distinguishes between, and the generic
        "Interrupted by user" callout that follows right after already tells
        the human what actually happened.
        """
        active = self._transient.active_subsession
        if active is None:
            return
        subsession_id = str(active.get("subsession_id", ""))
        name = str(active.get("agent", ""))
        display_name = str(active.get("display_name") or self._display_name(name))
        parent_display = str(active.get("parent_display_name") or _GUIDE_AGENT_NAME)
        self._compactor.clear_subsession_context()
        await self._emitters.emit_context_stats()
        self._transient.append_marker(
            {
                "type": "subsession_end",
                "subsession_id": subsession_id,
                "agent": name,
                "display_name": display_name,
                "parent_display_name": parent_display,
                "failed": True,
                "result": {},
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
                    "failed": True,
                },
            )
        )

    async def _replay_next_subsession(
        self: EngineHost, name: str, findings_path: str = ""
    ) -> dict[str, object]:
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
        output = await self._drive_subsession(name, subsession_id, rehydrated, findings_path)
        await self._close_subsession(name, subsession_id, output)
        return output

    def _display_name(self: EngineHost, agent_name: str) -> str:
        """User-friendly name for an agent (frontmatter ``display_name`` or derived)."""
        try:
            return self._registry.get(agent_name).display_name or agent_name
        except AgentLoadError:
            return agent_name
