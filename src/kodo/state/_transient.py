"""Per-session state store under ``.kodo/sessions/<session-id>/``.

Each server session gets one directory.  The directory is created on first
use and reused across restarts when the session is resumed.  Layout::

    .kodo/sessions/<posix-timestamp>/
        meta.json        — human-readable metadata (name, creation time)
        transient.json   — mutable runtime state (stage, prompt, autonomous,
                           active_subsession, security_rules,
                           security_path_rules, pending_security_alert,
                           pending_edit_review)
        session.jsonl    — append-only MAIN session log: top-level LLM messages
                           (agent-agnostic — Guide and Problem Solver
                           share it) interleaved with ``subsession_start`` /
                           ``subsession_end`` marker lines and per-call
                           ``usage`` markers (tokens/cost/model/stop_reason)
        subsessions/     — one ``<subsession-id>.jsonl`` per sub-agent run,
                           holding that sub-agent's full isolated message
                           history, its own ``usage`` markers included
        toolcalls/        — one ``<tool_use_id>.md`` per dispatched tool call;
                           a tool call that captured a before/after diff (see
                           ``write_diff_files``) additionally gets a sibling
                           ``<tool_use_id>_diff/`` directory holding the two
                           file versions plus a ``meta.json`` sidecar; a
                           ``web_search`` call additionally gets a sibling
                           ``<tool_use_id>_websearch_notes.json`` holding its
                           live-narration notes (see ``write_web_search_notes``)
        attachments/      — immutable copies of files the user attached to a
                           prompt (``store_attachment``). ``session.jsonl`` keeps
                           only a link (relative path + display name); the copy
                           here is what is injected into the LLM context on
                           resume and what the WebView chip opens, so the session
                           survives the original file being moved or deleted.

See ``doc/SESSIONS.md`` for the full session/subsession model.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = ["TransientStore", "new_session_id", "read_diff_files", "read_web_search_notes"]

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


def read_web_search_notes(toolcalls_dir: Path, tool_call_id: str) -> list[str]:
    """Read back a ``web_search`` call's persisted live-narration notes.

    Used by history rebuild (:meth:`~kodo.runtime._engine._history.HistoryProjector.
    history_entries`, which has no live :class:`TransientStore` reference beyond
    the directory itself) to replay the "Web Search" block's narration into a
    reloaded/resumed session, mirroring :func:`read_diff_files`.

    Returns:
        list[str]: The notes in order, or ``[]`` if none were written (no
        call, no free text produced, or the run was aborted before its
        best-effort flush at the end of :meth:`TransientStore.write_web_search_notes`).
    """
    path = toolcalls_dir / f"{tool_call_id}_websearch_notes.json"
    if not path.exists():
        return []
    try:
        notes = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(n) for n in notes] if isinstance(notes, list) else []


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
    __edit_control: str
    __command_control: str
    __thinking_level: str
    __security_rules: frozenset[tuple[str, str]]
    __security_path_rules: frozenset[tuple[str, str]]
    __pending_prompt: dict[str, object] | None
    __pending_security_alert: str | None
    __pending_edit_review: str | None
    __active_subsession: dict[str, object] | None
    __current_project: dict[str, str] | None

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
        self.__edit_control = "smart"
        self.__command_control = "smart"
        self.__thinking_level = ""
        self.__security_rules = frozenset()
        self.__security_path_rules = frozenset()
        self.__pending_prompt = None
        self.__pending_security_alert = None
        self.__pending_edit_review = None
        self.__active_subsession = None
        self.__current_project = None

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

    def store_attachment(self, display_name: str, content: str) -> tuple[str, str] | None:
        """Copy one attachment's text into the session, returning its ID and link path.

        The copy is immutable and self-contained: ``session.jsonl`` stores only
        the ID, the display name, and the returned relative path — never the
        content — so the message is reconstructable even after the original
        file is gone. The stored filename is prefixed with the attachment's own
        ID so two attachments with the same basename never collide, and so
        ``kodo.tools``'s ``read_attachment`` tool can find the copy by ID alone
        (via ``kodo.project.session_attachments_dir``) without depending on
        this store.

        Args:
            display_name (str): The original file's basename (display only).
            content (str): The validated UTF-8 text to store.

        Returns:
            tuple[str, str] | None: ``(attachment_id, stored_rel)`` — a freshly
            minted UUID4 and the copy's path relative to the session dir (e.g.
            ``attachments/<attachment_id>__name.py``) — or ``None`` if no
            session is attached or the write fails.
        """
        if self.__paths is None:
            return None
        safe = Path(display_name).name or "attachment"
        attachment_id = str(uuid.uuid4())
        rel = f"attachments/{attachment_id}__{safe}"
        try:
            self.__paths.attachments.mkdir(parents=True, exist_ok=True)
            (self.__paths.root / rel).write_text(content, encoding="utf-8")
        except OSError:
            _log.exception("Failed to store attachment %r", display_name)
            return None
        self.__touch_last_modified()
        return attachment_id, rel

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

    def write_web_search_notes(self, tool_call_id: str, notes: list[str]) -> None:
        """Persist a ``web_search`` call's live-narration notes, best-effort.

        Written once, after the agent's run ends (see ``_run_web_search_agent``),
        as ``toolcalls/<tool_call_id>_websearch_notes.json`` — a sidecar file
        keyed purely by ``tool_call_id`` like the tool-call Markdown doc and
        diff-file pair, so it works identically whether the call happened in
        the main turn or inside any subsession, and never touches
        ``session.jsonl``/a subsession log (so it can never leak into LLM
        context or a crash-resume replay). A failed write is only logged: this
        is a UI visibility aid, not part of the durable conversation, and it
        is fine to lose it (see doc/WEB_SEARCH.md §6).

        Args:
            tool_call_id (str): The tool-use block id (stable link key).
            notes (list[str]): The narration notes, in order.
        """
        if self.__paths is None:
            return
        path = self.__paths.toolcalls / f"{tool_call_id}_websearch_notes.json"
        try:
            self.__paths.toolcalls.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(notes), encoding="utf-8")
        except OSError as exc:
            _log.warning("Failed to write web_search notes %s: %s", path, exc)
            return
        self.__touch_last_modified()

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
        """Persisted workflow mode (``"guided"`` | ``"problem_solving"`` | ``"judge"``).

        ``"judge"`` is validator-only (kodo.validator._evaluate) and never sent
        by kodo-vsix. Per-session so a window hosting several sessions can keep
        each in its own mode across reloads/resume.
        """
        return self.__workflow_mode

    @property
    def edit_control(self) -> str:
        """Persisted Edit Control posture (``"review_all"`` | ``"allow_all"`` | ``"smart"``)."""
        return self.__edit_control

    @property
    def command_control(self) -> str:
        """Persisted Command Control posture (``"defensive"`` | ``"permissive"`` | ``"smart"``)."""
        return self.__command_control

    @property
    def thinking_level(self) -> str:
        """Persisted thinking-tier slug for the session's active local model.

        ``""`` while on a cloud model, a local model with no thinking family,
        or before the engine has ever set one. Unlike ``edit_control``/
        ``command_control`` this is not validated against a fixed set on
        load — the valid values depend on the active model, so the engine
        re-validates it against the *current* model on resume (see
        ``WorkflowEngine.start``/doc/SESSIONS.md).
        """
        return self.__thinking_level

    @property
    def security_rules(self) -> frozenset[tuple[str, str]]:
        """Persisted Phase 2 "always allow" grants for this session
        (doc/SECURITY_RULES_PLAN.md §2) — ``(executable, subcommand)``
        shapes. Mutated only via :meth:`add_security_rule`.
        """
        return self.__security_rules

    def add_security_rule(self, executable: str, subcommand: str) -> frozenset[tuple[str, str]]:
        """Grant ``(executable, subcommand)`` for the rest of this session
        and persist it to ``transient.json``, surviving crash-resume.

        Returns:
            frozenset[tuple[str, str]]: The updated rule set.
        """
        self.__security_rules = self.__security_rules | {(executable, subcommand)}
        if self.__paths is not None:
            self.__flush(self.__paths)
        return self.__security_rules

    @property
    def security_path_rules(self) -> frozenset[tuple[str, str]]:
        """Persisted workspace-escape path grants for this session
        (doc/SECURITY_RULES_PLAN.md §2.7) — ``(executable, resolved_absolute_
        path)`` shapes. The path sibling of :attr:`security_rules` — kept in
        a separate field/key rather than folded into it, since the two rule
        kinds are matched with different semantics (literal vs.
        resolve-then-compare). Mutated only via :meth:`add_security_path_rule`.
        """
        return self.__security_path_rules

    def add_security_path_rule(self, executable: str, path: str) -> frozenset[tuple[str, str]]:
        """Grant ``(executable, resolved_absolute_path)`` for the rest of
        this session and persist it to ``transient.json``, surviving
        crash-resume.

        Returns:
            frozenset[tuple[str, str]]: The updated path-rule set.
        """
        self.__security_path_rules = self.__security_path_rules | {(executable, path)}
        if self.__paths is not None:
            self.__flush(self.__paths)
        return self.__security_path_rules

    @property
    def pending_prompt(self) -> dict[str, object] | None:
        """The outstanding ``prompt.approval`` request, if any.

        Persisted so that a server restart with an unanswered approval can
        re-surface it to the user instead of silently dropping it.
        (``ask_user`` questions are no longer recorded here — their flushed
        ``tool_use`` drives the dangling-tool-use resume path instead.)
        """
        return self.__pending_prompt

    @property
    def pending_security_alert(self) -> str | None:
        """The ``tool_call_id`` of a ``run_command``-class call currently
        blocked at the security permission gate (``prompt.permission``), if
        any — set for the duration of :meth:`GateOrchestrator.fire_permission`'s
        wait and cleared the instant it resolves.

        Unlike ``prompt.permission`` itself (which carries no persisted
        ``pending_prompt`` — doc/SECURITY.md §7), this one field distinguishes
        a dangling tool call that died **before** dispatch (still at the
        gate, provably never executed) from one that died mid-execution
        (unknown side effects, never safe to retry). Only the former case is
        exempt from the interrupted-stub fallback on cold-restart resume —
        see :meth:`~kodo.runtime._engine._resume.ResumeMixin._resume_main_turn`.
        Because dispatch is strictly sequential, at most one tool call can be
        gating at any instant.
        """
        return self.__pending_security_alert

    @property
    def pending_edit_review(self) -> str | None:
        """The ``tool_call_id`` of a ``create_file``/``edit_file`` call
        currently blocked at the Edit Control review gate
        (``prompt.edit_review``), if any — set for the duration of
        :meth:`GateOrchestrator.fire_edit_review`'s wait and cleared the
        instant it resolves.

        Mirrors :attr:`pending_security_alert` exactly, for the same reason:
        it distinguishes a dangling tool call that died **before** dispatch
        (still at the gate, provably never executed) from one that died
        mid-execution — see
        :meth:`~kodo.runtime._engine._resume.ResumeMixin._resume_main_turn`.
        Because dispatch is strictly sequential, at most one tool call can be
        gating at any instant.
        """
        return self.__pending_edit_review

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

        *name* is disambiguated against every other session's persisted name
        first (see :meth:`__unique_name`) — the titler is a deterministic
        function of the prompt (sanitized to a narrow alnum/Title-Case
        shape), so two sessions started from similar or identical prompts
        would otherwise collide and be indistinguishable in the tab strip
        and session picker.

        Other ``meta.json`` fields (e.g. ``created_at``) are preserved.

        Args:
            name (str): New human-readable session name.
        """
        self.__session_name = self.__unique_name(name)
        if self.__paths is not None:
            self.__write_meta(self.__paths)

    def __unique_name(self, candidate: str) -> str:
        """Disambiguate *candidate* against sibling sessions' persisted names.

        Scans every other session directory's ``meta.json`` under
        ``sessions/`` (this session's own directory excluded) and appends
        ``-1``, ``-2``, ... until *candidate* is unique. Best-effort: a
        missing/unreadable/malformed sibling ``meta.json`` is simply skipped
        rather than failing the rename.
        """
        sessions_dir = self.__kodo_dir / "sessions"
        if not sessions_dir.is_dir():
            return candidate
        taken: set[str] = set()
        for path in sessions_dir.iterdir():
            if not path.is_dir() or path.name == self.__session_id:
                continue
            meta = path / "meta.json"
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            taken.add(str(data.get("session_name", "")))
        if candidate not in taken:
            return candidate
        suffix = 1
        while f"{candidate}-{suffix}" in taken:
            suffix += 1
        return f"{candidate}-{suffix}"

    def update(
        self,
        *,
        stage: str | None = None,
        prompt: str | None = None,
        autonomous: bool | None = None,
        workflow_mode: str | None = None,
        edit_control: str | None = None,
        command_control: str | None = None,
        thinking_level: str | None = None,
        pending_prompt: dict[str, object] | None = _UNSET,  # type: ignore[assignment]
        pending_security_alert: str | None = _UNSET,  # type: ignore[assignment]
        pending_edit_review: str | None = _UNSET,  # type: ignore[assignment]
        active_subsession: dict[str, object] | None = _UNSET,  # type: ignore[assignment]
        current_project: dict[str, str] | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Update mutable fields and flush ``transient.json`` to disk.

        Args:
            stage (str | None): New stage name if changed.
            prompt (str | None): Developer prompt to persist for resume.
            autonomous (bool | None): New autonomous flag if changed.
            workflow_mode (str | None): New workflow mode if changed.
            edit_control (str | None): New Edit Control posture if changed.
            command_control (str | None): New Command Control posture if changed.
            thinking_level (str | None): New thinking-tier slug if changed
                (``""`` is a valid value — no thinking family — and is
                distinguished from "unchanged" via ``None``).
            pending_prompt (dict[str, object] | None): Outstanding
                ``prompt.approval`` request to persist, or ``None`` to clear
                it. Left unchanged if omitted.
            pending_security_alert (str | None): The ``tool_call_id`` of a
                call currently blocked at the security permission gate, or
                ``None`` to clear it. Left unchanged if omitted.
            pending_edit_review (str | None): The ``tool_call_id`` of a
                ``create_file``/``edit_file`` call currently blocked at the
                Edit Control review gate, or ``None`` to clear it. Left
                unchanged if omitted.
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
        if edit_control is not None:
            self.__edit_control = edit_control
        if command_control is not None:
            self.__command_control = command_control
        if thinking_level is not None:
            self.__thinking_level = thinking_level
        if pending_prompt is not _UNSET:
            self.__pending_prompt = pending_prompt
        if pending_security_alert is not _UNSET:
            self.__pending_security_alert = pending_security_alert
        if pending_edit_review is not _UNSET:
            self.__pending_edit_review = pending_edit_review
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
        kind: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Append one top-level LLM message to the main ``session.jsonl``.

        The main log is agent-agnostic: both the Guide and the Problem
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
            kind (str | None): Optional entry discriminator (mirrors
                :meth:`append_subsession_message`'s ``kind``), read by
                ``history_entries``/``__message_to_entries`` to render the line
                as something other than a plain chat bubble — e.g.
                ``"stopped_notice"`` for the LLM-only "you were stopped" note
                :meth:`WorkflowEngine.__persist_interrupted_turn` appends, which
                replays as the same ``interrupted`` callout as the live one
                instead of a fake user message. Never part of ``content``, so it
                never reaches the LLM wire format on reload.
            detail (dict[str, object] | None): Optional client-only payload
                alongside ``kind`` — e.g. ``kind="agent_unstuck_nudge"``
                carries ``{"reasons", "note", "mode"}`` here
                (doc/STUCK_DETECTION.md) so the feed can show *why* Kōdo
                nudged the agent without that explanation ever reaching the
                LLM (only ``role``/``content`` round-trip into the live
                message array on reload).
        """
        if self.__paths is None:
            return
        record: dict[str, object] = {"role": role, "content": content}
        if entry_agent is not None:
            record["entry_agent"] = entry_agent
        if attachments:
            record["attachments"] = attachments
        if kind is not None:
            record["kind"] = kind
        if detail is not None:
            record["detail"] = detail
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
        self,
        subsession_id: str,
        role: str,
        content: str | list[dict[str, object]],
        kind: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Append one message to a sub-agent's isolated subsession log.

        Args:
            subsession_id (str): Session-wide unique subsession identifier.
            role (str): ``'user'`` or ``'assistant'``.
            content (str | list): Message content (plain text or content blocks).
            kind (str | None): Optional entry discriminator. ``"subagent_task"``
                tags the structured task the engine seeds a subsession with, so
                history reconstruction renders it as a distinct *task brief*
                rather than a user prompt bubble. ``"agent_unstuck_nudge"``
                (doc/STUCK_DETECTION.md) tags the stuck-watchdog's continuation
                nudge the same way. ``None`` for ordinary turns.
            detail (dict[str, object] | None): Optional client-only payload
                alongside ``kind`` — see :meth:`append_message`'s twin
                parameter for the shape ``kind="agent_unstuck_nudge"`` uses.
        """
        if self.__paths is None:
            return
        self.__paths.subsessions.mkdir(exist_ok=True)
        path = self.__subsession_path(subsession_id)
        record: dict[str, object] = {"role": role, "content": content}
        if kind is not None:
            record["kind"] = kind
        if detail is not None:
            record["detail"] = detail
        self.__append_line(path, record)
        self.__touch_last_modified()

    def append_subsession_marker(self, subsession_id: str, marker: dict[str, object]) -> None:
        """Append a non-message marker line to a sub-agent's subsession log.

        The subsession-scoped twin of :meth:`append_marker`: an event that
        happens *during* a subsession's own run (e.g. its per-turn usage
        stats, or an error/stuck-watchdog notice for its own agent) belongs
        in its own log, not the parent's — subsessions are otherwise
        identical containers to the main session, just unable to nest (see
        doc/SESSIONS.md). Markers carry a ``type`` key and never a ``role``
        key, so :meth:`read_subsession_messages` skips them.

        Args:
            subsession_id (str): Session-wide unique subsession identifier.
            marker (dict[str, object]): JSON-serialisable marker payload.
        """
        if self.__paths is None:
            return
        self.__paths.subsessions.mkdir(exist_ok=True)
        self.__append_line(self.__subsession_path(subsession_id), marker)
        self.__touch_last_modified()

    def read_subsession_lines(self, subsession_id: str) -> list[dict[str, object]]:
        """Return every line of a subsession's log in order, markers included.

        The subsession-scoped twin of :meth:`read_session_lines`. Use
        :meth:`read_subsession_messages` for LLM context reconstruction.

        Args:
            subsession_id (str): Subsession identifier.

        Returns:
            list[dict[str, object]]: Ordered raw line payloads (empty if the
            subsession file does not exist).
        """
        if self.__paths is None:
            return []
        return self.__read_jsonl(self.__subsession_path(subsession_id))

    def read_subsession_messages(self, subsession_id: str) -> list[dict[str, object]]:
        """Return only the message lines from a subsession's log, in order.

        Marker lines (e.g. ``usage``, ``error``) are filtered out so the
        result is that subsession's LLM context, in order — the subsession
        twin of :meth:`read_messages`.

        Args:
            subsession_id (str): Subsession identifier.

        Returns:
            list[dict[str, object]]: Ordered ``{role, content}`` dicts (empty if
            the subsession file does not exist).
        """
        if self.__paths is None:
            return []
        return [line for line in self.read_subsession_lines(subsession_id) if "role" in line]

    def __subsession_path(self, subsession_id: str) -> Path:
        assert self.__paths is not None
        return self.__paths.subsessions / f"{subsession_id}.jsonl"

    @staticmethod
    def __append_line(path: Path, record: dict[str, object]) -> None:
        """Write one JSONL line, stamping ``id``/``ts`` if the caller hasn't.

        Every persisted entry — message or marker, main log or subsession —
        goes through here, so this is the single place that guarantees both
        fields are present: ``id`` (a stable identifier a later entry can
        reference, e.g. to reconcile a dangling tool call once it resolves)
        and ``ts`` (ISO-8601 UTC, for the client's eventual chronological
        display — not read by anything server-side; append order is already
        chronological order).
        """
        record.setdefault("id", uuid.uuid4().hex)
        record.setdefault("ts", datetime.now(tz=UTC).isoformat())
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

    def __load_transient(self, paths: _SessionPaths) -> None:
        if not paths.transient.exists():
            return
        try:
            data = json.loads(paths.transient.read_text(encoding="utf-8"))
            self.__stage = str(data.get("stage", "IDLE"))
            self.__last_prompt = str(data.get("last_prompt", ""))
            self.__autonomous = bool(data.get("autonomous", False))
            raw_workflow_mode = data.get("workflow_mode")
            self.__workflow_mode = (
                raw_workflow_mode if raw_workflow_mode in ("problem_solving", "judge") else "guided"
            )
            edit = data.get("edit_control")
            self.__edit_control = edit if edit in ("review_all", "allow_all", "smart") else "smart"
            command = data.get("command_control")
            self.__command_control = (
                command if command in ("defensive", "permissive", "smart") else "smart"
            )
            self.__thinking_level = str(data.get("thinking_level", ""))
            raw_rules = data.get("security_rules")
            self.__security_rules = (
                frozenset(
                    (str(rule[0]), str(rule[1]))
                    for rule in raw_rules
                    if isinstance(rule, list) and len(rule) == 2
                )
                if isinstance(raw_rules, list)
                else frozenset()
            )
            raw_path_rules = data.get("security_path_rules")
            self.__security_path_rules = (
                frozenset(
                    (str(rule[0]), str(rule[1]))
                    for rule in raw_path_rules
                    if isinstance(rule, list) and len(rule) == 2
                )
                if isinstance(raw_path_rules, list)
                else frozenset()
            )
            pending = data.get("pending_prompt")
            self.__pending_prompt = pending if isinstance(pending, dict) else None
            alert = data.get("pending_security_alert")
            self.__pending_security_alert = alert if isinstance(alert, str) and alert else None
            review = data.get("pending_edit_review")
            self.__pending_edit_review = review if isinstance(review, str) and review else None
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
        tool-call documents) so the session list can show recency. Strictly
        increases even when the wall clock's resolution (coarser on some
        Windows configurations than the microsecond precision ``isoformat()``
        implies) yields the same instant as the previous stamp for two writes
        in quick succession.
        """
        if self.__paths is None:
            return
        now = datetime.now(tz=UTC)
        if self.__last_modified:
            try:
                previous = datetime.fromisoformat(self.__last_modified)
            except ValueError:
                previous = None
            if previous is not None and now <= previous:
                now = previous + timedelta(microseconds=1)
        self.__last_modified = now.isoformat()
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
            "edit_control": self.__edit_control,
            "command_control": self.__command_control,
            "thinking_level": self.__thinking_level,
            "security_rules": sorted([list(rule) for rule in self.__security_rules]),
            "security_path_rules": sorted([list(rule) for rule in self.__security_path_rules]),
            "pending_prompt": self.__pending_prompt,
            "pending_security_alert": self.__pending_security_alert,
            "pending_edit_review": self.__pending_edit_review,
            "active_subsession": self.__active_subsession,
            "current_project": self.__current_project,
        }
        paths.transient.write_text(json.dumps(data, indent=2), encoding="utf-8")
