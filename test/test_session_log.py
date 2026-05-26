"""Behavioral tests for SessionLog.

Tests verify that events are persisted as JSONL lines, read back in order,
and survive process restart (new instance reading the same file).
"""

from __future__ import annotations

from pathlib import Path

from kodo.workflow._session_log import SessionLog


def _log(tmp_path: Path, session_id: str = "sess-1") -> SessionLog:
    return SessionLog(sessions_dir=tmp_path / "sessions", session_id=session_id)


# ---------------------------------------------------------------------------
# append() persists events
# ---------------------------------------------------------------------------


def test_append_creates_jsonl_file(tmp_path: Path) -> None:
    """
    Given a new SessionLog,
    when append() is called,
    then the JSONL file is created.
    """
    log = _log(tmp_path)
    log.append({"kind": "session_start"})
    assert log.path.exists()


def test_append_event_is_readable(tmp_path: Path) -> None:
    """
    Given a SessionLog with one appended event,
    when read_events() is called,
    then the event dict is returned.
    """
    log = _log(tmp_path)
    log.append({"kind": "llm_request", "model": "claude"})
    events = log.read_events()
    assert len(events) == 1
    assert events[0]["kind"] == "llm_request"
    assert events[0]["model"] == "claude"


def test_multiple_appends_preserve_order(tmp_path: Path) -> None:
    """
    Given three events appended in order,
    when read_events() is called,
    then all three are returned in the same order.
    """
    log = _log(tmp_path)
    log.append({"kind": "session_start", "seq": 1})
    log.append({"kind": "llm_request", "seq": 2})
    log.append({"kind": "llm_response", "seq": 3})

    events = log.read_events()
    assert len(events) == 3
    assert [e["seq"] for e in events] == [1, 2, 3]


# ---------------------------------------------------------------------------
# read_events() on a missing file
# ---------------------------------------------------------------------------


def test_read_events_returns_empty_list_when_no_file(tmp_path: Path) -> None:
    """
    Given a SessionLog whose file does not exist,
    when read_events() is called,
    then an empty list is returned.
    """
    log = _log(tmp_path)
    assert log.read_events() == []


# ---------------------------------------------------------------------------
# Crash recovery: new instance reads prior events
# ---------------------------------------------------------------------------


def test_new_instance_reads_events_from_prior_session(tmp_path: Path) -> None:
    """
    Given events appended by a first SessionLog instance,
    when a second SessionLog instance for the same session_id is created,
    then read_events() returns the events written by the first instance.
    """
    first = _log(tmp_path, "sess-abc")
    first.append({"kind": "session_start"})
    first.append({"kind": "llm_request"})

    second = _log(tmp_path, "sess-abc")
    events = second.read_events()
    assert len(events) == 2
    assert events[0]["kind"] == "session_start"
    assert events[1]["kind"] == "llm_request"


# ---------------------------------------------------------------------------
# path and session_id properties
# ---------------------------------------------------------------------------


def test_path_is_inside_sessions_dir(tmp_path: Path) -> None:
    log = _log(tmp_path, "my-session")
    assert log.path == tmp_path / "sessions" / "my-session.jsonl"


def test_session_id_property(tmp_path: Path) -> None:
    log = _log(tmp_path, "test-123")
    assert log.session_id == "test-123"


# ---------------------------------------------------------------------------
# Sessions are isolated by session_id
# ---------------------------------------------------------------------------


def test_two_sessions_do_not_share_events(tmp_path: Path) -> None:
    """
    Given events appended to two different session IDs,
    when read_events() is called on each,
    then each returns only its own events.
    """
    log_a = _log(tmp_path, "sess-A")
    log_b = _log(tmp_path, "sess-B")

    log_a.append({"kind": "event-from-A"})
    log_b.append({"kind": "event-from-B"})

    assert log_a.read_events()[0]["kind"] == "event-from-A"
    assert log_b.read_events()[0]["kind"] == "event-from-B"
    assert len(log_a.read_events()) == 1
    assert len(log_b.read_events()) == 1
