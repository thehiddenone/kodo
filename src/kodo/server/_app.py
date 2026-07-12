"""aiohttp application factory and WebSocket endpoint for the Kōdo server.

The server is a machine-wide singleton: many VS Code windows connect to it, each
driving its own session.  Frames are routed by ``payload.session_id`` to the
owning :class:`~kodo.server.SessionManager` session; ``hello`` (the only frame
without a required ``session_id``) creates or resumes one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

from aiohttp import web

from kodo.binutils import ensure_all_utils
from kodo.llms import (
    LLMGateway,
    LLMRouting,
    LocalLLMEntry,
    Message,
    TokenDelta,
    TurnEnd,
    add_local_entry,
    clear_llama_server_override_path,
    detect_ram_gb,
    detect_vram_gb,
    get_cloud_registry,
    get_cloud_vendor_display_name,
    get_llama_server_override_path,
    get_local_registry,
    local_thinking_default_tier,
    local_thinking_family,
    local_thinking_tiers,
    parse_llama_args,
    remove_local_entry,
    set_llama_server_override_path,
)
from kodo.llms.llamacpp import (
    LlamaPlugin,
    LlamaServer,
    LlamaServerConfig,
    ensure_llama_running,
    find_installed,
    find_running_server,
    get_local_model_manager,
    install_llamacpp,
)
from kodo.llms.local import LocalModelError
from kodo.project import ProjectLayoutError, WorkspaceLayout, kodo_user_dir
from kodo.runtime import CheckpointState, MirrorDirtyError
from kodo.subagents import AgentRegistry
from kodo.titling import warm_up_titler_cache
from kodo.transport import (
    EVT_ERROR,
    EVT_LLAMA_STATE,
    EVT_LLAMACPP_INSTALL_PROGRESS,
    EVT_LOCAL_LLM_REGISTRY_STATE,
    MSG_CHECKPOINT_LIST,
    MSG_CHECKPOINT_REDO,
    MSG_CHECKPOINT_ROLL_FORWARD,
    MSG_CHECKPOINT_ROLLBACK,
    MSG_CHECKPOINT_UNDO,
    MSG_COMMAND_CONTROL_SET,
    MSG_COMPACT_NOW,
    MSG_CONFIG_RELOAD,
    MSG_EDIT_CONTROL_SET,
    MSG_HELLO,
    MSG_LLAMA_SERVER_OVERRIDE_REMOVE,
    MSG_LLAMA_SERVER_OVERRIDE_SET,
    MSG_LLAMA_START,
    MSG_LLAMA_STOP,
    MSG_LLAMACPP_INSTALL,
    MSG_LLM_COMPLETE,
    MSG_LLM_SELECT,
    MSG_LOCAL_LLM_ADD_FILE,
    MSG_LOCAL_LLM_ADD_HUGGINGFACE,
    MSG_LOCAL_LLM_ADD_SERVER_URL,
    MSG_LOCAL_LLM_INSTALL,
    MSG_LOCAL_LLM_PAUSE,
    MSG_LOCAL_LLM_REMOVE,
    MSG_LOCAL_LLM_RESUME,
    MSG_LOCAL_LLM_UNINSTALL,
    MSG_MODE_SET,
    MSG_PROJECT_CREATE,
    MSG_PROJECT_SET,
    MSG_PROMPT_SUBMIT,
    MSG_SESSION_DELETE,
    MSG_SESSION_LIST,
    MSG_SESSION_RELEASE,
    MSG_STOP,
    MSG_THINKING_LEVEL_SET,
    MSG_WORKFLOW_SET,
    MSG_WORKSPACE_FOLDERS,
    Connection,
    Envelope,
)

from ._config import Config
from ._connection_registry import (
    CONNECTION_REGISTRY_KEY,
    ConnectionRegistry,
    HandlerFn,
    Request,
)
from ._session import Session
from ._session_manager import SessionManager

_log = logging.getLogger(__name__)

_SERVER_VERSION: str = "0.2.0b1"
_MANAGER_KEY: web.AppKey[SessionManager] = web.AppKey("session_manager")

# Subagents directory: kodo/subagents/ next to kodo/server/
_AGENTS_DIR = Path(__file__).parent.parent / "subagents"


# ------------------------------------------------------------------
# Startup validation + logging
# ------------------------------------------------------------------


def _check_git_on_path() -> None:
    if shutil.which("git") is None:
        _log.error("'git' is not on PATH.  Kōdo requires git.")
        sys.exit(1)


def _setup_log_file(layout: WorkspaceLayout, log_level: str) -> None:
    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        layout.server_log,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(log_level)
    logging.getLogger().addHandler(handler)
    _log.info("Log file: %s", layout.server_log)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _require_session(req: Request) -> Session | None:
    """Resolve the request's session, replying with an error if unknown."""
    if req.session is not None:
        return req.session
    await req.reply(
        {
            "type": "error",
            "code": "unknown_session",
            "message": f"No such session: {req.session_id!r}",
            "recoverable": True,
        }
    )
    return None


# ------------------------------------------------------------------
# hello — create or resume a session, bind the connection
# ------------------------------------------------------------------


def _make_hello_handler(config: Config) -> HandlerFn:
    async def _handle_hello(req: Request) -> None:
        payload = req.env.payload
        window_id = str(payload.get("window_id") or req.connection.id)
        role = str(payload.get("role") or "session")

        # A control connection (the window's sidebar) drives window-global,
        # session-less frames only (llama / model management, session.list).
        # It must NOT create or bind a session — it just needs the
        # model/llama snapshot.
        if role == "control":
            _log.info(
                "Hello (control) from client=%s window=%s",
                payload.get("client", "unknown"),
                window_id[:8],
            )
            await req.reply(
                {
                    "type": "hello.ack",
                    "role": "control",
                    "server_version": _SERVER_VERSION,
                    **_llama_payload(config.reload_settings()),
                }
            )
            return

        await _handle_session_hello(req, config, payload, window_id)

    return _handle_hello


