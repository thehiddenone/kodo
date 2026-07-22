"""Tests for ``kodo.runtime._engine._history.HistoryProjector``.

Covers both read paths — ``history_entries`` (feed rebuild) and
``load_main_messages`` (live-context rehydration) — plus their static/module
helpers, against a small in-memory fake of ``TransientStore``.
"""

from __future__ import annotations

import re
from pathlib import Path

from kodo.runtime._checkpoints import CheckpointEntry, CheckpointState
from kodo.runtime._engine._history import HistoryProjector, _history_attachment_links

# ---------------------------------------------------------------------------
# _history_attachment_links
# ---------------------------------------------------------------------------


def test_history_attachment_links_empty_for_non_list() -> None:
    assert _history_attachment_links(None, Path("/tmp/session")) == []


def test_history_attachment_links_skips_non_dict_and_empty_stored() -> None:
    atts = ["not-a-dict", {"name": "a"}, {"name": "b", "stored": "b.bin"}]
    links = _history_attachment_links(atts, Path("/tmp/session"))
    assert links == [{"name": "b", "path": str(Path("/tmp/session") / "b.bin")}]


def test_history_attachment_links_defaults_name() -> None:
    links = _history_attachment_links([{"stored": "x.bin"}], Path("/s"))
    assert links == [{"name": "attachment", "path": str(Path("/s") / "x.bin")}]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMirrors:
    def __init__(self, states: dict[str, CheckpointState] | None = None) -> None:
        self.states = states or {}
        self.requested: list[str] = []

    async def state_for(self, root: str) -> CheckpointState:
        self.requested.append(root)
        return self.states.get(root, CheckpointState())


class _FakeCheckpoints:
    def __init__(self, states: dict[str, CheckpointState] | None = None) -> None:
        self.mirrors = _FakeMirrors(states)


class _FakeTransient:
    def __init__(self, tmp_path: Path) -> None:
        self._lines: list[dict[str, object]] = []
        # Deliberately not created on disk — read_diff_files/read_web_search_notes
        # (and doc.exists()) all treat a missing directory as "nothing captured".
        self.toolcalls_dir = tmp_path / "toolcalls"
        self.session_dir = tmp_path / "session"
        self._subsessions: dict[str, list[dict[str, object]]] = {}

    def read_session_lines(self) -> list[dict[str, object]]:
        return self._lines

    def read_subsession_messages(self, subsession_id: str) -> list[dict[str, object]]:
        return [ln for ln in self._subsessions.get(subsession_id, []) if "role" in ln]

    def read_subsession_lines(self, subsession_id: str) -> list[dict[str, object]]:
        return self._subsessions.get(subsession_id, [])


def _make_projector(tmp_path: Path, *, states: dict[str, CheckpointState] | None = None):
    transient = _FakeTransient(tmp_path)
    checkpoints = _FakeCheckpoints(states)
    projector = HistoryProjector(transient, checkpoints)  # type: ignore[arg-type]
    return projector, transient, checkpoints


# ---------------------------------------------------------------------------
# _tool_results_from_messages
# ---------------------------------------------------------------------------


def test_tool_results_from_messages_extracts_parsed_dict_results() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"ok": true}'},
                {"type": "tool_result", "tool_use_id": "tu_2", "content": "not json"},
                {"type": "tool_result", "tool_use_id": "", "content": '{"x": 1}'},
                {"type": "text", "text": "ignored"},
            ],
        },
        {"role": "assistant", "content": "plain string, ignored"},
    ]
    results = HistoryProjector._tool_results_from_messages(messages)
    assert results == {"tu_1": {"ok": True}}


def test_tool_results_from_messages_skips_non_dict_json() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "[1, 2]"}],
        }
    ]
    assert HistoryProjector._tool_results_from_messages(messages) == {}


# ---------------------------------------------------------------------------
# _divider_entry
# ---------------------------------------------------------------------------


def test_divider_entry_shape() -> None:
    marker = {
        "agent": "investigator",
        "display_name": "Investigator",
        "parent_display_name": "Guide",
        "failed": True,
        "subsession_id": "sub1",
    }
    assert HistoryProjector._divider_entry("subsession_start", marker) == {
        "type": "subsession_start",
        "agent": "investigator",
        "displayName": "Investigator",
        "parentDisplayName": "Guide",
        "failed": True,
        "subsessionId": "sub1",
    }


