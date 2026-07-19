"""Reading the persisted session back: feed rebuild + live-context rehydration.

:class:`HistoryProjector` owns the two read paths over ``session.jsonl``:

* :meth:`history_entries` — rebuild the full client-facing feed for a resumed
  session (messages, tool-call cards, subsession dividers, compaction
  markers), in the shape the VSIX webview's ``session.history`` handler
  expects.
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

    async def history_entries(self) -> list[dict[str, object]]:
        """Rebuild the full client-facing feed for a resumed session.

        Walks the main ``session.jsonl`` in order. Message lines become
        ``user_message`` / ``assistant_response`` / ``tool_call`` entries; a
        ``subsession_start`` marker emits a takeover divider and splices the
        sub-agent's full inner transcript (read from its subsession log), and a
        ``subsession_end`` marker emits a hand-back divider. A
        ``security_rule_added`` marker (doc/SECURITY_RULES_PLAN.md §2.4/§2.7)
        replays the user's own record of a granted "always allow" rule, and an
        ``agent_stuck_critical`` marker (doc/STUCK_DETECTION.md) replays the
        stuck-watchdog's "gave up nudging" notice. This gives the WebView a
        faithful replay of who did what, including sub-agent work.

        Each ``tool_call`` entry's ``checkpoint`` (root/sha/parent/index/undone)
        is reconstructed from the persisted ``checkpoint_sha``/``checkpoint_root``
        output fields plus that root's :class:`CheckpointState` — async because
        loading a root's state touches disk; see :meth:`_checkpoint_detail`.

        Returns:
            list[dict[str, object]]: Ordered entries in the shape expected by the
            VSIX webview's ``session.history`` handler.
        """
        tool_desc = {t.name: t.user_description for t in ALL_TOOLS}
        toolcalls_dir = self._transient.toolcalls_dir
        lines = self._transient.read_session_lines()

        # Pass 1: index every tool_use_id → its (normalized) output, so the
        # tool_call entries can be rebuilt with their detail rows and file link.
        # Subsession transcripts carry their own tool calls, so include them.
        all_messages: list[dict[str, object]] = [ln for ln in lines if "role" in ln]
        for line in lines:
            if line.get("type") == "subsession_start":
                sid = str(line.get("subsession_id", ""))
                all_messages.extend(self._transient.read_subsession_messages(sid))
        results_by_id = self._tool_results_from_messages(all_messages)

        session_dir = self._transient.session_dir
        # Loaded at most once per root for this whole rebuild.
        checkpoint_states: dict[str, CheckpointState] = {}
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
            if kind == "subsession_start":
                entries.append(self._divider_entry("subsession_start", line))
                sid = str(line.get("subsession_id", ""))
                for sub in self._transient.read_subsession_messages(sid):
                    entries.extend(
                        await self._message_to_entries(
                            sub,
                            tool_desc,
                            results_by_id,
                            toolcalls_dir,
                            session_dir,
                            checkpoint_states,
                        )
                    )
            elif kind == "subsession_end":
                entries.append(self._divider_entry("subsession_end", line))
            elif kind == "compaction":
                tb = line.get("tokens_before", 0)
                ta = line.get("tokens_after", 0)
                entries.append(
                    {
                        "type": "context_compacted",
                        "summaryExcerpt": str(line.get("summary", ""))[:_COMPACTION_EXCERPT_LEN],
                        # Full summary so the reloaded divider expands to the same
                        # post-compaction context shown live.
                        "summary": str(line.get("summary", "")),
                        "tokensBefore": tb if isinstance(tb, int) else 0,
                        "tokensAfter": ta if isinstance(ta, int) else 0,
                    }
                )
            elif kind == "error":
                entries.append(
                    {
                        "type": "runtime_error",
                        "message": str(line.get("message", "")),
                        "recoverable": line.get("recoverable") is not False,
                    }
                )
            elif kind == "security_rule_added":
                entries.append(
                    {
                        "type": "security_rule_added",
                        "scope": str(line.get("scope", "")),
                        "executable": str(line.get("executable", "")),
                        "subcommand": str(line.get("subcommand", "")),
                    }
                )
            elif kind == "agent_stuck_critical":
                entries.append(
                    {"type": "agent_stuck_critical", "message": str(line.get("message", ""))}
                )
        return entries

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