def _validate_initial_thinking_level(config: Config, raw: object) -> str | None:
    """Validate an optional ``hello.thinking_level`` seed for a brand-new session.

    ``None`` (field absent, or invalid for whatever local model is currently
    configured) lets the new session fall back to its model's thinking-family
    default, same as if the field were never sent — the validator's RVP judge
    is the only caller (its preceding ``llm.select`` already switched the
    active model to the one this value must be valid for, so a mismatch here
    means a caller bug, and degrading silently keeps ``hello`` itself sturdy
    rather than failing the whole handshake over an optional field).
    """
    if raw is None:
        return None
    value = str(raw).strip()
    settings = config.reload_settings()
    models_map = settings.get("models")
    model_key = str(models_map.get("local", "")) if isinstance(models_map, dict) else ""
    entry = get_local_registry(kodo_user_dir()).get(model_key) if model_key else None
    base_llm = entry.base_llm if entry is not None else ""
    return value if value in local_thinking_tiers(base_llm) else None


async def _handle_session_hello(
    req: Request, config: Config, payload: dict[str, object], window_id: str
) -> None:

    requested = str(payload.get("session_id") or "")
    _log.info(
        "Hello from client=%s window=%s session=%s",
        payload.get("client", "unknown"),
        window_id[:8],
        requested or "<new>",
    )

    if requested:
        session = await req.manager.open(requested, window_id)
        if session is None:
            await req.reply({"type": "hello.ack", "error": "session_in_use"})
            return
    else:
        thinking_level = _validate_initial_thinking_level(config, payload.get("thinking_level"))
        session = await req.manager.create(window_id, thinking_level=thinking_level)

    await req.manager.bind_connection(session, req.connection)

    await req.reply(
        {
            "type": "hello.ack",
            "server_version": _SERVER_VERSION,
            "session_id": session.id,
            "current_project": session.engine.current_project,
            "state": session.engine.session.to_dict(),
            **_llama_payload(config.reload_settings()),
        }
    )

    await session.channel.send(Envelope.make_event("state", session.engine.session.to_dict()))
    await session.channel.send(
        Envelope.make_event(
            "session.name",
            {"session_id": session.id, "name": session.engine.session_name},
        )
    )
    history = await session.engine.history_entries()
    if history:
        await session.channel.send(Envelope.make_event("session.history", {"entries": history}))

    # Only now replay anything buffered while this session was disconnected
    # (e.g. a mid-turn tool_call whose frame never reached the old socket).
    # These must land strictly after session.history above, or the webview's
    # reducer can see a live tool_call before history and permanently drop the
    # scrollback (its "history already applied" guard trips on the wrong
    # condition — see kodo-vsix reducer.ts).
    await req.manager.replay_backlog(session)


async def _handle_ping(req: Request) -> None:
    await req.reply({"type": "pong"})


def _local_entry_installed_path(entry: LocalLLMEntry, kodo_dir: Path) -> str | None:
    """Absolute path to *entry*'s files on disk, once installed — else ``None``.

    Backs both ``installed`` (non-``None`` means installed) and
    ``installed_path`` (what "Show me local files" reveals) in the wire
    payload below.
    """
    if entry.kind == "custom_server_url":
        return None  # not a local file at all
    if entry.kind == "custom_file":
        return entry.path if Path(entry.path).is_file() else None
    path = get_local_model_manager(kodo_dir).get_model_path(entry.name)
    return str(path) if path is not None else None


def _local_entry_installed(entry: LocalLLMEntry, kodo_dir: Path) -> bool:
    if entry.kind == "custom_server_url":
        return True
    return _local_entry_installed_path(entry, kodo_dir) is not None


def _thinking_families_payload(registry: dict[str, LocalLLMEntry]) -> dict[str, object]:
    """``base_llm -> {family, tiers, default}`` for every base model with a
    thinking-tier mechanism (see ``kodo.llms.local_thinking_family``).

    Server-computed rather than a second table hardcoded in kodo-vsix, since
    family membership already lives in ``_local_registry.py`` as the single
    source of truth (also needed there for the launch-time CLI flags) — a
    duplicate client-side copy would risk drifting out of sync.
    """
    base_llms = {e.base_llm for e in registry.values() if e.base_llm}
    return {
        base_llm: {
            "family": local_thinking_family(base_llm),
            "tiers": list(local_thinking_tiers(base_llm)),
            "default": local_thinking_default_tier(base_llm),
        }
        for base_llm in base_llms
        if local_thinking_family(base_llm) is not None
    }