def test_divider_entry_defaults() -> None:
    assert HistoryProjector._divider_entry("subsession_end", {}) == {
        "type": "subsession_end",
        "agent": "",
        "displayName": "",
        "parentDisplayName": "",
        "failed": False,
        "subsessionId": "",
    }


# ---------------------------------------------------------------------------
# _ask_user_entry
# ---------------------------------------------------------------------------


def test_ask_user_entry_escalate_blocker_no_summary_returns_none() -> None:
    assert HistoryProjector._ask_user_entry("escalate_blocker", "tu_1", {}, None) is None


def test_ask_user_entry_escalate_blocker_pending() -> None:
    entry = HistoryProjector._ask_user_entry(
        "escalate_blocker", "tu_1", {"summary": "need help"}, None
    )
    assert entry == {
        "type": "ask_user",
        "toolCallId": "tu_1",
        "questions": [{"question": "need help", "kind": "single_choice", "options": []}],
        "answers": None,
    }


def test_ask_user_entry_escalate_blocker_answered() -> None:
    entry = HistoryProjector._ask_user_entry(
        "escalate_blocker", "tu_1", {"summary": "need help"}, {"user_response": "sure"}
    )
    assert entry is not None
    assert entry["answers"] == [{"selected": [], "free_text": "sure"}]


def test_ask_user_entry_escalate_blocker_bad_output_returns_none() -> None:
    entry = HistoryProjector._ask_user_entry(
        "escalate_blocker", "tu_1", {"summary": "need help"}, {"user_response": 42}
    )
    assert entry is None


def test_ask_user_entry_non_ask_tool_returns_none() -> None:
    assert HistoryProjector._ask_user_entry("run_command", "tu_1", {}, None) is None


def test_ask_user_entry_ask_user_missing_questions_returns_none() -> None:
    assert HistoryProjector._ask_user_entry("ask_user", "tu_1", {}, None) is None
    assert HistoryProjector._ask_user_entry("ask_user", "tu_1", {"questions": []}, None) is None


def test_ask_user_entry_ask_user_pending() -> None:
    questions = [{"question": "pick one", "kind": "single_choice", "options": ["a", "b"]}]
    entry = HistoryProjector._ask_user_entry("ask_user", "tu_1", {"questions": questions}, None)
    assert entry == {
        "type": "ask_user",
        "toolCallId": "tu_1",
        "questions": questions,
        "answers": None,
    }


def test_ask_user_entry_ask_user_bad_output_returns_none() -> None:
    questions = [{"question": "pick one"}]
    entry = HistoryProjector._ask_user_entry(
        "ask_user", "tu_1", {"questions": questions}, {"answers": "not-a-list"}
    )
    assert entry is None


def test_ask_user_entry_ask_user_answered() -> None:
    questions = [{"question": "pick one"}]
    answers = [{"selected": ["a"], "free_text": None}]
    entry = HistoryProjector._ask_user_entry(
        "ask_user", "tu_1", {"questions": questions}, {"answers": answers}
    )
    assert entry is not None
    assert entry["answers"] == answers


# ---------------------------------------------------------------------------
# _checkpoint_detail
# ---------------------------------------------------------------------------


async def test_checkpoint_detail_none_when_output_none(tmp_path: Path) -> None:
    projector, _t, _c = _make_projector(tmp_path)
    assert await projector._checkpoint_detail(None, {}) is None


async def test_checkpoint_detail_none_when_sha_or_root_missing(tmp_path: Path) -> None:
    projector, _t, _c = _make_projector(tmp_path)
    assert await projector._checkpoint_detail({}, {}) is None
    assert await projector._checkpoint_detail({"checkpoint_sha": "abc"}, {}) is None


async def test_checkpoint_detail_none_when_sha_not_found(tmp_path: Path) -> None:
    projector, _t, _c = _make_projector(
        tmp_path, states={"root1": CheckpointState(entries=[], current_index=-1)}
    )
    output = {"checkpoint_sha": "missing", "checkpoint_root": "root1"}
    assert await projector._checkpoint_detail(output, {}) is None


