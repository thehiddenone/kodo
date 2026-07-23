"""Reading the persisted session back: feed rebuild + live-context rehydration.

:class:`HistoryProjector` owns the two read paths over ``session.jsonl``:

* :meth:`full_history` — rebuild the full client-facing feed for a resumed
  session, hydrating **one file at a time**: :meth:`history_entries` replays
  only the main ``session.jsonl`` (messages, tool-call cards, takeover/
  handback dividers, compaction markers), and :meth:`subsession_entries`
  replays exactly one subsession's own ``<id>.jsonl`` in isolation. Nothing
  ever splices a subsession's content into the main array server-side — the
  two are returned as separate collections (``{"entries", "subsessions"}``)
  and the client does the one-time, unambiguous placement (right after that
  subsession's start divider) itself. This is what lets the client replace
  its feed wholesale on every ``session.history`` delivery instead of having
  to guess, by tool-call id, what's already been merged in — see
  doc/SESSIONS.md.
* :meth:`load_main_messages` — rebuild the live LLM context (honouring the
  latest ``compaction`` marker and re-injecting stored attachments).

It writes nothing; the only non-transient dependency is the checkpoint
coordinator, for reconstructing a persisted tool call's checkpoint controls.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from kodo.llms import Message
from kodo.state import TransientStore, read_diff_files, read_web_search_notes
from kodo.toolspecs import ALL_TOOLS, build_detail_rows, tool_result_succeeded

from .._attachments import inject_attachments
from .._checkpoints import CheckpointState
from ._checkpointing import CheckpointCoordinator
from ._compaction import _COMPACTION_EXCERPT_LEN, compaction_context_message
from ._shared import _SPECS_BY_NAME

_log = logging.getLogger(__name__)


def _history_attachment_links(attachments: object, session_dir: Path) -> list[dict[str, str]]:
    """Resolve a persisted message's attachment links for the client feed.

    Each ``{"name", "stored"}`` link is turned into ``{"name", "path"}`` with an
    absolute path to the session's stored copy, so the WebView chip opens the
    durable snapshot regardless of what happened to the original file.
    """
    if not isinstance(attachments, list):
        return []
    links: list[dict[str, str]] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        stored = str(att.get("stored", ""))
        if not stored:
            continue
        links.append(
            {"name": str(att.get("name", "attachment")), "path": str(session_dir / stored)}
        )
    return links


class HistoryProjector:
    """Rebuilds the client feed and the live LLM context from ``session.jsonl``."""

    def __init__(self, transient: TransientStore, checkpoints: CheckpointCoordinator) -> None:
        self._transient = transient
        self._checkpoints = checkpoints

    async def history_entries(
        self, checkpoint_states: dict[str, CheckpointState] | None = None
    ) -> list[dict[str, object]]:
        """Rebuild the main session's client-facing feed from ``session.jsonl`` alone.

        Walks the main ``session.jsonl`` in order. Message lines become
        ``user_message`` / ``assistant_response`` / ``tool_call`` entries; a
        ``subsession_start``/``subsession_end`` marker becomes a takeover/
        hand-back divider carrying that subsession's id (``subsessionId``) —
        its inner transcript is deliberately **not** spliced in here (that
        used to make this one array an ever-growing merge of N files, which
        is what made the client's reconnect-time reconciliation so
        error-prone). Fetch a subsession's own content separately, from its
        own file, via :meth:`subsession_entries` — :meth:`full_history` is
        the usual entry point that does both and hands the client one file's
        worth of content at a time. Every other marker kind (``usage``,
        ``compaction``, ``error``, ``security_rule_added``,
        ``agent_stuck_critical``, ``agent_cyclic_thinking_critical`` — see
        :meth:`_marker_to_entries`) is rendered in its correct chronological
        position.

        Each ``tool_call`` entry's ``checkpoint`` (root/sha/parent/index/undone)
        is reconstructed from the persisted ``checkpoint_sha``/``checkpoint_root``
        output fields plus that root's :class:`CheckpointState` — async because
        loading a root's state touches disk; see :meth:`_checkpoint_detail`.

        Args:
            checkpoint_states: Optional shared cache of already-loaded root
                states, so a caller rebuilding several logs in one pass (see
                :meth:`full_history`) never loads the same root twice. A
                fresh, call-scoped cache is used when omitted.

        Returns:
            list[dict[str, object]]: Ordered entries in the shape expected by the
            VSIX webview's ``session.history`` handler.
        """
        tool_desc = {t.name: t.user_description for t in ALL_TOOLS}
        toolcalls_dir = self._transient.toolcalls_dir
        lines = self._transient.read_session_lines()
        results_by_id = self._tool_results_from_messages([ln for ln in lines if "role" in ln])

        session_dir = self._transient.session_dir
        if checkpoint_states is None:
            checkpoint_states = {}
        entries: list[dict[str, object]] = []
        for line in lines:
            if "role" in line:
                entries.extend(
                    await self._message_to_entries(
                        line,
                        tool_desc,
                        results_by_id,
                        toolcalls_dir,
                        session_dir,
                        checkpoint_states,
                    )
                )
                continue
            kind = line.get("type")
            if kind in ("subsession_start", "subsession_end"):
                entries.append(self._divider_entry(kind, line))
            else:
                entries.extend(self._marker_to_entries(line))
        return entries

    async def subsession_entries(
        self,
        subsession_id: str,
        checkpoint_states: dict[str, CheckpointState] | None = None,
    ) -> list[dict[str, object]]:
        """Rebuild one subsession's own inner transcript from its own file alone.

        The subsession-scoped twin of :meth:`history_entries`: reads only
        ``subsessions/<subsession_id>.jsonl`` and resolves that content's
        tool-call results/checkpoints purely from within it — subsessions
        cannot nest, so the only marker kinds that can appear are the
        non-divider ones :meth:`_marker_to_entries` handles, and there is
        never a need to cross-reference the main log or any other
        subsession's file.

        Args:
            subsession_id: The subsession to rebuild.
            checkpoint_states: Optional shared cache — see :meth:`history_entries`.

        Returns:
            list[dict[str, object]]: Ordered entries for this subsession alone.
        """
        tool_desc = {t.name: t.user_description for t in ALL_TOOLS}
        toolcalls_dir = self._transient.toolcalls_dir
        session_dir = self._transient.session_dir
        lines = self._transient.read_subsession_lines(subsession_id)
        results_by_id = self._tool_results_from_messages([ln for ln in lines if "role" in ln])
        if checkpoint_states is None:
            checkpoint_states = {}
        entries: list[dict[str, object]] = []
        for line in lines:
            if "role" in line:
                entries.extend(
                    await self._message_to_entries(
                        line,
                        tool_desc,
                        results_by_id,
                        toolcalls_dir,
                        session_dir,
                        checkpoint_states,
                    )
                )
            else:
                entries.extend(self._marker_to_entries(line))
        return entries

    async def full_history(self) -> dict[str, object]:
        """Rebuild the whole client-facing feed, hydrating one file at a time.

        Returns ``{"entries": [...main...], "subsessions": {id: [...]}}`` —
        the main array (dividers only, no inline splice) plus every
        subsession referenced by one of those dividers, each sourced from
        exactly its own file via :meth:`subsession_entries`. The client (the
        VSIX webview reducer) does the one-time, unambiguous placement of
        each ``subsessions[id]`` block right after that subsession's start
        divider — see doc/SESSIONS.md — so the server never needs to
        pre-flatten N files into one array, and the client never needs to
        guess what's already merged in on a reconnect.

        A single :class:`CheckpointState` cache is shared across the main
        walk and every subsession so a root touched by tool calls in more
        than one of them is only loaded once.

        Returns:
            dict[str, object]: ``{"entries", "subsessions"}`` as described above.
        """
        checkpoint_states: dict[str, CheckpointState] = {}
        entries = await self.history_entries(checkpoint_states)
        subsession_ids = [
            str(e["subsessionId"])
            for e in entries
            if e.get("type") == "subsession_start" and e.get("subsessionId")
        ]
        subsessions = {
            sid: await self.subsession_entries(sid, checkpoint_states) for sid in subsession_ids
        }
        return {"entries": entries, "subsessions": subsessions}

    @staticmethod
    def _marker_to_entries(line: dict[str, object]) -> list[dict[str, object]]:
        """Convert one non-message, non-divider marker line to feed entries.

        Shared by the main-log walk and the subsession splice in
        :meth:`history_entries`: every marker kind here can happen inside a
        subsession's own run just as easily as at the top level (a usage
        stat, an error, a granted security rule, the stuck watchdog giving
        up) — ``subsession_start``/``subsession_end`` are the only kinds that
        can't, and stay handled by the caller since subsessions never nest.
        """
        kind = line.get("type")
        if kind == "compaction":
            tb = line.get("tokens_before", 0)
            ta = line.get("tokens_after", 0)
            return [
                {
                    "type": "context_compacted",
                    "summaryExcerpt": str(line.get("summary", ""))[:_COMPACTION_EXCERPT_LEN],
                    # Full summary so the reloaded divider expands to the same
                    # post-compaction context shown live.
                    "summary": str(line.get("summary", "")),
                    "tokensBefore": tb if isinstance(tb, int) else 0,
                    "tokensAfter": ta if isinstance(ta, int) else 0,
                }
            ]
        if kind == "error":
            return [
                {
                    "type": "runtime_error",
                    "message": str(line.get("message", "")),
                    "recoverable": line.get("recoverable") is not False,
                }
            ]
        if kind == "security_rule_added":
            return [
                {
                    "type": "security_rule_added",
                    "scope": str(line.get("scope", "")),
                    "executable": str(line.get("executable", "")),
                    "subcommand": str(line.get("subcommand", "")),
                }
            ]
        if kind == "agent_stuck_critical":
            return [{"type": "agent_stuck_critical", "message": str(line.get("message", ""))}]
        if kind == "agent_cyclic_thinking_critical":
            return [
                {
                    "type": "agent_cyclic_thinking_critical",
                    "message": str(line.get("message", "")),
                }
            ]
        if kind == "usage":
            tokens = line.get("last_call_tokens")
            tokens = tokens if isinstance(tokens, dict) else {}
            input_tokens = int(tokens.get("input") or 0)
            cache_read = int(tokens.get("cache_read") or 0)
            cache_write = int(tokens.get("cache_write") or 0)
            duration = line.get("duration_seconds")
            duration_seconds = duration if isinstance(duration, (int, float)) else 0.0
            return [
                {
                    "type": "status_response",
                    "durationMs": round(duration_seconds * 1000),
                    "inputTokens": input_tokens,
                    "outputTokens": int(tokens.get("output") or 0),
                    "contextTokens": input_tokens + cache_read + cache_write,
                }
            ]
        return []

    @staticmethod
    def _tool_results_from_messages(
        messages: list[dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        """Map ``tool_use_id`` → parsed tool output across persisted messages.

        Tool outputs live in ``tool_result`` blocks of user messages; the
        content is the normalized JSON string the engine stored at dispatch.
        """
        results: dict[str, dict[str, object]] = {}
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id", ""))
                raw_content = block.get("content")
                if not tool_use_id or not isinstance(raw_content, str):
                    continue
                try:
                    parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    results[tool_use_id] = parsed
        return results

    @staticmethod
    def _divider_entry(kind: str, marker: dict[str, object]) -> dict[str, object]:
        return {
            "type": kind,
            "agent": str(marker.get("agent", "")),
            "displayName": str(marker.get("display_name", "")),
            "parentDisplayName": str(marker.get("parent_display_name", "")),
            "failed": marker.get("failed") is True,
            # Keys :meth:`subsession_entries`'s output to this divider — read
            # by the client to splice that subsession's content in right
            # after a "start" divider (see :meth:`full_history`).
            "subsessionId": str(marker.get("subsession_id", "")),
        }

    async def _message_to_entries(
        self,
        msg: dict[str, object],
        tool_desc: dict[str, str],
        results_by_id: dict[str, dict[str, object]],
        toolcalls_dir: Path,
        session_dir: Path,
        checkpoint_states: dict[str, CheckpointState],
    ) -> list[dict[str, object]]:
        """Convert one persisted ``{role, content}`` line to client feed entries."""
        role = msg.get("role")
        content = msg.get("content")
        out: list[dict[str, object]] = []
        # A subsession's seed task is a user-role message tagged ``subagent_task``;
        # render it as a distinct task brief, never as the user's prompt bubble.
        if msg.get("kind") == "subagent_task":
            out.append(
                {"type": "subagent_task", "content": content if isinstance(content, str) else ""}
            )
            return out
        # The LLM-only "you were stopped" note _persist_interrupted_turn
        # appends (see _STOPPED_TURN_NOTICE) — replay it as the same red
        # callout the live client shows on the Stop itself, not as a user
        # message the human never actually typed.
        if msg.get("kind") == "stopped_notice":
            out.append({"type": "interrupted"})
            return out
        # The stuck-watchdog's continuation nudge (doc/STUCK_DETECTION.md) —
        # a real, LLM-visible "please continue" turn, but replayed as a
        # distinct feed entry (not a fake user-typed bubble) carrying the
        # client-only explanation of *why* Kōdo sent it.
        if msg.get("kind") == "agent_unstuck_nudge":
            detail = msg.get("detail")
            detail = detail if isinstance(detail, dict) else {}
            reasons = detail.get("reasons")
            out.append(
                {
                    "type": "agent_unstuck_nudge",
                    "note": str(detail.get("note", "")),
                    "reasons": [str(r) for r in reasons] if isinstance(reasons, list) else [],
                    "mode": str(detail.get("mode", "")),
                }
            )
            return out
        # The mid-stream cyclic-thinking detector's strike-1 notice
        # (doc/STUCK_DETECTION.md §2.7) — like the nudge above, a real,
        # LLM-visible turn, replayed as a distinct feed entry. Unlike
        # stopped_notice's fixed client-side string, the persisted content is
        # passed straight through, so the wording stays single-sourced in
        # WatchdogMixin._CYCLIC_THINKING_NOTICE rather than duplicated here.
        if msg.get("kind") == "cyclic_thinking_notice":
            out.append(
                {
                    "type": "cyclic_thinking_notice",
                    "message": content if isinstance(content, str) else "",
                }
            )
            return out
        if isinstance(content, str):
            if role == "user":
                atts = _history_attachment_links(msg.get("attachments"), session_dir)
                if content or atts:
                    out.append({"type": "user_message", "content": content, "attachments": atts})
            elif role == "assistant" and content:
                out.append({"type": "assistant_response", "content": content})
            return out
        if not isinstance(content, list):
            return out
        if role == "assistant":
            thinking_text = "".join(
                str(b.get("thinking", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "thinking"
            )
            if thinking_text:
                out.append({"type": "thinking_block", "content": thinking_text})
            text = "".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                out.append({"type": "assistant_response", "content": text})
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name", ""))
                    tool_use_id = str(block.get("id", ""))
                    tool_input = block.get("input")
                    if not isinstance(tool_input, dict):
                        tool_input = {}
                    output = results_by_id.get(tool_use_id)
                    # ask_user renders as the dedicated question panel, not a
                    # generic tool-call card (matching the live suppression in
                    # _dispatch_tool_calls). An error result (validation
                    # failure, or a legacy single-question call) falls through
                    # to the generic card so nothing is silently hidden.
                    if name == "ask_user":
                        ask_entry = self._ask_user_entry(name, tool_use_id, tool_input, output)
                        if ask_entry is not None:
                            out.append(ask_entry)
                            continue
                    spec = _SPECS_BY_NAME.get(name)
                    rows = build_detail_rows(spec, tool_input, output) if spec is not None else []
                    doc = toolcalls_dir / f"{tool_use_id}.md"
                    diff = read_diff_files(toolcalls_dir, tool_use_id)
                    checkpoint = await self._checkpoint_detail(output, checkpoint_states)
                    entry: dict[str, object] = {
                        "type": "tool_call",
                        "toolName": name,
                        "description": tool_desc.get(name, ""),
                        "toolCallId": tool_use_id,
                        "rows": rows,
                        "detailFile": str(doc) if doc.exists() else None,
                        "schemaCompliance": (
                            output.get("schema_compliance") if output is not None else None
                        ),
                        "success": tool_result_succeeded(output),
                        "diff": (
                            {
                                "label": diff["label"],
                                "prevPath": diff["prev_path"],
                                "newPath": diff["new_path"],
                            }
                            if diff is not None
                            else None
                        ),
                        "checkpoint": checkpoint,
                        # Live narration the web_search agent produced while
                        # running (doc/WEB_SEARCH.md §6); [] for every other
                        # tool and for a web_search call that never flushed one
                        # (e.g. aborted mid-run — acceptable, see the sidecar's
                        # own docstring).
                        "webSearchNotes": (
                            read_web_search_notes(toolcalls_dir, tool_use_id)
                            if name == "web_search"
                            else []
                        ),
                    }
                    out.append(entry)
                    # escalate_blocker rides the question gate with the user's
                    # free-text response in interactive mode; replay it as a
                    # question panel *after* its card, matching the live order
                    # (card at dispatch, panel when the gate fires).
                    if name == "escalate_blocker":
                        esc_entry = self._ask_user_entry(name, tool_use_id, tool_input, output)
                        if esc_entry is not None:
                            out.append(esc_entry)
        elif role == "user":
            text = "".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if text:
                out.append({"type": "user_message", "content": text, "attachments": []})
        return out

    @staticmethod
    def _ask_user_entry(
        name: str,
        tool_use_id: str,
        tool_input: dict[str, object],
        output: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Rebuild an ``ask_user`` call as a question-panel history entry.

        The panel is derived entirely from the persisted ``tool_use`` input
        (the questions) plus its ``tool_result`` (the confirmed answers) — no
        extra session content is stored, so nothing beyond the call + result
        ever reaches LLM context. ``answers`` is ``None`` while the call is
        still dangling (crash-resume re-drives it and the client re-attaches
        the live request by ``toolCallId``).

        ``escalate_blocker`` also rides the question gate (one free-text-only
        question carrying its summary); its panel is synthesized from the
        persisted ``summary`` input and ``user_response`` output and rendered
        *alongside* its generic card, not instead of it.

        Returns ``None`` when this block is not a renderable question panel
        (malformed input, an error result, or an autonomous-mode escalation
        that never asked) so the caller falls back to the card alone.
        """
        if name == "escalate_blocker":
            summary = str(tool_input.get("summary", ""))
            if not summary:
                return None
            if output is not None and not isinstance(output.get("user_response"), str):
                return None
            return {
                "type": "ask_user",
                "toolCallId": tool_use_id,
                "questions": [{"question": summary, "kind": "single_choice", "options": []}],
                "answers": (
                    [{"selected": [], "free_text": output.get("user_response") or None}]
                    if output is not None
                    else None
                ),
            }
        if name != "ask_user":
            return None
        questions = tool_input.get("questions")
        if not isinstance(questions, list) or not questions:
            return None
        if output is not None and not isinstance(output.get("answers"), list):
            return None
        return {
            "type": "ask_user",
            "toolCallId": tool_use_id,
            "questions": questions,
            "answers": output.get("answers") if output is not None else None,
        }

    async def _checkpoint_detail(
        self,
        output: dict[str, object] | None,
        checkpoint_states: dict[str, CheckpointState],
    ) -> dict[str, object] | None:
        """Reconstruct a persisted tool call's checkpoint dict for the history feed.

        ``checkpoint_sha``/``checkpoint_root`` are the only checkpoint data
        actually persisted in ``session.jsonl`` (injected at the turn loop's
        ``_finalize_tool_result``); ``parent``/``index``/``undone`` are looked
        up from that root's :class:`CheckpointState` (cached in
        *checkpoint_states*, populated at most once per root per
        :meth:`history_entries` call). Fails open to ``None`` — same as when
        there's no mirror at all — if the sha can't be found (e.g. an
        externally deleted ``state.json``).
        """
        if output is None:
            return None
        sha = output.get("checkpoint_sha")
        root = output.get("checkpoint_root")
        if not isinstance(sha, str) or not sha or not isinstance(root, str) or not root:
            return None
        state = checkpoint_states.get(root)
        if state is None:
            state = await self._checkpoints.mirrors.state_for(root)
            checkpoint_states[root] = state
        index = state.index_of(sha)
        if index is None:
            return None
        entry = state.entries[index]
        return {
            "root": root,
            "sha": sha,
            "parent": entry.parent,
            "index": index,
            "undone": entry.undone,
            "current_index": state.current_index,
        }

    def load_main_messages(self) -> list[Message]:
        """Rebuild the live LLM context from ``session.jsonl``.

        Honours the latest compaction marker: the live LLM context is the
        compacted summary block plus every message appended after that marker.
        Lines before it remain in session.jsonl as audit history (and are still
        replayed into the client feed by :meth:`history_entries`), but are never
        resent to the model. With no marker this is the full message history.
        """
        lines = self._transient.read_session_lines()
        last_compaction = -1
        for i, line in enumerate(lines):
            if line.get("type") == "compaction":
                last_compaction = i

        messages: list[Message] = []
        if last_compaction >= 0:
            summary = str(lines[last_compaction].get("summary", ""))
            if summary:
                messages.append(compaction_context_message(summary))

        for item in lines[last_compaction + 1 :]:
            if "role" not in item:
                continue
            try:
                role = str(item["role"])
                content = item["content"]
                if isinstance(content, str):
                    content = self._expand_persisted_attachments(content, item.get("attachments"))
                if isinstance(content, (str, list)):
                    messages.append(Message(role=role, content=content))
            except (KeyError, TypeError):
                _log.warning("Skipping malformed message in session.jsonl")
        return messages

    def _expand_persisted_attachments(self, clean_text: str, attachments: object) -> str:
        """Rebuild a persisted user message's attachment manifest.

        ``session.jsonl`` stores only the clean prompt plus attachment links
        (``id``, ``name``, ``stored``); on resume the LLM context must match
        what was sent originally, so each link is turned back into its
        ``<ATTACHMENT>`` tag with the same layout used at submit time
        (:func:`inject_attachments`) — content is fetched on demand via the
        ``read_attachment`` tool, never re-read here. A link from before
        attachment IDs existed (no ``id`` key) gets a freshly minted one so the
        tag still renders; its underlying file predates the ID-keyed naming
        scheme, so ``read_attachment`` will report it unavailable if the model
        asks for it.
        """
        if not isinstance(attachments, list) or not attachments:
            return clean_text
        items: list[tuple[str, str]] = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            name = str(att.get("name", "attachment"))
            attachment_id = str(att.get("id") or uuid.uuid4())
            items.append((attachment_id, name))
        return inject_attachments(clean_text, items)