def _local_registry_payload() -> dict[str, object]:
    """The ``{local_registry, llama_server_override_path, detected_vram_gb,
    detected_ram_gb, thinking_families}`` shape shared by ``hello.ack`` and
    every ``local_llm.registry_state`` event.

    Download-in-progress state is deliberately **not** part of this payload —
    kodo-vsix reads ``manager-state.json`` directly off disk instead of
    waiting for a WS push (see doc/LOCAL_MODEL_MANAGER.md §11 and
    doc/LLM_REGISTRY.md §4.4); this keeps every open window in eventually-
    consistent agreement without the server needing to track or broadcast to
    more than the single connection that issued each request.
    """
    kodo_dir = kodo_user_dir()
    registry = get_local_registry(kodo_dir)
    local_payload = [
        {
            "name": e.name,
            "kind": e.kind,
            "description": e.description,
            "repo_id": e.repo_id,
            "filename": e.filename,
            "path": e.path,
            "url": e.url,
            "installed": _local_entry_installed(e, kodo_dir),
            "installed_path": _local_entry_installed_path(e, kodo_dir),
            "base_llm": e.base_llm,
            "quant_author": e.quant_author,
            "quant_type": e.quant_type,
            "size_hint": e.size_hint,
            "gpu_tip": e.gpu_tip,
            "mac_tip": e.mac_tip,
            "min_memory": e.min_memory,
            "memory": e.memory,
        }
        for e in registry.values()
    ]
    return {
        "local_registry": local_payload,
        "llama_server_override_path": get_llama_server_override_path(kodo_dir),
        "detected_vram_gb": detect_vram_gb(),
        "detected_ram_gb": detect_ram_gb(),
        "thinking_families": _thinking_families_payload(registry),
    }


def _cloud_registry_payload() -> dict[str, object]:
    return {
        vendor: {
            "display_name": get_cloud_vendor_display_name(vendor),
            "models": [
                {
                    "model_id": m.model_id,
                    "name": m.name,
                    "description": m.description,
                    "context_window": m.context_window,
                    "recommendation": m.recommendation,
                }
                for m in models
            ],
        }
        for vendor, models in get_cloud_registry().items()
    }


def _llama_payload(settings: dict[str, object] | None = None) -> dict[str, object]:
    llama = find_installed(kodo_user_dir())
    active = LlamaServer.get_active_llama_server()
    llama_is_running = active is not None and active.is_running
    active_vendor = str((settings or {}).get("active_cloud_vendor", "anthropic"))
    return {
        "cloud_registry": _cloud_registry_payload(),
        "active_cloud_vendor": active_vendor,
        **_local_registry_payload(),
        "llama_installed": llama is not None,
        "llama_version": f"b{llama.build}" if llama is not None else None,
        "llama_running": llama_is_running,
        "llama_model": active.model_name if llama_is_running and active is not None else None,
    }


# ------------------------------------------------------------------
# Session list / release
# ------------------------------------------------------------------


async def _handle_session_list(req: Request) -> None:
    await req.reply({"type": "session.list.ack", "sessions": req.manager.list_sessions()})


async def _handle_session_release(req: Request) -> None:
    if req.session_id:
        req.manager.release(req.session_id)
    await req.reply({"type": "session.release.ack"})


async def _handle_session_delete(req: Request) -> None:
    """Delete the session's files; on success close the socket, else reply error.

    The client reads a clean socket closure as confirmation (and closes the tab);
    on error it keeps the socket open and surfaces ``message``.
    """
    session = await _require_session(req)
    if session is None:
        return
    try:
        await req.manager.delete(req.session_id)
    except Exception as exc:  # noqa: BLE001 — any failure is reported to the client
        _log.exception("Failed to delete session %s", req.session_id)
        await req.reply({"type": "session.delete.error", "message": str(exc)})
        return
    # The session is gone: close the socket so the client treats the closure as
    # success. (drop_connection is a no-op now — delete() already detached it.)
    await req.connection.ws.close()


# ------------------------------------------------------------------
# Session-scoped engine handlers
# ------------------------------------------------------------------