async def test_checkpoint_detail_found_and_cached(tmp_path: Path) -> None:
    entry = CheckpointEntry(
        sha="abc123", parent="parent1", label="edit", kind="tool_call", undone=True
    )
    state = CheckpointState(entries=[entry], current_index=0)
    projector, _t, checkpoints = _make_projector(tmp_path, states={"root1": state})
    output = {"checkpoint_sha": "abc123", "checkpoint_root": "root1"}
    cache: dict[str, CheckpointState] = {}

    detail = await projector._checkpoint_detail(output, cache)
    assert detail == {
        "root": "root1",
        "sha": "abc123",
        "parent": "parent1",
        "index": 0,
        "undone": True,
        "current_index": 0,
    }
    assert checkpoints.mirrors.requested == ["root1"]

    # Second call for the same root must hit the cache, not the mirror again.
    await projector._checkpoint_detail(output, cache)
    assert checkpoints.mirrors.requested == ["root1"]


# ---------------------------------------------------------------------------
# _message_to_entries
# ---------------------------------------------------------------------------


async def test_message_to_entries_subagent_task() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "user", "kind": "subagent_task", "content": "do the thing"},
        {},
        {},
        Path("."),
        Path("."),
        {},
    )
    assert entries == [{"type": "subagent_task", "content": "do the thing"}]


async def test_message_to_entries_stopped_notice() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "assistant", "kind": "stopped_notice", "content": "interrupted"},
        {},
        {},
        Path("."),
        Path("."),
        {},
    )
    assert entries == [{"type": "interrupted"}]


async def test_message_to_entries_string_user_message_with_content() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "user", "content": "hello"}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == [{"type": "user_message", "content": "hello", "attachments": []}]


async def test_message_to_entries_string_user_message_blank_no_attachments_is_dropped() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "user", "content": ""}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == []


async def test_message_to_entries_string_assistant_message() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": "hi there"}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == [{"type": "assistant_response", "content": "hi there"}]


async def test_message_to_entries_string_assistant_blank_is_dropped() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": ""}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == []


async def test_message_to_entries_non_list_non_str_content_returns_empty() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "user", "content": 42}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == []


async def test_message_to_entries_assistant_thinking_and_text() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    content = [
        {"type": "thinking", "thinking": "pondering..."},
        {"type": "text", "text": "the answer"},
    ]
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": content}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == [
        {"type": "thinking_block", "content": "pondering..."},
        {"type": "assistant_response", "content": "the answer"},
    ]


async def test_message_to_entries_user_list_content_text_only() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    content = [{"type": "tool_result", "content": "ignored"}, {"type": "text", "text": "user text"}]
    entries = await projector._message_to_entries(
        {"role": "user", "content": content}, {}, {}, Path("."), Path("."), {}
    )
    assert entries == [{"type": "user_message", "content": "user text", "attachments": []}]


async def test_message_to_entries_user_list_content_no_text_returns_empty() -> None:
    projector = HistoryProjector(_FakeTransient(Path(".")), _FakeCheckpoints())
    entries = await projector._message_to_entries(
        {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
        {},
        {},
        Path("."),
        Path("."),
        {},
    )
    assert entries == []


async def test_message_to_entries_tool_use_ask_user_renders_question_panel(tmp_path: Path) -> None:
    projector = HistoryProjector(_FakeTransient(tmp_path), _FakeCheckpoints())
    content = [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "ask_user",
            "input": {"questions": [{"question": "pick"}]},
        }
    ]
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": content}, {}, {}, tmp_path, tmp_path, {}
    )
    assert entries == [
        {
            "type": "ask_user",
            "toolCallId": "tu_1",
            "questions": [{"question": "pick"}],
            "answers": None,
        }
    ]


async def test_message_to_entries_tool_use_generic_card(tmp_path: Path) -> None:
    projector = HistoryProjector(_FakeTransient(tmp_path), _FakeCheckpoints())
    content = [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "run_command",
            "input": {"command": "ls"},
        }
    ]
    tool_desc = {"run_command": "Run a shell command"}
    results_by_id = {"tu_1": {"exit_code": 0, "stdout": "a.txt"}}
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": content}, tool_desc, results_by_id, tmp_path, tmp_path, {}
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == "tool_call"
    assert entry["toolName"] == "run_command"
    assert entry["toolCallId"] == "tu_1"
    assert entry["description"] == "Run a shell command"
    assert entry["success"] is True
    assert entry["checkpoint"] is None
    assert entry["webSearchNotes"] == []


