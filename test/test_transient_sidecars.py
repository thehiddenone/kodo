"""Behaviour tests for TransientStore's sidecar files, subsession logs and edge cases.

Covers the parts of :class:`kodo.state.TransientStore` not exercised by
``test_transient.py``: the per-tool-call sidecars (diff pairs, web-search
notes) and their free-function readers, subsession logs and markers, the
unattached-store no-op paths, best-effort write failures, rule revocation,
sampling overrides, and resilience to malformed on-disk state.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kodo.state import (
    TransientStore,
    new_session_id,
    read_diff_files,
    read_web_search_notes,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def kodo_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".kodo"
    d.mkdir()
    return d


@pytest.fixture()
def store(kodo_dir: Path) -> TransientStore:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    return s


def _resume(kodo_dir: Path, session_id: str = "1748792400") -> TransientStore:
    s = TransientStore(kodo_dir)
    s.attach_session(session_id, resumed=True)
    return s


def _read_transient(store: TransientStore) -> dict[str, object]:
    return json.loads((store.session_dir / "transient.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# new_session_id
# ---------------------------------------------------------------------------


def test_new_session_id_is_current_posix_seconds() -> None:
    before = int(time.time())
    sid = new_session_id()
    after = int(time.time())
    assert sid.isdigit()
    assert before <= int(sid) <= after


# ---------------------------------------------------------------------------
# Directory properties
# ---------------------------------------------------------------------------


def test_directory_properties_live_under_session_dir(store: TransientStore) -> None:
    assert store.subsessions_dir == store.session_dir / "subsessions"
    assert store.toolcalls_dir == store.session_dir / "toolcalls"
    assert store.attachments_dir == store.session_dir / "attachments"
    assert store.subsessions_dir.is_dir()
    assert store.toolcalls_dir.is_dir()


def test_attachment_abs_path_points_at_stored_copy(store: TransientStore) -> None:
    result = store.store_attachment("notes.txt", "hello")
    assert result is not None
    _, rel = result
    abs_path = Path(store.attachment_abs_path(rel))
    assert abs_path.read_text(encoding="utf-8") == "hello"
    assert abs_path.parent == store.attachments_dir


def test_store_attachment_falls_back_to_generic_name_for_empty_basename(
    store: TransientStore,
) -> None:
    result = store.store_attachment("", "x")
    assert result is not None
    attachment_id, rel = result
    assert rel == f"attachments/{attachment_id}__attachment"


# ---------------------------------------------------------------------------
# Unattached store: every write is a silent no-op, every read is empty
# ---------------------------------------------------------------------------


def test_unattached_store_writes_are_noops(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)

    assert s.store_attachment("a.txt", "x") is None
    assert s.write_tool_call("tu_1", "# doc") is None
    assert s.write_diff_files("tu_1", "a.py", "a.py", "old", "new") is None
    s.write_web_search_notes("tu_1", ["note"])
    s.append_message("user", "hi")
    s.append_marker({"type": "x"})
    s.append_subsession_message("sub", "user", "hi")
    s.append_subsession_marker("sub", {"type": "usage"})

    assert s.read_session_lines() == []
    assert s.read_messages() == []
    assert s.read_subsession_lines("sub") == []
    assert s.read_subsession_messages("sub") == []
    assert not (kodo_dir / "sessions").exists()
    assert s.last_modified == ""


def test_unattached_store_mutators_update_memory_only(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)

    s.set_sampling("q4", {"temperature": 0.2})
    s.add_security_rule("git", "status")
    s.remove_security_rule("git", "status")
    s.add_security_path_rule("cat", "/etc/hosts")
    s.remove_security_path_rule("cat", "/etc/hosts")
    s.set_session_name("Fresh Name")

    assert s.sampling == {"q4": {"temperature": 0.2}}
    assert s.security_rules == frozenset()
    assert s.security_path_rules == frozenset()
    assert s.session_name == "Fresh Name"
    assert not (kodo_dir / "sessions").exists()


# ---------------------------------------------------------------------------
# Tool-call document
# ---------------------------------------------------------------------------


def test_write_tool_call_failure_returns_none(store: TransientStore) -> None:
    # A directory squatting on the target filename makes the write fail.
    (store.toolcalls_dir / "tu_bad.md").mkdir()
    assert store.write_tool_call("tu_bad", "# doc") is None


def test_store_attachment_failure_returns_none(store: TransientStore) -> None:
    # A plain file where the attachments directory should go blocks mkdir.
    store.attachments_dir.write_text("not a dir", encoding="utf-8")
    assert store.store_attachment("a.txt", "x") is None


# ---------------------------------------------------------------------------
# Diff files
# ---------------------------------------------------------------------------


def test_write_diff_files_round_trips_through_read_diff_files(store: TransientStore) -> None:
    link = store.write_diff_files("tu_1", "src/pkg/bar.py", "pkg/bar.py", "old\n", "new\n")

    assert link is not None
    prev_path = Path(str(link["prev_path"]))
    new_path = Path(str(link["new_path"]))
    assert link["label"] == "src/pkg/bar.py"
    assert prev_path.name == "bar_prev.py"
    assert new_path.name == "bar.py"
    assert prev_path.read_text(encoding="utf-8") == "old\n"
    assert new_path.read_text(encoding="utf-8") == "new\n"

    assert read_diff_files(store.toolcalls_dir, "tu_1") == link


def test_write_diff_files_bumps_last_modified(store: TransientStore) -> None:
    before = store.last_modified
    store.write_diff_files("tu_1", "a.py", "a.py", "", "x")
    assert store.last_modified > before


def test_write_diff_files_failure_returns_none(store: TransientStore) -> None:
    (store.toolcalls_dir / "tu_1_diff").write_text("squatter", encoding="utf-8")
    assert store.write_diff_files("tu_1", "a.py", "a.py", "", "x") is None


def test_read_diff_files_none_when_nothing_captured(tmp_path: Path) -> None:
    assert read_diff_files(tmp_path, "tu_missing") is None


def test_read_diff_files_none_on_malformed_meta(store: TransientStore) -> None:
    store.write_diff_files("tu_1", "a.py", "a.py", "", "x")
    (store.toolcalls_dir / "tu_1_diff" / "meta.json").write_text("{nope", encoding="utf-8")
    assert read_diff_files(store.toolcalls_dir, "tu_1") is None


def test_read_diff_files_none_when_a_version_is_missing(store: TransientStore) -> None:
    link = store.write_diff_files("tu_1", "a.py", "a.py", "", "x")
    assert link is not None
    Path(str(link["prev_path"])).unlink()
    assert read_diff_files(store.toolcalls_dir, "tu_1") is None


def test_read_diff_files_label_defaults_to_filename(store: TransientStore) -> None:
    store.write_diff_files("tu_1", "a.py", "a.py", "", "x")
    meta = store.toolcalls_dir / "tu_1_diff" / "meta.json"
    meta.write_text(json.dumps({"filename": "a.py"}), encoding="utf-8")

    link = read_diff_files(store.toolcalls_dir, "tu_1")
    assert link is not None
    assert link["label"] == "a.py"


# ---------------------------------------------------------------------------
# Web-search notes
# ---------------------------------------------------------------------------


def test_web_search_notes_round_trip(store: TransientStore) -> None:
    before = store.last_modified
    store.write_web_search_notes("tu_ws", ["Searching", "Reading results"])

    assert read_web_search_notes(store.toolcalls_dir, "tu_ws") == ["Searching", "Reading results"]
    assert store.last_modified > before


def test_web_search_notes_empty_when_absent(tmp_path: Path) -> None:
    assert read_web_search_notes(tmp_path, "tu_none") == []


def test_web_search_notes_empty_on_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "tu_ws_websearch_notes.json").write_text("[oops", encoding="utf-8")
    assert read_web_search_notes(tmp_path, "tu_ws") == []


def test_web_search_notes_empty_when_not_a_list(tmp_path: Path) -> None:
    (tmp_path / "tu_ws_websearch_notes.json").write_text('{"a": 1}', encoding="utf-8")
    assert read_web_search_notes(tmp_path, "tu_ws") == []


def test_web_search_notes_items_coerced_to_str(tmp_path: Path) -> None:
    (tmp_path / "tu_ws_websearch_notes.json").write_text('["a", 2]', encoding="utf-8")
    assert read_web_search_notes(tmp_path, "tu_ws") == ["a", "2"]


def test_write_web_search_notes_failure_is_swallowed(store: TransientStore) -> None:
    (store.toolcalls_dir / "tu_ws_websearch_notes.json").mkdir()
    before = store.last_modified

    store.write_web_search_notes("tu_ws", ["note"])

    assert store.last_modified == before


# ---------------------------------------------------------------------------
# Main-log kind/detail and markers
# ---------------------------------------------------------------------------


def test_append_message_records_kind_and_detail(store: TransientStore) -> None:
    store.append_message(
        "user", "keep going", top_agent="guide", kind="nudge", detail={"ui_text": "Nudged"}
    )

    (line,) = store.read_messages()
    assert line["kind"] == "nudge"
    assert line["detail"] == {"ui_text": "Nudged"}
    assert line["top_agent"] == "guide"
    assert "id" in line and "ts" in line


def test_read_messages_skips_markers_and_malformed_lines(store: TransientStore) -> None:
    store.append_message("user", "hi")
    store.append_marker({"type": "usage", "tokens": 3})
    with store.session_log_path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n\n")
    store.append_message("assistant", "hello")

    lines = store.read_session_lines()
    assert [ln.get("type") for ln in lines] == [None, "usage", None]
    assert [m["content"] for m in store.read_messages()] == ["hi", "hello"]


# ---------------------------------------------------------------------------
# Subsession logs
# ---------------------------------------------------------------------------


def test_subsession_messages_and_markers_are_isolated(store: TransientStore) -> None:
    store.append_subsession_message(
        "sub1", "user", "task", kind="subagent_task", detail={"reasons": []}
    )
    store.append_subsession_marker("sub1", {"type": "usage", "tokens": 5})
    store.append_subsession_message("sub1", "assistant", "done")

    lines = store.read_subsession_lines("sub1")
    assert [ln.get("type") for ln in lines] == [None, "usage", None]
    messages = store.read_subsession_messages("sub1")
    assert [m["content"] for m in messages] == ["task", "done"]
    assert messages[0]["kind"] == "subagent_task"
    assert messages[0]["detail"] == {"reasons": []}
    assert "kind" not in messages[1]

    assert store.read_session_lines() == []
    assert store.read_subsession_lines("other") == []


def test_subsession_marker_recreates_missing_subsessions_dir(store: TransientStore) -> None:
    store.subsessions_dir.rmdir()
    store.append_subsession_marker("sub1", {"type": "error"})
    assert store.read_subsession_lines("sub1")[0]["type"] == "error"


# ---------------------------------------------------------------------------
# Pending fields / active subsession via update()
# ---------------------------------------------------------------------------


def test_pending_fields_and_active_subsession_persist_and_resume(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    s.update(
        pending_prompt={"kind": "approval", "summary": "s"},
        pending_edit_review="tu_9",
        active_subsession={"subsession_id": "sub1", "agent": "coder"},
    )

    assert s.pending_prompt == {"kind": "approval", "summary": "s"}
    assert s.pending_edit_review == "tu_9"
    assert s.active_subsession == {"subsession_id": "sub1", "agent": "coder"}

    resumed = _resume(kodo_dir)
    assert resumed.pending_prompt == {"kind": "approval", "summary": "s"}
    assert resumed.pending_edit_review == "tu_9"
    assert resumed.active_subsession == {"subsession_id": "sub1", "agent": "coder"}


def test_pending_fields_cleared_by_explicit_none(store: TransientStore) -> None:
    store.update(pending_prompt={"kind": "approval"}, pending_edit_review="tu_9")
    store.update(pending_prompt=None, pending_edit_review=None, active_subsession=None)

    assert store.pending_prompt is None
    assert store.pending_edit_review is None
    assert store.active_subsession is None
    data = _read_transient(store)
    assert data["pending_prompt"] is None
    assert data["pending_edit_review"] is None


# ---------------------------------------------------------------------------
# Rule revocation
# ---------------------------------------------------------------------------


def test_remove_security_rule_persists(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    s.add_security_rule("git", "status")
    s.add_security_rule("git", "log")

    assert s.remove_security_rule("git", "status") == frozenset({("git", "log")})
    assert _resume(kodo_dir).security_rules == frozenset({("git", "log")})


def test_remove_absent_security_rule_is_noop(store: TransientStore) -> None:
    store.add_security_rule("git", "log")
    assert store.remove_security_rule("npm", "install") == frozenset({("git", "log")})


def test_remove_security_path_rule_persists(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    s.add_security_path_rule("cat", "/a")
    s.add_security_path_rule("cat", "/b")

    assert s.remove_security_path_rule("cat", "/a") == frozenset({("cat", "/b")})
    assert _resume(kodo_dir).security_path_rules == frozenset({("cat", "/b")})


# ---------------------------------------------------------------------------
# Sampling overrides
# ---------------------------------------------------------------------------


def test_sampling_defaults_to_empty(store: TransientStore) -> None:
    assert store.sampling == {}


def test_set_sampling_replaces_and_persists(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    s.set_sampling("q4", {"temperature": 0.2, "top_p": 0.9})
    s.set_sampling("q4", {"temperature": 0.5})
    s.set_sampling("q8", {"top_k": 40})

    assert s.sampling == {"q4": {"temperature": 0.5}, "q8": {"top_k": 40}}
    assert _resume(kodo_dir).sampling == {"q4": {"temperature": 0.5}, "q8": {"top_k": 40}}


def test_set_sampling_empty_values_drops_entry(store: TransientStore) -> None:
    store.set_sampling("q4", {"temperature": 0.2})
    store.set_sampling("q4", {})
    store.set_sampling("never-set", {})

    assert store.sampling == {}
    assert _read_transient(store)["sampling"] == {}


def test_sampling_property_returns_a_copy(store: TransientStore) -> None:
    store.set_sampling("q4", {"temperature": 0.2})
    snapshot = store.sampling
    snapshot["q4"]["temperature"] = 9.9
    snapshot["q9"] = {}
    assert store.sampling == {"q4": {"temperature": 0.2}}


def test_resume_drops_malformed_sampling_entries(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    path = s.session_dir / "transient.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sampling"] = {"q4": {"temperature": 0.3}, "bad": "nope"}
    path.write_text(json.dumps(data), encoding="utf-8")

    assert _resume(kodo_dir).sampling == {"q4": {"temperature": 0.3}}


def test_resume_treats_non_dict_sampling_as_empty(kodo_dir: Path) -> None:
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)
    path = s.session_dir / "transient.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sampling"] = ["q4"]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert _resume(kodo_dir).sampling == {}


# ---------------------------------------------------------------------------
# Malformed on-disk state
# ---------------------------------------------------------------------------


def _make_session_dir(kodo_dir: Path, session_id: str = "1748792400") -> Path:
    d = kodo_dir / "sessions" / session_id
    d.mkdir(parents=True)
    return d


def test_resume_with_unparseable_transient_keeps_defaults(kodo_dir: Path) -> None:
    d = _make_session_dir(kodo_dir)
    (d / "transient.json").write_text("{broken", encoding="utf-8")

    s = _resume(kodo_dir)
    assert s.stage == "IDLE"
    assert s.autonomous is False
    assert s.edit_control == "smart"


def test_resume_with_non_object_transient_keeps_defaults(kodo_dir: Path) -> None:
    d = _make_session_dir(kodo_dir)
    (d / "transient.json").write_text("[1, 2]", encoding="utf-8")

    s = _resume(kodo_dir)
    assert s.stage == "IDLE"
    assert s.cumulative_input_tokens == 0


def test_resume_without_any_files_keeps_defaults(kodo_dir: Path) -> None:
    _make_session_dir(kodo_dir)

    s = _resume(kodo_dir)
    assert s.stage == "IDLE"
    assert s.session_name == "Unnamed Session"
    assert s.created_at == ""
    assert s.subsessions_dir.is_dir()


def test_resume_with_unparseable_meta_keeps_default_name(kodo_dir: Path) -> None:
    d = _make_session_dir(kodo_dir)
    (d / "meta.json").write_text("not json", encoding="utf-8")

    s = _resume(kodo_dir)
    assert s.session_name == "Unnamed Session"
    assert s.is_session_named is False


def test_resume_legacy_meta_without_last_modified_uses_created_at(kodo_dir: Path) -> None:
    d = _make_session_dir(kodo_dir)
    (d / "meta.json").write_text(
        json.dumps({"session_name": "Old", "created_at": "2025-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    s = _resume(kodo_dir)
    assert s.session_name == "Old"
    assert s.is_session_named is True
    assert s.last_modified == "2025-01-01T00:00:00+00:00"


def test_append_after_unparseable_last_modified_stamps_now(kodo_dir: Path) -> None:
    d = _make_session_dir(kodo_dir)
    (d / "meta.json").write_text(
        json.dumps({"session_name": "S", "created_at": "c", "last_modified": "garbage"}),
        encoding="utf-8",
    )
    s = _resume(kodo_dir)

    s.append_message("user", "hi")

    stamped = datetime.fromisoformat(s.last_modified)
    assert abs(stamped - datetime.now(tz=UTC)) < timedelta(minutes=1)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_modified"] == s.last_modified


def test_last_modified_strictly_increases_past_a_future_stamp(kodo_dir: Path) -> None:
    future = datetime(2999, 1, 1, tzinfo=UTC)
    d = _make_session_dir(kodo_dir)
    (d / "meta.json").write_text(
        json.dumps({"session_name": "S", "created_at": "c", "last_modified": future.isoformat()}),
        encoding="utf-8",
    )
    s = _resume(kodo_dir)

    s.append_marker({"type": "x"})

    assert datetime.fromisoformat(s.last_modified) == future + timedelta(microseconds=1)


# ---------------------------------------------------------------------------
# Session-name disambiguation edge cases
# ---------------------------------------------------------------------------


def test_set_session_name_skips_unreadable_siblings(kodo_dir: Path) -> None:
    (kodo_dir / "sessions" / "no-meta").mkdir(parents=True)
    bad = kodo_dir / "sessions" / "bad-meta"
    bad.mkdir()
    (bad / "meta.json").write_text("{nope", encoding="utf-8")
    (kodo_dir / "sessions" / "stray-file").write_text("x", encoding="utf-8")
    s = TransientStore(kodo_dir)
    s.attach_session("1748792400", resumed=False)

    s.set_session_name("Snake Game")

    assert s.session_name == "Snake Game"


def test_set_session_name_ignores_own_meta(store: TransientStore) -> None:
    store.set_session_name("Snake Game")
    store.set_session_name("Snake Game")
    assert store.session_name == "Snake Game"