async def _handle_prompt(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    text = str(req.env.payload.get("text", "")).strip()
    if not text:
        await req.reply(
            {
                "type": "error",
                "code": "empty_prompt",
                "message": "Prompt text is required.",
                "recoverable": True,
            }
        )
        return
    _log.info("Prompt submitted (session=%s): %r", session.id, text[:80])
    await req.reply({"type": "prompt.accepted"})
    await session.engine.handle_prompt_submit(text, req.env.id)


async def _handle_mode(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_mode_set(bool(req.env.payload.get("autonomous", False)))
    await req.reply({"type": "mode.accepted"})


async def _handle_workflow(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_workflow_set(str(req.env.payload.get("mode", "guided")))
    await req.reply({"type": "workflow.accepted"})


async def _handle_edit_control(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_edit_control_set(str(req.env.payload.get("edit_control", "smart")))
    await req.reply({"type": "edit_control.accepted"})


async def _handle_command_control(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_command_control_set(
        str(req.env.payload.get("command_control", "smart"))
    )
    await req.reply({"type": "command_control.accepted"})


async def _handle_thinking_level(req: Request) -> None:
    """``thinking_level.set {thinking_level}`` (WS_PROTOCOL.md §7.x).

    Unlike edit_control.set/command_control.set, the value can be rejected —
    the valid set depends on the session's active local model — so the reply
    carries ``ok`` for the client to act on (a stale/racing client is the only
    expected failure mode; the client that computed the request already knows
    the valid set).
    """
    session = await _require_session(req)
    if session is None:
        return
    ok = await session.engine.handle_thinking_level_set(
        str(req.env.payload.get("thinking_level", "")).strip()
    )
    await req.reply({"type": "thinking_level.accepted", "ok": ok})


async def _handle_workspace_folders(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    physical_root = str(req.env.payload.get("physical_root", ""))
    raw = req.env.payload.get("folders", {})
    folders = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    await session.engine.handle_workspace_folders(physical_root, folders)
    await req.reply({"type": "workspace.folders.ack"})


async def _handle_project_set(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    root = str(req.env.payload.get("root", "")).strip()
    name = str(req.env.payload.get("name", "")).strip()
    if not root:
        await req.reply(
            {
                "type": "error",
                "code": "missing_project_root",
                "message": "project.set requires a 'root'.",
                "recoverable": True,
            }
        )
        return
    await session.engine.bind_project(root, name or root)
    await req.reply({"type": "project.accepted"})


async def _handle_project_create(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    path = str(req.env.payload.get("path", "")).strip()
    name = str(req.env.payload.get("name", "")).strip()
    force = bool(req.env.payload.get("force", False))
    if not path and not name:
        await req.reply(
            {
                "type": "error",
                "code": "missing_project_name_or_path",
                "message": "project.create requires a 'path' or 'name'.",
                "recoverable": True,
            }
        )
        return
    try:
        result = await session.engine.handle_project_create(name, path or None, force)
    except ProjectLayoutError as exc:
        await req.reply({"type": "project.create.error", "message": str(exc)})
        return
    await req.reply({"type": "project.create.done", **result})


async def _handle_stop(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.stop()
    await req.reply({"type": "stop.accepted"})


async def _handle_compact(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    await session.engine.handle_compact_now()
    await req.reply({"type": "compact.accepted"})


def _checkpoint_state_payload(state: CheckpointState) -> dict[str, object]:
    """The wire shape for a CheckpointState, shared by every checkpoint reply."""
    return {
        "current_index": state.current_index,
        "entries": [{"sha": e.sha, "undone": e.undone} for e in state.entries],
    }


async def _checkpoint_request(req: Request) -> tuple[Session, str, str, str | None] | None:
    """Shared ``(session, root, sha, resolution)`` extraction for checkpoint ops.

    ``resolution`` (``"stash"|"discard"``) is only present on a retry after a
    ``*.needs_confirmation`` reply caused by a dirty work tree.
    """
    session = await _require_session(req)
    if session is None:
        return None
    root = str(req.env.payload.get("root", ""))
    sha = str(req.env.payload.get("sha", ""))
    resolution = req.env.payload.get("resolution")
    return session, root, sha, str(resolution) if isinstance(resolution, str) else None


async def _reply_checkpoint_done(
    req: Request, verb: str, root: str, sha: str, state: CheckpointState
) -> None:
    payload = {"type": f"checkpoint.{verb}.done", "root": root, "sha": sha}
    await req.reply({**payload, **_checkpoint_state_payload(state)})


async def _reply_needs_confirmation(req: Request, verb: str, root: str, sha: str) -> None:
    await req.reply({"type": f"checkpoint.{verb}.needs_confirmation", "root": root, "sha": sha})


async def _handle_checkpoint_rollback(req: Request) -> None:
    parsed = await _checkpoint_request(req)
    if parsed is None:
        return
    session, root, sha, resolution = parsed
    try:
        state = await session.engine.handle_checkpoint_rollback(root, sha, resolution)
    except MirrorDirtyError:
        await _reply_needs_confirmation(req, "rollback", root, sha)
        return
    await _reply_checkpoint_done(req, "rollback", root, sha, state)


async def _handle_checkpoint_roll_forward(req: Request) -> None:
    parsed = await _checkpoint_request(req)
    if parsed is None:
        return
    session, root, sha, resolution = parsed
    try:
        state = await session.engine.handle_checkpoint_roll_forward(root, sha, resolution)
    except MirrorDirtyError:
        await _reply_needs_confirmation(req, "roll_forward", root, sha)
        return
    await _reply_checkpoint_done(req, "roll_forward", root, sha, state)


async def _handle_checkpoint_undo(req: Request) -> None:
    parsed = await _checkpoint_request(req)
    if parsed is None:
        return
    session, root, sha, resolution = parsed
    try:
        state = await session.engine.handle_checkpoint_undo(root, sha, resolution)
    except MirrorDirtyError:
        await _reply_needs_confirmation(req, "undo", root, sha)
        return
    await _reply_checkpoint_done(req, "undo", root, sha, state)


async def _handle_checkpoint_redo(req: Request) -> None:
    parsed = await _checkpoint_request(req)
    if parsed is None:
        return
    session, root, sha, resolution = parsed
    try:
        state = await session.engine.handle_checkpoint_redo(root, sha, resolution)
    except MirrorDirtyError:
        await _reply_needs_confirmation(req, "redo", root, sha)
        return
    await _reply_checkpoint_done(req, "redo", root, sha, state)


async def _handle_checkpoint_list(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    root = str(req.env.payload.get("root", ""))
    state = await session.engine.handle_checkpoint_list(root)
    payload = {"type": "checkpoint.list.done", "root": root}
    await req.reply({**payload, **_checkpoint_state_payload(state)})


def _make_config_reload_handler(config: Config) -> HandlerFn:
    async def _handle_config_reload(req: Request) -> None:
        try:
            config.reload_settings()
            # The model selection is window-global; notify every live session so
            # a switch to a smaller-context model can compact right away.
            for session in req.manager.live_sessions():
                await session.engine.handle_config_changed()
            await req.reply({"type": "config.reload.ack"})
        except Exception as exc:  # noqa: BLE001
            await req.reply(
                {
                    "type": "error",
                    "code": "config_reload_failed",
                    "message": str(exc),
                    "recoverable": True,
                }
            )

    return _handle_config_reload


# ------------------------------------------------------------------
# llama / model management (process-global; reply on the connection)
# ------------------------------------------------------------------


async def _handle_llamacpp_install(req: Request) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()

    def progress_cb(pct: int, msg: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (pct, msg))

    async def run() -> None:
        try:
            await asyncio.to_thread(install_llamacpp, kodo_user_dir(), progress_cb=progress_cb)
        except Exception:  # noqa: BLE001
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.create_task(run())
    while True:
        item = await queue.get()
        if item is None:
            break
        pct, msg = item
        await req.connection.send(
            Envelope.make_event(EVT_LLAMACPP_INSTALL_PROGRESS, {"percent": pct, "message": msg})
        )


def _run_background_download(
    model_id: str, work: Callable[[], object], connection: Connection
) -> None:
    """Fire-and-forget a blocking download/resume call on a worker thread.

    Byte-level progress is **not** streamed back over this (or any) connection
    — kodo-vsix follows it by polling ``manager-state.json`` directly off disk
    instead (see doc/LOCAL_MODEL_MANAGER.md §11), which is what lets the
    transfer survive the requesting connection/window closing entirely.

    The *outcome* is different: once ``work`` finishes (successfully, with a
    ``LocalModelError``, or with any other exception), a fresh
    ``local_llm.registry_state`` is pushed back on *connection* — the same
    event every other ``local_llm.*`` mutation already replies with — so the
    requesting window's sidebar and Local Inference Settings panel pick up
    the new ``installed``/``installed_path`` state without needing to
    reconnect or reopen the panel. A ``LocalModelError`` also gets an ``error``
    event of its own (same ``local_llm_error`` code as the synchronous
    validation failures in this module) *before* that registry_state push, so
    kodo-vsix surfaces it as a notification instead of the failure only ever
    reaching the server log — see doc/LOCAL_MODEL_MANAGER.md §11. Best-effort:
    ``Connection.send`` silently no-ops if the socket already closed (e.g. the
    window closed mid-download).
    """

    async def run() -> None:
        try:
            await asyncio.to_thread(work)
        except LocalModelError as exc:
            _log.exception("Background download failed for %r", model_id)
            await connection.send(
                Envelope.make_event(
                    EVT_ERROR,
                    {
                        "code": "local_llm_error",
                        "message": f"Download of {model_id!r} failed: {exc}",
                        "recoverable": True,
                    },
                )
            )
        finally:
            await connection.send(
                Envelope.make_event(EVT_LOCAL_LLM_REGISTRY_STATE, _local_registry_payload())
            )

    asyncio.create_task(run())


async def _handle_local_llm_install(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    if not name:
        return
    kodo_dir = kodo_user_dir()
    entry = get_local_registry(kodo_dir).get(name)
    if entry is None or entry.kind not in ("hardcoded_hf", "custom_hf"):
        await _reply_local_llm_error(req, f"Unknown or non-downloadable model: {name!r}")
        return
    manager = get_local_model_manager(kodo_dir)
    # Kickoff state must be *sent* (not just scheduled) before the background
    # task is created — otherwise the completion push racing the kickoff
    # send on independent await chains could land the two registry_state
    # events on the wire out of order (observed with a near-instant fake
    # download in tests; a real multi-second HF transfer masks it, but
    # nothing guarantees that).
    await _send_registry_state(req)
    _run_background_download(
        name,
        lambda: manager.download_model(entry.name, entry.repo_id, entry.filename),
        req.connection,
    )


async def _handle_local_llm_resume(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    if not name:
        return
    manager = get_local_model_manager(kodo_user_dir())
    if manager.get_record(name) is None:
        await _reply_local_llm_error(req, f"No download record for {name!r} — nothing to resume")
        return
    await _send_registry_state(req)  # see the ordering note in _handle_local_llm_install
    _run_background_download(name, lambda: manager.resume_download(name), req.connection)


async def _handle_local_llm_pause(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    if name:
        get_local_model_manager(kodo_user_dir()).pause_download(name)
        _log.info("Paused download %r", name)
    await _send_registry_state(req)


async def _handle_local_llm_uninstall(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    if name:
        await asyncio.to_thread(get_local_model_manager(kodo_user_dir()).uninstall, name)
        _log.info("Uninstalled model %r", name)
    await _send_registry_state(req)


async def _send_registry_state(req: Request) -> None:
    await req.connection.send(
        Envelope.make_event(EVT_LOCAL_LLM_REGISTRY_STATE, _local_registry_payload())
    )


async def _reply_local_llm_error(req: Request, message: str) -> None:
    await req.connection.send(
        Envelope.make_event(
            EVT_ERROR, {"code": "local_llm_error", "message": message, "recoverable": True}
        )
    )


def _parse_context_window(raw: object) -> int:
    try:
        return int(cast(int, raw) or 0)
    except (TypeError, ValueError):
        return 0


async def _handle_local_llm_add_huggingface(req: Request) -> None:
    payload = req.env.payload
    entry = LocalLLMEntry(
        name=str(payload.get("name", "")).strip(),
        kind="custom_hf",
        description=str(payload.get("description", "")),
        repo_id=str(payload.get("repo_id", "")).strip(),
        filename=str(payload.get("filename", "")).strip(),
        llama_args=parse_llama_args(payload.get("llama_args", {})),
        context_window=_parse_context_window(payload.get("context_window", 0)),
    )
    if not entry.name or not entry.repo_id or not entry.filename:
        await _reply_local_llm_error(req, "name, repo_id, and filename are all required")
        return
    try:
        add_local_entry(kodo_user_dir(), entry)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    await _send_registry_state(req)


async def _handle_local_llm_add_file(req: Request) -> None:
    payload = req.env.payload
    entry = LocalLLMEntry(
        name=str(payload.get("name", "")).strip(),
        kind="custom_file",
        description=str(payload.get("description", "")),
        path=str(payload.get("path", "")).strip(),
        llama_args=parse_llama_args(payload.get("llama_args", {})),
        context_window=_parse_context_window(payload.get("context_window", 0)),
    )
    if not entry.name or not entry.path:
        await _reply_local_llm_error(req, "name and path are both required")
        return
    try:
        add_local_entry(kodo_user_dir(), entry)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    await _send_registry_state(req)


async def _handle_local_llm_add_server_url(req: Request) -> None:
    payload = req.env.payload
    entry = LocalLLMEntry(
        name=str(payload.get("name", "")).strip(),
        kind="custom_server_url",
        description=str(payload.get("description", "")),
        url=str(payload.get("url", "")).strip(),
    )
    if not entry.name or not entry.url:
        await _reply_local_llm_error(req, "name and url are both required")
        return
    try:
        add_local_entry(kodo_user_dir(), entry)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    await _send_registry_state(req)


async def _handle_local_llm_remove(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    kodo_dir = kodo_user_dir()
    try:
        entry = get_local_registry(kodo_dir).get(name)
        is_downloadable = entry is not None and entry.kind in ("hardcoded_hf", "custom_hf")
        manager = get_local_model_manager(kodo_dir)
        # get_record (not get_model_path) so a *partial* download record isn't
        # orphaned in manager-state.json when its registry entry disappears —
        # get_model_path is None for anything not yet fully installed.
        if is_downloadable and manager.get_record(name) is not None:
            await asyncio.to_thread(manager.uninstall, name)
            _log.info("Uninstalled model %r", name)
        remove_local_entry(kodo_dir, name)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    await _send_registry_state(req)


async def _handle_llama_server_override_set(req: Request) -> None:
    path = str(req.env.payload.get("path", "")).strip()
    try:
        set_llama_server_override_path(kodo_user_dir(), path)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    await _send_registry_state(req)


async def _handle_llama_server_override_remove(req: Request) -> None:
    clear_llama_server_override_path(kodo_user_dir())
    await _send_registry_state(req)


def _make_llama_start_handler(config: Config) -> HandlerFn:
    async def _handle_llama_start(req: Request) -> None:
        user_dir = kodo_user_dir()
        settings = config.reload_settings()
        models_map = settings.get("models", {})
        model_name = str(models_map.get("local", "") if isinstance(models_map, dict) else "")
        if not model_name:
            await req.connection.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE,
                    {"running": False, "model": None, "error": "No local model selected"},
                )
            )
            return
        registry = get_local_registry(user_dir)
        entry = registry.get(model_name)
        if entry is None:
            error = f"Unknown local model: {model_name!r}"
            await req.connection.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE, {"running": False, "model": None, "error": error}
                )
            )
            return

        if entry.kind == "custom_server_url":
            # Not managed by kodo — stop our own server (if any) and report it
            # stopped; the plugin itself points its client at entry.url on the
            # next dispatch (see LlamaPlugin.__ensure_running).
            managed = LlamaServer.get_active_llama_server()
            if managed is not None and managed.is_running:
                await managed.stop()
            await req.connection.send(
                Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None})
            )
            return

        try:
            server = await ensure_llama_running(entry, user_dir)
        except Exception as exc:  # noqa: BLE001
            await req.connection.send(
                Envelope.make_event(
                    EVT_LLAMA_STATE, {"running": False, "model": None, "error": str(exc)}
                )
            )
            return
        await req.connection.send(
            Envelope.make_event(
                EVT_LLAMA_STATE, {"running": True, "model": server.model_name, "port": server.port}
            )
        )

    return _handle_llama_start


async def _handle_llama_stop(req: Request) -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None:
        await server.stop()
    await req.connection.send(
        Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None})
    )


# ------------------------------------------------------------------
# Synchronous model selection + one-shot completion (doc/WS_PROTOCOL.md
# §7.6a/§7.6b) — built for kodo.validator's LUT↔VLLM swaps, usable by any
# client.
# ------------------------------------------------------------------


def _persist_local_model_selection(name: str) -> None:
    """Write ``mode: "local"`` + ``models.local = name`` into settings.json.

    Patches the raw user file (not the merged defaults view), so unrelated
    keys the user never set stay absent. Every engine dispatch re-reads
    settings from disk, so live sessions pick the new model up on their next
    LLM call with no further signal.
    """
    path = WorkspaceLayout().settings_json
    data: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Rewriting unreadable settings file %s: %s", path, exc)
    models = data.get("models")
    if not isinstance(models, dict):
        models = {}
    models["local"] = name
    data["mode"] = "local"
    data["models"] = models
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _handle_llm_select(req: Request) -> None:
    """``llm.select {name}`` — switch the active local model and confirm readiness.

    Persists the selection, then (re)starts llama-server for it and waits
    until it actually serves — the correlated ``llm.select.done`` reply is
    the caller's guarantee that the next dispatch hits the requested model.
    A failed start still leaves the selection persisted (matching what a
    settings-write + failed ``llama.start`` would leave behind); the caller
    decides whether to retry or select something else.

    Carries no thinking-tier field: thinking is session-scoped (doc/
    SESSIONS.md), so the validator's RVP judge — the one caller that used to
    need this — pins its tier via its own ``hello``'s ``thinking_level``
    field once its session actually exists, instead of persisting through
    here first.
    """

    def _fail(error: str, *, model: str | None = None) -> dict[str, object]:
        return {"type": "llm.select.done", "ok": False, "model": model, "error": error}

    name = str(req.env.payload.get("name", "")).strip()
    if not name:
        await req.reply(_fail("name is required"))
        return
    user_dir = kodo_user_dir()
    entry = get_local_registry(user_dir).get(name)
    if entry is None:
        await req.reply(_fail(f"Unknown local model: {name!r}"))
        return

    _persist_local_model_selection(name)

    if entry.kind == "custom_server_url":
        # Externally-managed server: nothing to start; stop our own process so
        # it is not shadowing the external one (same rule as llama.start).
        managed = LlamaServer.get_active_llama_server()
        if managed is not None and managed.is_running:
            await managed.stop()
        await req.connection.send(
            Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None})
        )
        await req.reply({"type": "llm.select.done", "ok": True, "model": name})
        return

    try:
        server = await ensure_llama_running(entry, user_dir)
    except Exception as exc:  # noqa: BLE001 — startup failure is the reply, not a crash
        await req.connection.send(
            Envelope.make_event(
                EVT_LLAMA_STATE, {"running": False, "model": None, "error": str(exc)}
            )
        )
        await req.reply(_fail(str(exc), model=name))
        return
    await req.connection.send(
        Envelope.make_event(
            EVT_LLAMA_STATE, {"running": True, "model": server.model_name, "port": server.port}
        )
    )
    await req.reply({"type": "llm.select.done", "ok": True, "model": server.model_name})


def _make_llm_complete_handler(config: Config, gateway: LLMGateway) -> HandlerFn:
    """``llm.complete {prompt, system?, json_schema?, thinking_level?}`` — one-shot
    local completion.

    A single tool-less turn on the currently selected local model, scheduled
    through the shared gateway feed (serializing with session dispatches).
    The full response text comes back in the correlated reply; no stream
    frames are emitted. ``json_schema`` grammar-constrains the output.

    ``thinking_level`` (a valid tier slug for the active model's thinking
    family) is a pure per-call override — built for the validator's
    User-Proxy answers (doc/VALIDATOR.md §9), which pin a low tier so
    ``ask_user`` answers don't burn time thinking. This call has no session
    to persist into, so there is nothing else for it to affect.
    """

    async def _handle_llm_complete(req: Request) -> None:
        def _fail(error: str, *, model: str | None = None) -> dict[str, object]:
            return {"type": "llm.complete.done", "ok": False, "model": model, "error": error}

        payload = req.env.payload
        prompt = str(payload.get("prompt", ""))
        if not prompt:
            await req.reply(_fail("prompt is required"))
            return
        schema_raw = payload.get("json_schema")
        if schema_raw is not None and not isinstance(schema_raw, dict):
            await req.reply(_fail("json_schema must be a JSON object"))
            return
        schema = cast("dict[str, object] | None", schema_raw)

        settings = config.reload_settings()
        models_map = settings.get("models", {})
        model = str(models_map.get("local", "") if isinstance(models_map, dict) else "")
        user_dir = kodo_user_dir()
        entry = get_local_registry(user_dir).get(model) if model else None
        if not model or entry is None:
            await req.reply(_fail("No local model selected — llm.complete is local-only"))
            return

        thinking_level_raw = payload.get("thinking_level")
        thinking_level: str | None = None
        if thinking_level_raw is not None:
            thinking_level = str(thinking_level_raw).strip()
            tiers = local_thinking_tiers(entry.base_llm)
            if not tiers:
                await req.reply(
                    _fail(
                        f"{model!r} has no thinking-tier family; thinking_level is not applicable",
                        model=model,
                    )
                )
                return
            if thinking_level not in tiers:
                await req.reply(
                    _fail(
                        f"Invalid thinking_level {thinking_level!r} for {model!r}; "
                        f"expected one of {list(tiers)}",
                        model=model,
                    )
                )
                return

        plugin = LlamaPlugin(sink=req.connection, kodo_dir=user_dir)
        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        try:
            async for event in gateway.stream_query(
                routing=LLMRouting(residence="local"),
                plugin=plugin,
                sink=req.connection,
                stream_id=uuid.uuid4().hex,
                model=model,
                system=str(payload.get("system", "")),
                messages=[Message(role="user", content=prompt)],
                tools=[],
                cache_breakpoints=[],
                json_schema=schema,
                thinking_level=thinking_level,
            ):
                if isinstance(event, TokenDelta):
                    text_parts.append(event.text)
                elif isinstance(event, TurnEnd):
                    input_tokens = event.usage.input_tokens
                    output_tokens = event.usage.output_tokens
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller, not a crash
            await req.reply(_fail(str(exc), model=model))
            return
        await req.reply(
            {
                "type": "llm.complete.done",
                "ok": True,
                "model": model,
                "text": "".join(text_parts),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )

    return _handle_llm_complete


# ------------------------------------------------------------------
# App factory
# ------------------------------------------------------------------


async def _start_background(app: web.Application) -> None:
    user_dir = kodo_user_dir()

    # Ensure the bundled third-party utils (uv, ripgrep, fd) are present under
    # ~/.kodo/bin, and the session-titler model is cached under
    # ~/.kodo/titler (kodo.titling — doc/INTERNALS.md §10c). Both are
    # best-effort and idempotent: a no-op once already present, so this only
    # does real work on a first console-style launch. Off the event loop
    # (asyncio.to_thread, run concurrently) so a first-run download of either
    # does not block server readiness any longer than the slower of the two.
    await asyncio.gather(
        asyncio.to_thread(ensure_all_utils, user_dir),
        asyncio.to_thread(warm_up_titler_cache),
    )

    running = find_running_server(user_dir)
    if running is not None:
        llama_install = find_installed(user_dir)
        model_path = (
            get_local_model_manager(user_dir).get_model_path(running.model)
            if running.model
            else None
        )
        if llama_install is not None and model_path is not None:
            cfg = LlamaServerConfig(
                executable=llama_install.executable,
                model_path=model_path,
                kodo_dir=user_dir,
                model_name=running.model,
                host=running.host,
                port=running.port,
            )
            LlamaServer(cfg).adopt(running)


async def _stop_background(app: web.Application) -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None and server.is_running:
        await server.stop()
    await app[_MANAGER_KEY].shutdown()


async def _ws_endpoint(request: web.Request) -> web.WebSocketResponse:
    return await request.app[CONNECTION_REGISTRY_KEY].run_ws(request)


def create_app(config: Config) -> web.Application:
    """Build and configure the singleton-server aiohttp application.

    Args:
        config: Resolved server configuration.

    Returns:
        web.Application: Ready-to-serve aiohttp application.
    """
    _check_git_on_path()

    layout = WorkspaceLayout()
    layout.init()
    _setup_log_file(layout, config.log_level)

    registry = AgentRegistry(_AGENTS_DIR)
    gateway = LLMGateway(
        cloud_concurrency=lambda: _cloud_concurrency(config),
    )
    manager = SessionManager(
        registry=registry,
        gateway=gateway,
        get_settings=config.reload_settings,
        layout=layout,
    )
    conn_registry = ConnectionRegistry(manager)

    conn_registry.register_handler(MSG_HELLO, _make_hello_handler(config))
    conn_registry.register_handler(MSG_SESSION_LIST, _handle_session_list)
    conn_registry.register_handler(MSG_SESSION_RELEASE, _handle_session_release)
    conn_registry.register_handler(MSG_SESSION_DELETE, _handle_session_delete)
    conn_registry.register_handler(MSG_PROMPT_SUBMIT, _handle_prompt)
    conn_registry.register_handler(MSG_MODE_SET, _handle_mode)
    conn_registry.register_handler(MSG_WORKFLOW_SET, _handle_workflow)
    conn_registry.register_handler(MSG_EDIT_CONTROL_SET, _handle_edit_control)
    conn_registry.register_handler(MSG_COMMAND_CONTROL_SET, _handle_command_control)
    conn_registry.register_handler(MSG_THINKING_LEVEL_SET, _handle_thinking_level)
    conn_registry.register_handler(MSG_WORKSPACE_FOLDERS, _handle_workspace_folders)
    conn_registry.register_handler(MSG_PROJECT_SET, _handle_project_set)
    conn_registry.register_handler(MSG_PROJECT_CREATE, _handle_project_create)
    conn_registry.register_handler(MSG_STOP, _handle_stop)
    conn_registry.register_handler(MSG_COMPACT_NOW, _handle_compact)
    conn_registry.register_handler(MSG_CHECKPOINT_ROLLBACK, _handle_checkpoint_rollback)
    conn_registry.register_handler(MSG_CHECKPOINT_ROLL_FORWARD, _handle_checkpoint_roll_forward)
    conn_registry.register_handler(MSG_CHECKPOINT_UNDO, _handle_checkpoint_undo)
    conn_registry.register_handler(MSG_CHECKPOINT_REDO, _handle_checkpoint_redo)
    conn_registry.register_handler(MSG_CHECKPOINT_LIST, _handle_checkpoint_list)
    conn_registry.register_handler(MSG_CONFIG_RELOAD, _make_config_reload_handler(config))
    conn_registry.register_handler(MSG_LLAMACPP_INSTALL, _handle_llamacpp_install)
    conn_registry.register_handler(MSG_LOCAL_LLM_INSTALL, _handle_local_llm_install)
    conn_registry.register_handler(MSG_LOCAL_LLM_RESUME, _handle_local_llm_resume)
    conn_registry.register_handler(MSG_LOCAL_LLM_PAUSE, _handle_local_llm_pause)
    conn_registry.register_handler(MSG_LOCAL_LLM_UNINSTALL, _handle_local_llm_uninstall)
    conn_registry.register_handler(MSG_LOCAL_LLM_REMOVE, _handle_local_llm_remove)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_HUGGINGFACE, _handle_local_llm_add_huggingface)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_FILE, _handle_local_llm_add_file)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_SERVER_URL, _handle_local_llm_add_server_url)
    conn_registry.register_handler(MSG_LLAMA_SERVER_OVERRIDE_SET, _handle_llama_server_override_set)
    conn_registry.register_handler(
        MSG_LLAMA_SERVER_OVERRIDE_REMOVE, _handle_llama_server_override_remove
    )
    conn_registry.register_handler(MSG_LLAMA_START, _make_llama_start_handler(config))
    conn_registry.register_handler(MSG_LLAMA_STOP, _handle_llama_stop)
    conn_registry.register_handler(MSG_LLM_SELECT, _handle_llm_select)
    conn_registry.register_handler(MSG_LLM_COMPLETE, _make_llm_complete_handler(config, gateway))

    app = web.Application()
    app[CONNECTION_REGISTRY_KEY] = conn_registry
    app[_MANAGER_KEY] = manager
    app.router.add_get("/ws", _ws_endpoint)
    app.on_startup.append(_start_background)
    app.on_shutdown.append(_stop_background)

    _log.info("Kōdo server %s — home=%s port=%d", _SERVER_VERSION, layout.kodo_dir, config.port)
    return app


def _cloud_concurrency(config: Config) -> int:
    raw = config.reload_settings().get("cloud_concurrency", 2)
    try:
        return max(1, int(str(raw)))
    except (TypeError, ValueError):
        return 2