async def test_message_to_entries_tool_use_input_not_dict_defaults_empty(tmp_path: Path) -> None:
    projector = HistoryProjector(_FakeTransient(tmp_path), _FakeCheckpoints())
    content = [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": "not-a-dict"}]
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": content}, {}, {}, tmp_path, tmp_path, {}
    )
    assert entries[0]["toolName"] == "run_command"


async def test_message_to_entries_escalate_blocker_appends_question_panel_after_card(
    tmp_path: Path,
) -> None:
    projector = HistoryProjector(_FakeTransient(tmp_path), _FakeCheckpoints())
    content = [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "escalate_blocker",
            "input": {"summary": "need a decision"},
        }
    ]
    results_by_id = {"tu_1": {"user_response": "go ahead"}}
    entries = await projector._message_to_entries(
        {"role": "assistant", "content": content}, {}, results_by_id, tmp_path, tmp_path, {}
    )
    assert len(entries) == 2
    assert entries[0]["type"] == "tool_call"
    assert entries[1]["type"] == "ask_user"
    assert entries[1]["answers"] == [{"selected": [], "free_text": "go ahead"}]


# ---------------------------------------------------------------------------
# history_entries (top-level orchestration)
# ---------------------------------------------------------------------------


async def test_history_entries_empty_session() -> None:
    projector, transient, _c = _make_projector(Path("/nonexistent-doesnt-matter"))
    assert await projector.history_entries() == []


async def test_history_entries_walks_messages_and_markers(tmp_path: Path) -> None:
    projector, transient, _c = _make_projector(tmp_path)
    transient._lines = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi!"},
        {
            "type": "compaction",
            "summary": "a" * 400,
            "tokens_before": 100,
            "tokens_after": 10,
        },
        {"type": "error", "message": "oops", "recoverable": False},
    ]

    entries = await projector.history_entries()

    assert entries[0] == {"type": "user_message", "content": "hello", "attachments": []}
    assert entries[1] == {"type": "assistant_response", "content": "hi!"}
    assert entries[2]["type"] == "context_compacted"
    assert entries[2]["tokensBefore"] == 100
    assert len(entries[2]["summaryExcerpt"]) == 280
    assert entries[3] == {"type": "runtime_error", "message": "oops", "recoverable": False}


async def test_history_entries_error_marker_recoverable_defaults_true(tmp_path: Path) -> None:
    projector, transient, _c = _make_projector(tmp_path)
    transient._lines = [{"type": "error", "message": "oops"}]
    entries = await projector.history_entries()
    assert entries == [{"type": "runtime_error", "message": "oops", "recoverable": True}]


async def test_history_entries_security_rule_added_marker(tmp_path: Path) -> None:
    projector, transient, _c = _make_projector(tmp_path)
    transient._lines = [
        {
            "type": "security_rule_added",
            "scope": "session",
            "executable": "git",
            "subcommand": "push",
            "ts": "2026-07-17T00:00:00+00:00",
        }
    ]
    entries = await projector.history_entries()
    assert entries == [
        {
            "type": "security_rule_added",
            "scope": "session",
            "executable": "git",
            "subcommand": "push",
        }
    ]


async def test_history_entries_does_not_splice_subsession_transcript(tmp_path: Path) -> None:
    """history_entries() (main log only) emits dividers, never inline content —
    that is now subsession_entries()'s job, read from its own file alone."""
    projector, transient, _c = _make_projector(tmp_path)
    transient._lines = [
        {
            "type": "subsession_start",
            "subsession_id": "sub1",
            "agent": "investigator",
            "display_name": "Investigator",
        },
        {"type": "subsession_end", "subsession_id": "sub1", "agent": "investigator"},
    ]
    transient._subsessions["sub1"] = [
        {"role": "user", "kind": "subagent_task", "content": "look into it"},
        {"role": "assistant", "content": "found it"},
    ]

    entries = await projector.history_entries()

    assert [e["type"] for e in entries] == ["subsession_start", "subsession_end"]
    assert entries[0]["subsessionId"] == "sub1"
    assert entries[1]["subsessionId"] == "sub1"


