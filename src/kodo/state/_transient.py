"""Per-session state store under ``.kodo/sessions/<session-id>/``.

Each server session gets one directory.  The directory is created on first
use and reused across restarts when the session is resumed.  Layout::

    .kodo/sessions/<posix-timestamp>/
        meta.json        — human-readable metadata (name, creation time)
        transient.json   — mutable runtime state (stage, prompt, autonomous,
                           active_subsession)
        session.jsonl    — append-only MAIN session log: top-level LLM messages
                           (agent-agnostic — Orchestrator and Problem Solver
                           share it) interleaved with ``subsession_start`` /
                           ``subsession_end`` marker lines
        subsessions/     — one ``<subsession-id>.jsonl`` per sub-agent run,
                           holding that sub-agent's full isolated message history
        agents/          — per-sub-agent JSONL call logs (usage stats)
        toolcalls/        — one ``<tool_use_id>.md`` per dispatched tool call;
                           a tool call that captured a before/after diff (see
                           ``write_diff_files``) additionally gets a sibling
                           ``<tool_use_id>_diff/`` directory holding the two
                           file versions plus a ``meta.json`` sidecar
        attachments/      — immutable copies of files the user attached to a
                           prompt (``store_attachment``). ``session.jsonl`` keeps
                           only a link (relative path + display name); the copy
                           here is what is injected into the LLM context on
                           resume and what the WebView chip opens, so the session
                           survives the original file being moved or deleted.

See ``doc/SESSIONS.md`` for the full session/subsession model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["TransientStore", "new_session_id", "read_diff_files"]

_log = logging.getLogger(__name__)

_UNSET: object = object()

_DEFAULT_SESSION_NAME = "Unnamed Session"


def new_session_id() -> str:
    """Return a new session ID based on the current POSIX timestamp."""
    return str(int(time.time()))


def _diff_file_paths(diff_dir: Path, filename: str) -> tuple[Path, Path]:
    """Return ``(prev_path, new_path)`` for *filename* inside *diff_dir*.

    ``new_path`` keeps the original file name; ``prev_path`` inserts a
    ``_prev`` suffix before the extension (e.g. ``bar.py`` -> ``bar_prev.py``).
    Shared by :meth:`TransientStore.write_diff_files` and :func:`read_diff_files`
    so both sides agree on the on-disk naming.
    """
    name = Path(filename).name
    stem = Path(name).stem
    suffix = Path(name).suffix
    return diff_dir / f"{stem}_prev{suffix}", diff_dir / name


def read_diff_files(toolcalls_dir: Path, tool_call_id: str) -> dict[str, object] | None:
    """Look up a previously-written before/after diff pair for a tool call.

    Used by history rebuild (:meth:`WorkflowEngine.history_entries`, which has
    no live :class:`TransientStore` reference beyond the directory itself) to
    reconstruct the diff link on reload, mirroring what
    :meth:`TransientStore.write_diff_files` returns at dispatch time.

    Returns:
        dict[str, object] | None: ``{"label", "prev_path", "new_path"}`` (paths
        as strings), or ``None`` if no diff was captured for this tool call.
    """
    diff_dir = toolcalls_dir / f"{tool_call_id}_diff"
    meta_path = diff_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    filename = str(meta.get("filename", ""))
    label = str(meta.get("label", filename))
    prev_path, new_path = _diff_file_paths(diff_dir, filename)
    if not (prev_path.exists() and new_path.exists()):
        return None
    return {"label": label, "prev_path": str(prev_path), "new_path": str(new_path)}


@dataclass
class _SessionPaths:
    root: Path

    @property
    def meta(self) -> Path:
        return self.root / "meta.json"

    @property
    def transient(self) -> Path:
        return self.root / "transient.json"

    @property
    def session_log(self) -> Path:
        return self.root / "session.jsonl"

    @property
    def subsessions(self) -> Path:
        return self.root / "subsessions"

    @property
    def toolcalls(self) -> Path:
        return self.root / "toolcalls"

    @property
    def agents(self) -> Path:
        return self.root / "agents"

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"


class TransientStore:
    """Per-session transient state store under ``.kodo/sessions/``.

    Created early (before bootstrap); call :meth:`attach_session` once the
    session ID is known from :class:`~kodo.runtime._bootstrap.ProjectBootstrap`.

    Args:
        kodo_dir (Path): The project's ``.kodo/`` directory.
    """

    __kodo_dir: Path
    __paths: _SessionPaths | None
    __session_id: str
    __session_name: str
    __created_at: str
    __last_modified: str
    __stage: str
    __last_prompt: str
    __autonomous: bool
    __workflow_mode: str
    __pending_prompt: dict[str, object] | None
    __active_subsession: dict[str, object] | None
    __current_project: dict[str, str] | None
    __lock: asyncio.Lock

    def __init__(self, kodo_dir: Path) -> None:
        """Initialise without attaching a session.

        Args:
            kodo_dir (Path): The project's ``.kodo/`` directory.
        """
        self.__kodo_dir = kodo_dir
        self.__paths = None
        self.__session_id = ""
        self.__session_name = _DEFAULT_SESSION_NAME
        self.__created_at = ""
        self.__last_modified = ""
        self.__stage = "IDLE"
        self.__last_prompt = ""
        self.__autonomous = False
        self.__workflow_mode = "guided"
        self.__pending_prompt = None
        self.__active_subsession = None
        self.__current_project = None
        self.__lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        """Identifier for the current session."""
        return self.__session_id

    @property
    def session_name(self) -> str:
        """Human-readable session name, persisted in ``meta.json``.

        Defaults to ``"Unnamed Session"`` until the session titler names it.
        """
        return self.__session_name

    @property
    def is_session_named(self) -> bool:
        """Whether the session has been given a name beyond the default."""
        return self.__session_name != _DEFAULT_SESSION_NAME

    @property
    def created_at(self) -> str:
        """ISO-8601 timestamp of when the session was created (``meta.json``)."""
        return self.__created_at

    @property
    def last_modified(self) -> str:
        """ISO-8601 timestamp of the session's last persisted write.

        Bumped to the current time whenever a record is appended to
        ``session.jsonl``, a subsession log, or a tool-call document; seeded to
        :attr:`created_at` when the session is first created.
        """
        return self.__last_modified

    @property
    def session_dir(self) -> Path:
        """Absolute path to this session's directory."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.root

    @property
    def session_log_path(self) -> Path:
        """Path to the main session JSONL message log."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.session_log

    @property
    def subsessions_dir(self) -> Path:
        """Directory holding this session's per-sub-agent subsession logs."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.subsessions

    @property
    def toolcalls_dir(self) -> Path:
        """Directory holding this session's per-tool-call Markdown documents."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.toolcalls

    @property
    def attachments_dir(self) -> Path:
        """Directory holding this session's stored prompt-attachment copies."""
        assert self.__paths is not None, "attach_session() not yet called"
        return self.__paths.attachments

    def store_attachment(self, display_name: str, content: str) -> str | None:
        """Copy one attachment's text into the session, returning its link path.

        The copy is immutable and self-contained: ``session.jsonl`` stores only
        the returned relative path (plus the display name), never the content,
        so the message is reconstructable even after the original file is gone.
        The stored filename is prefixed with a random token so two attachments
        with the same basename never collide.

        Args:
            display_name (str): The original file's basename (display only).
            content (str): The validated UTF-8 text to store.

        Returns:
            str | None: The stored copy's path relative to the session dir
            (e.g. ``attachments/<token>__name.py``), or ``None`` if no session
            is attached or the write fails.
        """
        if self.__paths is None:
            return None
        safe = Path(display_name).name or "attachment"
        rel = f"attachments/{uuid.uuid4().hex[:12]}__{safe}"
        try:
            self.__paths.attachments.mkdir(parents=True, exist_ok=True)
            (self.__paths.root / rel).write_text(content, encoding="utf-8")
        except OSError:
            _log.exception("Failed to store attachment %r", display_name)
            return None
        self.__touch_last_modified()
        return rel

    def read_attachment(self, stored_rel: str) -> str | None:
        """Read back a stored attachment's text by its session-relative link path.

        Used on resume to re-expand a persisted user message into the exact LLM
        context seen at submit time. Returns ``None`` if the copy is missing or
        unreadable (the caller degrades gracefully rather than failing resume).

        Args:
            stored_rel (str): The relative path returned by
                :meth:`store_attachment` (must stay within ``attachments/``).
        """
        if self.__paths is None:
            return None
        # Guard against path traversal from a tampered/legacy log line.
        rel = Path(stored_rel)
        if rel.is_absolute() or ".." in rel.parts or rel.parts[:1] != ("attachments",):
            return None
        try:
            return (self.__paths.root / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    def attachment_abs_path(self, stored_rel: str) -> str:
        """Absolute path of a stored attachment (for the WebView chip to open)."""
        assert self.__paths is not None, "attach_session() not yet called"
        return str(self.__paths.root / stored_rel)

    def write_tool_call(self, tool_use_id: str, markdown: str) -> Path | None:
        """Persist one tool call's Markdown document, returning its path.

        The file is named ``<tool_use_id>.md`` so the client-history rebuild can
        relink it from the ``tool_use`` block id alone. Returns ``None`` if no
        session is attached or the write fails.

        Args:
            tool_use_id (str): The tool-use block id (stable link key).
            markdown (str): The rendered Markdown document.
        """
        if self.__paths is None:
            return None
        path = self.__paths.toolcalls / f"{tool_use_id}.md"
        try:
            self.__paths.toolcalls.mkdir(parents=True, exist_ok=True)
            path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            _log.warning("Failed to write tool-call document %s: %s", path, exc)
            return None
        self.__touch_last_modified()
        return path

    def write_diff_files(
        self,
        tool_call_id: str,
        label: str,
        filename: str,
        old_content: str,
        new_content: str,
    ) -> dict[str, object] | None:
        """Persist a before/after file pair backing a tool call's diff link.

        Stored under ``toolcalls/<tool_call_id>_diff/`` as ``<name>_prev<ext>``
        (old content) and ``<name><ext>`` (new content), plus a ``meta.json``
        sidecar recording *label*/*filename* so :func:`read_diff_files` can
        reconstruct the link on history reload, when only the directory (not
        the tool's original raw output) is available.

        Args:
            tool_call_id (str): The tool-use block id (keys the directory).
            label (str): Human-readable text for the diff link (e.g. the
                file's project-relative path).
            filename (str): The file's base name; determines the on-disk
                names of both versions.
            old_content (str): The file's content before this tool call.
            new_content (str): The file's content after this tool call.

        Returns:
            dict[str, object] | None: ``{"label", "prev_path", "new_path"}``
            (paths as strings), or ``None`` if no session is attached or the
            write fails.
        """
        if self.__paths is None:
            return None
        diff_dir = self.__paths.toolcalls / f"{tool_call_id}_diff"
        prev_path, new_path = _diff_file_paths(diff_dir, filename)
        try:
            diff_dir.mkdir(parents=True, exist_ok=True)
            prev_path.write_text(old_content, encoding="utf-8")
            new_path.write_text(new_content, encoding="utf-8")
            meta = {"label": label, "filename": Path(filename).name}
            (diff_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:
            _log.warning("Failed to write diff files for %s: %s", tool_call_id, exc)
            return None
        self.__touch_last_modified()
        return {"label": label, "prev_path": str(prev_path), "new_path": str(new_path)}

    @property
    def active_subsession(self) -> dict[str, object] | None:
        """The currently in-flight sub-agent subsession, if any.

        Persisted in ``transient.json`` so that a server restart while a
        sub-agent is mid-run can recover into that subsession and resume it.
        ``None`` whenever the top-level (main) agent holds the turn. The record
        carries at least ``{"subsession_id", "agent", "display_name",
        "parent_display_name"}``.
        """
        return self.__active_subsession

    @property
    def current_project(self) -> dict[str, str] | None:
        """The session's locked current project ``{root, name}`` (Guided), if any.

        Persisted in ``transient.json`` so that a server restart re-binds the
        same project and crash-resume of a Guided turn keeps working.
        """
        return self.__current_project

    @property
    def stage(self) -> str:
        """Most recent workflow stage."""
        return self.__stage

    @property
    def last_prompt(self) -> str:
        """Last developer prompt, stored for resume support."""
        return self.__last_prompt

    @property
    def autonomous(self) -> bool:
        """Whether autonomous mode is active."""
        return self.__autonomous

    @property
    def workflow_mode(self) -> str:
        """Persisted workflow mode (``"guided"`` | ``"problem_solving"``).

        Per-session so a window hosting several sessions can keep each in its
        own mode across reloads/resume.
        """
        return self.__workflow_mode

    @property
    def pending_prompt(self) -> dict[str, object] | None:
        """The outstanding ``prompt.question``/``prompt.approval`` request, if any.

        Persisted so that a server restart with an unanswered prompt can
        re-surface it to the user instead of silently dropping it.
        """
        return self.__pending_prompt

    def attach_session(self, session_id: str, resumed: bool) -> None:
        """Attach to an existing session or create a new one.

        Called by the engine immediately after bootstrap completes.

        Args:
            session_id (str): Session identifier from bootstrap.
            resumed (bool): ``True`` if the session already exists on disk.
        """
        paths = _SessionPaths(self.__kodo_dir / "sessions" / session_id)
        self.__paths = paths
        self.__session_id = session_id

        if resumed:
            paths.subsessions.mkdir(exist_ok=True)
            paths.toolcalls.mkdir(exist_ok=True)
            self.__load_transient(paths)
            self.__load_meta(paths)
            _log.info("Transient session resumed: %s (name=%r)", session_id, self.__session_name)
        else:
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.agents.mkdir(exist_ok=True)
            paths.subsessions.mkdir(exist_ok=True)
            paths.toolcalls.mkdir(exist_ok=True)
            self.__session_name = _DEFAULT_SESSION_NAME
            self.__created_at = datetime.now(tz=UTC).isoformat()
            self.__last_modified = self.__created_at
            self.__write_meta(paths)
            self.__flush(paths)
            _log.info("Transient session created: %s", session_id)

    def set_session_name(self, name: str) -> None:
        """Set the session name and persist it to ``meta.json``.

        Other ``meta.json`` fields (e.g. ``created_at``) are preserved.

        Args:
            name (str): New human-readable session name.
        """
        self.__session_name = name
        if self.__paths is not None:
            self.__write_meta(self.__paths)

    def update(
        self,
        *,
        stage: str | None = None,
        prompt: str | None = None,
        autonomous: bool | None = None,
        workflow_mode: str | None = None,
        pending_prompt: dict[str, object] | None = _UNSET,  # type: ignore[assignment]
        active_subsession: dict[str, object] | None = _UNSET,  # type: ignore[assignment]
        current_project: dict[str, str] | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Update mutable fields and flush ``transient.json`` to disk.

        Args:
            stage (str | None): New stage name if changed.
            prompt (str | None): Developer prompt to persist for resume.
            autonomous (bool | None): New autonomous flag if changed.
            pending_prompt (dict[str, object] | None): Outstanding
                ``prompt.question``/``prompt.approval`` request to persist,
                or ``None`` to clear it. Left unchanged if omitted.
            active_subsession (dict[str, object] | None): The in-flight
                sub-agent subsession record to persist, or ``None`` to clear it
                (the main agent holds the turn again). Left unchanged if omitted.
            current_project (dict[str, str] | None): The session's locked
                current project ``{root, name}``. Left unchanged if omitted.
        """
        if stage is not None:
            self.__stage = stage
        if prompt is not None:
            self.__last_prompt = prompt
        if autonomous is not None:
            self.__autonomous = autonomous
        if workflow_mode is not None:
            self.__workflow_mode = workflow_mode
        if pending_prompt is not _UNSET:
            self.__pending_prompt = pending_prompt
        if active_subsession is not _UNSET:
            self.__active_subsession = active_subsession
        if current_project is not _UNSET:
            self.__current_project = current_project
        if self.__paths is not None:
            self.__flush(self.__paths)

    def append_message(
        self,
        role: str,
        content: str | list[dict[str, object]],
        entry_agent: str | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> None:
        """Append one top-level LLM message to the main ``session.jsonl``.

        The main log is agent-agnostic: both the Orchestrator and the Problem
        Solver append to it. ``entry_agent`` tags which top-level agent produced
        the message (display/audit only — context is shared across them).

        ``attachments`` records prompt file-attachments as opaque links —
        ``{"name", "stored"}`` where ``stored`` is the session-relative path of
        the copy written by :meth:`store_attachment`. The persisted ``content``
        is the user's *clean* prompt (no file content); the attachment text is
        re-injected on resume from the stored copies, so it never bloats the log.

        Args:
            role (str): ``'user'`` or ``'assistant'``.
            content (str | list): Message content (plain text or content blocks).
            entry_agent (str | None): Name of the top-level agent that produced
                this message, if known.
            attachments (list[dict[str, str]] | None): Attachment links to bind
                to this message, or ``None``/empty for a plain message.
        """
        if self.__paths is None:
            return
        record: dict[str, object] = {"role": role, "content": content}
        if entry_agent is not None:
            record["entry_agent"] = entry_agent
        if attachments:
            record["attachments"] = attachments
        self.__append_line(self.__paths.session_log, record)
        self.__touch_last_modified()

    def append_marker(self, marker: dict[str, object]) -> None:
        """Append a non-message marker line to the main ``session.jsonl``.

        Markers (``subsession_start`` / ``subsession_end``) sit inline, in order,
        between the message lines so that the chronological structure of a
        session — including which sub-agents took over and when — is recoverable
        for both resume and client-side history rebuild. Markers carry a
        ``type`` key and never a ``role`` key, so :meth:`read_messages` skips them.

        Args:
            marker (dict[str, object]): JSON-serialisable marker payload.
        """
        if self.__paths is None:
            return
        self.__append_line(self.__paths.session_log, marker)
        self.__touch_last_modified()

    def read_session_lines(self) -> list[dict[str, object]]:
        """Return every line of the main ``session.jsonl`` in order.

        Includes both message lines (``role`` present) and marker lines
        (``type`` present). Use :meth:`read_messages` for context reconstruction.

        Returns:
            list[dict[str, object]]: Ordered raw line payloads.
        """
        return self.__read_jsonl(None if self.__paths is None else self.__paths.session_log)

    def read_messages(self) -> list[dict[str, object]]:
        """Return only the message lines from the main ``session.jsonl``.

        Marker lines (``subsession_start`` / ``subsession_end``) are filtered
        out so the result is the top-level LLM context, in order.

        Returns:
            list[dict[str, object]]: Ordered list of ``{role, content}`` dicts.
        """
        return [line for line in self.read_session_lines() if "role" in line]

    # -- Subsession logs -------------------------------------------------

    def append_subsession_message(
        self, subsession_id: str, role: str, content: str | list[dict[str, object]]
    ) -> None:
        """Append one message to a sub-agent's isolated subsession log.

        Args:
            subsession_id (str): Session-wide unique subsession identifier.
            role (str): ``'user'`` or ``'assistant'``.
            content (str | list): Message content (plain text or content blocks).
        """
        if self.__paths is None:
            return
        self.__paths.subsessions.mkdir(exist_ok=True)
        path = self.__paths.subsessions / f"{subsession_id}.jsonl"
        self.__append_line(path, {"role": role, "content": content})
        self.__touch_last_modified()

    def read_subsession_messages(self, subsession_id: str) -> list[dict[str, object]]:
        """Return a subsession's full message history in order.

        Args:
            subsession_id (str): Subsession identifier.

        Returns:
            list[dict[str, object]]: Ordered ``{role, content}`` dicts (empty if
            the subsession file does not exist).
        """
        if self.__paths is None:
            return []
        return self.__read_jsonl(self.__paths.subsessions / f"{subsession_id}.jsonl")

    @staticmethod
    def __append_line(path: Path, record: dict[str, object]) -> None:
        line = json.dumps(record) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    @staticmethod
    def __read_jsonl(path: Path | None) -> list[dict[str, object]]:
        if path is None or not path.exists():
            return []
        out: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                try:
                    out.append(json.loads(stripped))
                except json.JSONDecodeError:
                    _log.warning("Skipping malformed JSONL line in %s", path.name)
        return out

    async def write_agent_record(self, agent_name: str, record: dict[str, object]) -> None:
        """Append one JSON record to a sub-agent's JSONL log.

        Args:
            agent_name (str): Agent identifier (used as the filename stem).
            record (dict[str, object]): Arbitrary JSON-serialisable data.
        """
        if self.__paths is None:
            return
        path = self.__paths.agents / f"{agent_name}.jsonl"
        line = json.dumps(record, default=str) + "\n"
        async with self.__lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: path.open("a", encoding="utf-8").write(line)
            )

    def __load_transient(self, paths: _SessionPaths) -> None:
        if not paths.transient.exists():
            return
        try:
            data = json.loads(paths.transient.read_text(encoding="utf-8"))
            self.__stage = str(data.get("stage", "IDLE"))
            self.__last_prompt = str(data.get("last_prompt", ""))
            self.__autonomous = bool(data.get("autonomous", False))
            self.__workflow_mode = (
                "problem_solving" if data.get("workflow_mode") == "problem_solving" else "guided"
            )
            pending = data.get("pending_prompt")
            self.__pending_prompt = pending if isinstance(pending, dict) else None
            active = data.get("active_subsession")
            self.__active_subsession = active if isinstance(active, dict) else None
            project = data.get("current_project")
            self.__current_project = (
                {"root": str(project.get("root", "")), "name": str(project.get("name", ""))}
                if isinstance(project, dict) and project.get("root")
                else None
            )
        except Exception:
            _log.warning("Could not parse transient.json — using defaults")

    def __load_meta(self, paths: _SessionPaths) -> None:
        if not paths.meta.exists():
            return
        try:
            data = json.loads(paths.meta.read_text(encoding="utf-8"))
            self.__session_name = str(data.get("session_name", _DEFAULT_SESSION_NAME))
            self.__created_at = str(data.get("created_at", ""))
            # last_modified defaults to created_at for sessions persisted before
            # the field existed, so reloaded legacy sessions still show a value.
            self.__last_modified = str(data.get("last_modified", self.__created_at))
        except Exception:
            _log.warning("Could not parse meta.json — using defaults")

    def __touch_last_modified(self) -> None:
        """Stamp ``last_modified`` with the current time and rewrite ``meta.json``.

        Called after every persisted write (``session.jsonl``, subsession logs,
        tool-call documents) so the session list can show recency.
        """
        if self.__paths is None:
            return
        self.__last_modified = datetime.now(tz=UTC).isoformat()
        self.__write_meta(self.__paths)

    def __write_meta(self, paths: _SessionPaths) -> None:
        created_at = self.__created_at or datetime.now(tz=UTC).isoformat()
        meta = {
            "session_name": self.__session_name,
            "created_at": created_at,
            "last_modified": self.__last_modified or created_at,
        }
        paths.meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def __flush(self, paths: _SessionPaths) -> None:
        data = {
            "stage": self.__stage,
            "last_prompt": self.__last_prompt,
            "autonomous": self.__autonomous,
            "workflow_mode": self.__workflow_mode,
            "pending_prompt": self.__pending_prompt,
            "active_subsession": self.__active_subsession,
            "current_project": self.__current_project,
        }
        paths.transient.write_text(json.dumps(data, indent=2), encoding="utf-8")
