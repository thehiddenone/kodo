"""Append-only JSONL audit log for a single sub-agent session.

Each session's events are persisted to ``.kodo/sessions/<session_id>.jsonl``
so that the bootstrap process can recover in-flight state after a crash and
an external audit trail is always available.

The append-before-respond invariant: the caller MUST call :meth:`SessionLog.append`
with the LLM request event *before* invoking the LLM, and again with the
response event *before* forwarding it back to the sub-agent.  This guarantees
that on crash, both the request and (if received) the response are on disk.
"""

from __future__ import annotations

import json
from pathlib import Path


class SessionLog:
    """Append-only JSONL log for one sub-agent session.

    Events are stored as newline-delimited JSON in a file named
    ``<session_id>.jsonl`` inside ``sessions_dir``.  The log is never
    truncated or rewritten; only new lines are added.

    Args:
        sessions_dir (Path): Directory that holds per-session JSONL files
            (typically ``<project>/.kodo/sessions/``).
        session_id (str): Identifier for this session; used as the file stem.
    """

    __session_id: str
    __path: Path

    def __init__(self, sessions_dir: Path, session_id: str) -> None:
        """Initialise the log handle.

        Args:
            sessions_dir (Path): Parent directory for session files.
            session_id (str): Unique session identifier.
        """
        self.__session_id = session_id
        self.__path = sessions_dir / f"{session_id}.jsonl"

    @property
    def session_id(self) -> str:
        """The session identifier this log belongs to."""
        return self.__session_id

    @property
    def path(self) -> Path:
        """Absolute path to the JSONL file."""
        return self.__path

    def append(self, event: dict[str, object]) -> None:
        """Append one event as a JSON line.

        The parent directory is created if absent.  The write is synchronous
        and fully flushed before returning so that the event is durable even
        if the process is killed immediately afterward.

        Args:
            event (dict[str, object]): JSON-serialisable event payload.
        """
        self.__path.parent.mkdir(parents=True, exist_ok=True)
        with self.__path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def read_events(self) -> list[dict[str, object]]:
        """Return all events in the order they were appended.

        Returns an empty list if the session file does not exist yet.

        Returns:
            list[dict[str, object]]: Ordered list of event payloads.
        """
        if not self.__path.exists():
            return []
        events: list[dict[str, object]] = []
        for line in self.__path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                events.append(json.loads(stripped))
        return events