async def test_subsession_entries_reads_only_its_own_file(tmp_path: Path) -> None:
    projector, transient, _c = _make_projector(tmp_path)
    transient._subsessions["sub1"] = [
        {"role": "user", "kind": "subagent_task", "content": "look into it"},
        {"role": "assistant", "content": "found it"},
    ]

    entries = await projector.subsession_entries("sub1")

    assert entries == [
        {"type": "subagent_task", "content": "look into it"},
        {"type": "assistant_response", "content": "found it"},
    ]


async def test_subsession_entries_usage_marker_in_position(tmp_path: Path) -> None:
    """A subsession's own ``usage`` marker (its "Kodo responded in..." row)
    must render *between* its two turns, not bunched up elsewhere — the
    original hydration-ordering bug this closes."""
    projector, transient, _c = _make_projector(tmp_path)
    transient._subsessions["sub1"] = [
        {"role": "assistant", "content": "first turn"},
        {
            "type": "usage",
            "duration_seconds": 2.0,
            "last_call_tokens": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
        },
        {"role": "assistant", "content": "second turn"},
    ]

    entries = await projector.subsession_entries("sub1")
    types = [e["type"] for e in entries]

    assert types == ["assistant_response", "status_response", "assistant_response"]
    status_entry = entries[types.index("status_response")]
    assert status_entry["durationMs"] == 2000
    assert status_entry["inputTokens"] == 10
    assert status_entry["outputTokens"] == 5
    assert status_entry["contextTokens"] == 10


async def test_subsession_entries_tool_results_scoped_to_its_own_file(tmp_path: Path) -> None:
    """A subsession's own tool_result blocks feed its own results-by-id index,
    never cross-referencing the main log or another subsession's file."""
    projector, transient, _c = _make_projector(tmp_path)
    transient._subsessions["sub1"] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_9", "name": "run_command", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_9", "content": '{"exit_code": 0}'}
            ],
        },
    ]

    entries = await projector.subsession_entries("sub1")
    tool_call_entries = [e for e in entries if e.get("type") == "tool_call"]
    assert len(tool_call_entries) == 1
    assert tool_call_entries[0]["success"] is True


# ---------------------------------------------------------------------------
# full_history (one-file-at-a-time orchestration)
# ---------------------------------------------------------------------------


async def test_full_history_empty_session(tmp_path: Path) -> None:
    projector, _t, _c = _make_projector(tmp_path)
    assert await projector.full_history() == {"entries": [], "subsessions": {}}


async def test_full_history_collects_every_referenced_subsession(tmp_path: Path) -> None:
    projector, transient, _c = _make_projector(tmp_path)
    transient._lines = [
        {"role": "user", "content": "hi"},
        {"type": "subsession_start", "subsession_id": "sub1", "agent": "investigator"},
        {"type": "subsession_end", "subsession_id": "sub1", "agent": "investigator"},
    ]
    transient._subsessions["sub1"] = [{"role": "assistant", "content": "found it"}]

    history = await projector.full_history()

    assert [e["type"] for e in history["entries"]] == [
        "user_message",
        "subsession_start",
        "subsession_end",
    ]
    assert history["subsessions"] == {
        "sub1": [{"type": "assistant_response", "content": "found it"}]
    }


async def test_full_history_no_subsessions_when_none_referenced(tmp_path: Path) -> None:
    projector, transient, _c = _make_projector(tmp_path)
    transient._lines = [{"role": "user", "content": "hi"}]
    history = await projector.full_history()
    assert history == {
        "entries": [{"type": "user_message", "content": "hi", "attachments": []}],
        "subsessions": {},
    }


async def test_full_history_shares_checkpoint_cache_across_main_and_subsessions(
    tmp_path: Path,
) -> None:
    """A checkpoint root touched by both the main log and a subsession is
    only loaded once (the CheckpointState cache spans the whole rebuild)."""
    entry = CheckpointEntry(sha="abc", parent=None, label="edit", kind="tool_call", undone=False)
    state = CheckpointState(entries=[entry], current_index=0)
    projector, transient, checkpoints = _make_projector(tmp_path, states={"root1": state})
    transient._lines = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "edit_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": '{"checkpoint_sha": "abc", "checkpoint_root": "root1"}',
                }
            ],
        },
        {"type": "subsession_start", "subsession_id": "sub1", "agent": "investigator"},
        {"type": "subsession_end", "subsession_id": "sub1", "agent": "investigator"},
    ]
    transient._subsessions["sub1"] = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_2", "name": "edit_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_2",
                    "content": '{"checkpoint_sha": "abc", "checkpoint_root": "root1"}',
                }
            ],
        },
    ]

    await projector.full_history()

    assert checkpoints.mirrors.requested == ["root1"]


# ---------------------------------------------------------------------------
# load_main_messages
# ---------------------------------------------------------------------------


def test_load_main_messages_empty_session() -> None:
    projector, transient, _c = _make_projector(Path("."))
    assert projector.load_main_messages() == []


def test_load_main_messages_no_compaction_returns_all_role_lines() -> None:
    projector, transient, _c = _make_projector(Path("."))
    transient._lines = [
        {"role": "user", "content": "hi"},
        {"type": "subsession_start", "subsession_id": "x"},  # non-role line, skipped
        {"role": "assistant", "content": "hello"},
    ]
    messages = projector.load_main_messages()
    assert [(m.role, m.content) for m in messages] == [("user", "hi"), ("assistant", "hello")]


def test_load_main_messages_honors_latest_compaction_marker() -> None:
    projector, transient, _c = _make_projector(Path("."))
    transient._lines = [
        {"role": "user", "content": "old message, dropped"},
        {"type": "compaction", "summary": "the gist"},
        {"role": "user", "content": "new message, kept"},
    ]
    messages = projector.load_main_messages()
    assert len(messages) == 2
    assert "the gist" in messages[0].content
    assert messages[1].content == "new message, kept"


def test_load_main_messages_skips_compaction_with_blank_summary() -> None:
    projector, transient, _c = _make_projector(Path("."))
    transient._lines = [{"type": "compaction", "summary": ""}]
    assert projector.load_main_messages() == []


def test_load_main_messages_skips_malformed_message() -> None:
    projector, transient, _c = _make_projector(Path("."))
    transient._lines = [{"role": "user", "content": 12345}]  # not str/list
    assert projector.load_main_messages() == []


def test_load_main_messages_skips_line_missing_content_key() -> None:
    projector, transient, _c = _make_projector(Path("."))
    transient._lines = [{"role": "user"}]  # KeyError on item["content"]
    assert projector.load_main_messages() == []


def test_load_main_messages_expands_attachments_in_string_content() -> None:
    projector, transient, _c = _make_projector(Path("."))
    transient._lines = [
        {
            "role": "user",
            "content": "please review",
            "attachments": [{"id": "abc-123", "name": "a.txt", "stored": "a.txt"}],
        }
    ]
    messages = projector.load_main_messages()
    assert "please review" in messages[0].content
    assert '<ATTACHMENT ID="abc-123" filename="a.txt"/>' in messages[0].content


# ---------------------------------------------------------------------------
# _expand_persisted_attachments
# ---------------------------------------------------------------------------


def test_expand_persisted_attachments_returns_text_unchanged_without_attachments() -> None:
    projector, transient, _c = _make_projector(Path("."))
    assert projector._expand_persisted_attachments("clean text", None) == "clean text"
    assert projector._expand_persisted_attachments("clean text", []) == "clean text"


def test_expand_persisted_attachments_renders_tag_from_link() -> None:
    projector, transient, _c = _make_projector(Path("."))
    text = projector._expand_persisted_attachments(
        "clean text", [{"id": "id-1", "name": "notes.txt", "stored": "attachments/id-1__notes.txt"}]
    )
    assert text == 'clean text\n\n<ATTACHMENT ID="id-1" filename="notes.txt"/>'


def test_expand_persisted_attachments_synthesizes_id_for_legacy_link() -> None:
    # Pre-ID sessions persisted only {"name", "stored"} — the tag must still
    # render (with a freshly minted id) rather than dropping the attachment.
    projector, transient, _c = _make_projector(Path("."))
    text = projector._expand_persisted_attachments(
        "clean text", [{"name": "gone.txt", "stored": "gone.txt"}]
    )
    assert 'filename="gone.txt"' in text
    assert re.search(r'ATTACHMENT ID="[0-9a-f-]{36}"', text)


def test_expand_persisted_attachments_skips_non_dict_entries() -> None:
    projector, transient, _c = _make_projector(Path("."))
    text = projector._expand_persisted_attachments("clean text", ["not-a-dict"])
    assert "clean text" in text
