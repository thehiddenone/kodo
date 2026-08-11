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
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import cast

from aiohttp import web
from huggingface_hub.errors import GatedRepoError

from kodo.binutils import ensure_all_utils
from kodo.llms import (
    LLMGateway,
    LLMRouting,
    LocalLLMEntry,
    Message,
    TokenDelta,
    TurnEnd,
    add_local_entry,
    add_profile,
    clear_llama_server_override_path,
    detect_ram_gb,
    detect_vram_gb,
    get_active_profile,
    get_cloud_registry,
    get_cloud_vendor_display_name,
    get_knob_selections,
    get_llama_server_override_path,
    get_local_registry,
    get_profiles,
    llama_arg_catalog_to_json,
    local_thinking_default_tier,
    local_thinking_family,
    local_thinking_tiers,
    parse_llama_args,
    parse_llama_args_text,
    remove_local_entry,
    remove_profile,
    resolve_default_profile_args,
    sampling_specs_to_json,
    set_active_profile,
    set_knobs,
    set_llama_server_override_path,
    update_profile,
)
from kodo.llms.llamacpp import (
    LlamaInstall,
    LlamaPlugin,
    LlamaServer,
    LlamaServerConfig,
    build_exists,
    ensure_llama_running,
    fetch_latest_build_number,
    find_installed,
    find_running_server,
    get_local_model_manager,
    install_llamacpp,
    uninstall_llamacpp,
    update_llamacpp,
)
from kodo.llms.local import LocalModelError
from kodo.project import ProjectLayoutError, WorkspaceLayout, kodo_user_dir
from kodo.runtime import (
    CheckpointState,
    MirrorDirtyError,
    delete_global_security_rules,
    list_global_security_rules,
)
from kodo.subagents import AgentRegistry
from kodo.titling import (
    DEFAULT_HOUSEKEEPER_LLM_ID,
    HOUSEKEEPER_LLM_OPTIONS,
    start_titling,
    stop_titling,
)
from kodo.transport import (
    EVT_ERROR,
    EVT_HF_TOKEN_REVOKE,
    EVT_LLAMA_STATE,
    EVT_LLAMACPP_INSTALL_PROGRESS,
    EVT_LOCAL_LLM_REGISTRY_STATE,
    EVT_LOCAL_LLM_UPDATES_AVAILABLE,
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
    MSG_HOUSEKEEPER_LLM_GET,
    MSG_HOUSEKEEPER_LLM_SET,
    MSG_LLAMA_SERVER_OVERRIDE_REMOVE,
    MSG_LLAMA_SERVER_OVERRIDE_SET,
    MSG_LLAMA_START,
    MSG_LLAMA_STOP,
    MSG_LLAMACPP_INSTALL,
    MSG_LLAMACPP_UNINSTALL,
    MSG_LLAMACPP_UPDATE,
    MSG_LLAMACPP_VERSION_INFO,
    MSG_LLM_COMPLETE,
    MSG_LLM_SELECT,
    MSG_LOCAL_LLM_ADD_FILE,
    MSG_LOCAL_LLM_ADD_HUGGINGFACE,
    MSG_LOCAL_LLM_ADD_PROFILE,
    MSG_LOCAL_LLM_ADD_SERVER_URL,
    MSG_LOCAL_LLM_CHECK_UPDATES,
    MSG_LOCAL_LLM_INSTALL,
    MSG_LOCAL_LLM_PAUSE,
    MSG_LOCAL_LLM_REMOVE,
    MSG_LOCAL_LLM_REMOVE_PROFILE,
    MSG_LOCAL_LLM_RESUME,
    MSG_LOCAL_LLM_SET_ACTIVE_PROFILE,
    MSG_LOCAL_LLM_SET_KNOBS,
    MSG_LOCAL_LLM_UNINSTALL,
    MSG_LOCAL_LLM_UPDATE,
    MSG_LOCAL_LLM_UPDATE_PROFILE,
    MSG_MODE_SET,
    MSG_PROJECT_CREATE,
    MSG_PROMPT_SUBMIT,
    MSG_SAMPLING_SET,
    MSG_SECURITY_RULES_DELETE,
    MSG_SECURITY_RULES_LIST,
    MSG_SERVER_SHUTDOWN,
    MSG_SESSION_DELETE,
    MSG_SESSION_DELETE_BY_ID,
    MSG_SESSION_LIST,
    MSG_SESSION_RELEASE,
    MSG_SESSION_SECURITY_RULES_DELETE,
    MSG_SESSION_SECURITY_RULES_LIST,
    MSG_STOP,
    MSG_STUCK_DETECTION_GET,
    MSG_STUCK_DETECTION_SET,
    MSG_THINKING_LEVEL_SET,
    MSG_WORKFLOW_SET,
    MSG_WORKSPACE_FOLDERS,
    SREQ_HF_TOKEN_REQUEST,
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
    history = await session.engine.full_history()
    if history["entries"]:
        await session.channel.send(Envelope.make_event("session.history", history))

    # Only now replay anything buffered while this session was disconnected
    # (e.g. a mid-turn tool_call whose frame never reached the old socket),
    # plus any still-unanswered approval/question/permission/API-key prompt
    # (SessionManager.replay_backlog). These must land strictly after
    # session.history above, or the webview's reducer can see a live
    # tool_call before history and permanently drop the scrollback (its
    # "history already applied" guard trips on the wrong condition — see
    # kodo-vsix reducer.ts).
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
    family membership already lives in ``kodo.llms.local_registry._thinking``
    as the single source of truth (also needed there for the launch-time CLI
    flags) — a duplicate client-side copy would risk drifting out of sync.
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


def _knob_defs_payload(registry: dict[str, LocalLLMEntry]) -> dict[str, object]:
    """Every knob any entry offers, **deduplicated by id**.

    Knobs are overwhelmingly shared — all 82 hardcoded entries offer the same
    six, and the only per-family ones are the three YaRN context knobs — so
    repeating each definition (five options, each with a paragraph of help
    text) on every entry would dominate the payload. Entries carry a list of
    knob ids instead and look them up here. ``_validate_catalog`` guarantees
    two entries never disagree about what one id means, so this flattening is
    lossless.
    """
    defs: dict[str, object] = {}
    for entry in registry.values():
        for knob in entry.knobs:
            if knob.id in defs:
                continue
            defs[knob.id] = {
                "id": knob.id,
                "name": knob.name,
                "description": knob.description,
                "kind": knob.kind.value,
                "advanced": knob.advanced,
                "default": knob.resolved_default(),
                "options": [
                    {
                        "id": option.id,
                        "name": option.name,
                        "description": option.description,
                        "llama_args": option.llama_args,
                    }
                    for option in knob.options
                ],
                "flag": knob.flag,
                "minimum": knob.minimum,
                "maximum": knob.maximum,
                "step": knob.step,
                "unset_label": knob.unset_label,
            }
    return defs


def _profiles_payload(entry: LocalLLMEntry, kodo_dir: Path) -> list[dict[str, object]]:
    """*entry*'s user-defined profiles. Never includes the Default profile —
    that one has no stored args (see ``default_profile_args`` instead)."""
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "llama_args": p.llama_args,
        }
        for p in get_profiles(kodo_dir, entry)
    ]


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
            "llm_author": e.llm_author,
            "llamacpp_version": e.llamacpp_version,
            "context_window": e.context_window,
            "knobs": [k.id for k in e.knobs],
            "knob_selections": get_knob_selections(kodo_dir, e),
            # What those selections currently resolve to. Sent so the client
            # can show the effective context size (and the exact flags a knob
            # change produced) without re-implementing knob composition or
            # making a round trip — see doc/LLM_REGISTRY.md §4.6.
            "default_profile_args": resolve_default_profile_args(kodo_dir, e)
            if e.kind != "custom_server_url"
            else {},
            "profiles": _profiles_payload(e, kodo_dir),
            "active_profile": get_active_profile(kodo_dir, e.name),
        }
        for e in registry.values()
    ]
    return {
        "local_registry": local_payload,
        "llama_server_override_path": get_llama_server_override_path(kodo_dir),
        "detected_vram_gb": detect_vram_gb(),
        "detected_ram_gb": detect_ram_gb(),
        "thinking_families": _thinking_families_payload(registry),
        # Knob definitions, deduplicated across every entry — see
        # _knob_defs_payload. Entries reference these by id.
        "knob_defs": _knob_defs_payload(registry),
        # Static table, shipped with the registry rather than with every
        # per-session `state` push (it is ~27 entries of help text and
        # never changes at runtime). kodo-vsix renders the sampling modal
        # from this instead of hardcoding a second copy — same reasoning as
        # `thinking_families` above. See doc/SAMPLING.md.
        "sampling_specs": sampling_specs_to_json(),
        # The curated llama-server flag table the user-defined profile editor's
        # "Add argument" picker renders from (doc/LLM_REGISTRY.md §4.7). Static,
        # shipped here for the same reason `sampling_specs` is.
        "llama_arg_catalog": llama_arg_catalog_to_json(),
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
    """``session.list`` (WS_PROTOCOL.md §7.1b). *physical_root*/*folders* are
    optional — the requesting window's own current workspace shape, used to
    compute each locked session's ``workspace.compatible`` flag
    (:func:`~kodo.state.workspace_shape_compatible`). Parsing mirrors
    ``_handle_workspace_folders``'s exact idiom below."""
    physical_root = str(req.env.payload.get("physical_root", ""))
    raw_folders = req.env.payload.get("folders", {})
    folders = (
        {str(k): str(v) for k, v in raw_folders.items()} if isinstance(raw_folders, dict) else {}
    )
    await req.reply(
        {
            "type": "session.list.ack",
            "sessions": req.manager.list_sessions(physical_root=physical_root, folders=folders),
        }
    )


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


async def _handle_session_delete_by_id(req: Request) -> None:
    """Delete an arbitrary session by id from a management UI (e.g. the Kōdo
    Settings panel's "Sessions" list), over any connection — typically the
    control connection, not that session's own tab. Unlike
    ``_handle_session_delete`` this replies with an ack/error and never
    closes the request's socket, since that socket isn't dedicated to this
    one session.
    """
    session_id = req.session_id
    if not session_id:
        await req.reply(
            {
                "type": "session.delete_by_id.error",
                "message": "No session_id given.",
            }
        )
        return
    try:
        await req.manager.delete(session_id)
    except Exception as exc:  # noqa: BLE001 — any failure is reported to the client
        _log.exception("Failed to delete session %s", session_id)
        await req.reply({"type": "session.delete_by_id.error", "message": str(exc)})
        return
    await req.reply({"type": "session.delete_by_id.ack", "session_id": session_id})


# ------------------------------------------------------------------
# Global security rules (machine-wide, control connection) — Kōdo Settings
# panel's "Global Allow-Rules" section (doc/SECURITY_RULES_PLAN.md §Phase 3
# item 2). Session-scoped rules are handled just below — they live in
# per-session runtime state rather than this machine-wide store.
# ------------------------------------------------------------------


async def _handle_security_rules_list(req: Request) -> None:
    await req.reply({"type": "security.rules.list.ack", "rules": list_global_security_rules()})


async def _handle_security_rules_delete(req: Request) -> None:
    raw = req.env.payload.get("rules", [])
    rules = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    updated = delete_global_security_rules(rules)
    await req.reply({"type": "security.rules.delete.ack", "rules": updated})


# ------------------------------------------------------------------
# Session-scoped security rules (control connection, arbitrary session_id) —
# Kōdo Settings panel's "Sessions" → "Session Settings" modal
# (doc/SECURITY_RULES_PLAN.md §Phase 3 item 2's session-scope follow-up).
# ------------------------------------------------------------------


async def _handle_session_security_rules_list(req: Request) -> None:
    session_id = req.session_id
    rules = req.manager.list_session_security_rules(session_id) if session_id else []
    await req.reply({"type": "session.security_rules.list.ack", "rules": rules})


async def _handle_session_security_rules_delete(req: Request) -> None:
    session_id = req.session_id
    raw = req.env.payload.get("rules", [])
    rules = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    updated = req.manager.delete_session_security_rules(session_id, rules) if session_id else []
    await req.reply({"type": "session.security_rules.delete.ack", "rules": updated})


# ------------------------------------------------------------------
# Stuck-agent watchdog settings (machine-wide, control connection) — Kōdo
# Settings panel's "General" section (doc/STUCK_DETECTION.md §2.2, doc/
# SETTINGS.md §2.6). settings.json's stuck_detection block is read fresh per
# stall check, so unlike model selection this never needs a config.reload
# notification for a live session to pick up a change.
# ------------------------------------------------------------------

_STUCK_DETECTION_ACTIVE_VALUES = ("off", "local_only", "local_and_cloud")
_STUCK_DETECTION_SCOPE_VALUES = ("top_level", "top_level_and_subagents")


def _stuck_detection_payload(raw: dict[str, object]) -> dict[str, object]:
    """Defensively parse/clamp a ``stuck_detection`` block to its three fields.

    Mirrors ``kodo.runtime._engine._watchdog``'s own defensive parsing (kept
    as a separate copy rather than an import — ``_watchdog`` is a private
    module of a different package) so an unrecognised/missing value here
    falls back to the same documented default the watchdog itself would use,
    both when reading the file back and when validating a client's `.set`.
    """
    active = raw.get("active")
    scope = raw.get("scope")
    return {
        "active": active if active in _STUCK_DETECTION_ACTIVE_VALUES else "local_only",
        "scope": scope if scope in _STUCK_DETECTION_SCOPE_VALUES else "top_level",
        "auto_unstuck_interactive": bool(raw.get("auto_unstuck_interactive", False)),
    }


def _persist_stuck_detection(block: dict[str, object]) -> None:
    """Write the ``stuck_detection`` block into settings.json.

    Patches the raw user file (not the merged defaults view), so unrelated
    keys the user never set stay absent — same read-modify-write shape as
    ``_persist_local_model_selection``.
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
    data["stuck_detection"] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_stuck_detection_get_handler(config: Config) -> HandlerFn:
    async def _handle_stuck_detection_get(req: Request) -> None:
        settings = config.reload_settings()
        raw = settings.get("stuck_detection")
        raw = raw if isinstance(raw, dict) else {}
        await req.reply({"type": "stuck_detection.get.ack", **_stuck_detection_payload(raw)})

    return _handle_stuck_detection_get


async def _handle_stuck_detection_set(req: Request) -> None:
    block = _stuck_detection_payload(req.env.payload)
    _persist_stuck_detection(block)
    await req.reply({"type": "stuck_detection.set.ack", **block})


# ------------------------------------------------------------------
# housekeeper_llm.get / housekeeper_llm.set (doc/WS_PROTOCOL.md §7.6f,
# doc/SETTINGS.md §2.7) — which small local model backs session
# titling/greeting (kodo.titling). Same "General" section as stuck_detection
# above, same raw-file read-modify-write persistence shape.
# ------------------------------------------------------------------


def _housekeeper_llm_options_payload() -> list[dict[str, object]]:
    """``HOUSEKEEPER_LLM_OPTIONS`` shaped for the wire, in catalog order.

    One radio button per entry — adding a new entry to the dict is the only
    change needed for a new radio button to appear in the Kōdo Settings panel.
    """
    return [
        {"id": option.model_id, "name": option.display_name, "description": option.description}
        for option in HOUSEKEEPER_LLM_OPTIONS.values()
    ]


def _valid_housekeeper_llm_id(raw: object) -> str:
    """Coerce a settings.json value to a known catalog id, or the default."""
    if isinstance(raw, str) and raw in HOUSEKEEPER_LLM_OPTIONS:
        return raw
    return DEFAULT_HOUSEKEEPER_LLM_ID


def _current_housekeeper_llm_id() -> str:
    """The persisted ``housekeeper_llm`` selection from settings.json.

    Falls back to :data:`DEFAULT_HOUSEKEEPER_LLM_ID` if unset, unreadable, or
    naming an id no longer in the catalog — same defensive shape as
    ``_current_local_model_name``. Read directly off the raw file (not
    ``config.reload_settings()``) so the ``start_titling`` call sites below
    (startup, llamacpp install/update) don't need a ``Config`` in scope.
    """
    path = WorkspaceLayout().settings_json
    if not path.exists():
        return DEFAULT_HOUSEKEEPER_LLM_ID
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_HOUSEKEEPER_LLM_ID
    selected = data.get("housekeeper_llm") if isinstance(data, dict) else None
    return _valid_housekeeper_llm_id(selected)


def _persist_housekeeper_llm(option_id: str) -> None:
    """Write the ``housekeeper_llm`` key into settings.json.

    Patches the raw user file (not the merged defaults view), so unrelated
    keys the user never set stay absent — same read-modify-write shape as
    ``_persist_stuck_detection``.
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
    data["housekeeper_llm"] = option_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_housekeeper_llm_get_handler(config: Config) -> HandlerFn:
    async def _handle_housekeeper_llm_get(req: Request) -> None:
        settings = config.reload_settings()
        selected = _valid_housekeeper_llm_id(settings.get("housekeeper_llm"))
        await req.reply(
            {
                "type": "housekeeper_llm.get.ack",
                "selected": selected,
                "options": _housekeeper_llm_options_payload(),
            }
        )

    return _handle_housekeeper_llm_get


async def _handle_housekeeper_llm_set(req: Request) -> None:
    option_id = str(req.env.payload.get("id", "")).strip()
    if option_id not in HOUSEKEEPER_LLM_OPTIONS:
        await req.reply(
            {
                "type": "housekeeper_llm.set.ack",
                "ok": False,
                "error": f"Unknown housekeeper LLM: {option_id!r}",
            }
        )
        return
    _persist_housekeeper_llm(option_id)
    # Fire-and-forget, same as the llamacpp-install/startup call sites below
    # — a first pick of a not-yet-downloaded model can take a while, and
    # titling is a "nice to have" that must never make the settings panel
    # wait on it. start_titling stops whatever's currently running (if a
    # different model) before starting the newly selected one.
    asyncio.create_task(start_titling(kodo_user_dir(), option_id))
    await req.reply({"type": "housekeeper_llm.set.ack", "ok": True, "selected": option_id})


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


async def _handle_sampling_set(req: Request) -> None:
    """``sampling.set {model, sampling}`` (WS_PROTOCOL.md §7.x, doc/SAMPLING.md).

    The reply echoes the set actually stored, which may be a strict subset
    of what was sent: unknown/reserved/wrong-typed parameters are dropped
    and out-of-range numbers clamped rather than failing the whole request,
    so a client built against a different llama.cpp still gets the
    parameters both sides understand. ``ok: false`` means only that
    *model* is blank or not a known local entry.
    """
    session = await _require_session(req)
    if session is None:
        return
    payload = req.env.payload
    raw = payload.get("sampling")
    ok, stored = await session.engine.handle_sampling_set(
        str(payload.get("model", "")).strip(),
        raw if isinstance(raw, dict) else {},
    )
    await req.reply({"type": "sampling.accepted", "ok": ok, "sampling": stored})


async def _handle_workspace_folders(req: Request) -> None:
    session = await _require_session(req)
    if session is None:
        return
    physical_root = str(req.env.payload.get("physical_root", ""))
    raw = req.env.payload.get("folders", {})
    folders = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    raw_code_file = req.env.payload.get("code_workspace_file")
    code_workspace_file = (
        raw_code_file if isinstance(raw_code_file, str) and raw_code_file else None
    )
    await session.engine.handle_workspace_folders(physical_root, folders, code_workspace_file)
    await req.reply({"type": "workspace.folders.ack"})


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


async def _stream_llamacpp_progress(
    req: Request, work: Callable[[Callable[[int, str], None]], object]
) -> bool:
    """Run *work* (``install_llamacpp``/``update_llamacpp``) off-thread, streaming progress.

    Shared by :func:`_handle_llamacpp_install` and :func:`_handle_llamacpp_update`
    — both stream the same ``EVT_LLAMACPP_INSTALL_PROGRESS`` shape on the
    requesting connection until *work* finishes.

    Returns:
        bool: ``True`` if *work* completed without raising.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    ok = True

    def progress_cb(pct: int, msg: str) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (pct, msg))

    async def run() -> None:
        nonlocal ok
        try:
            await asyncio.to_thread(work, progress_cb)
        except Exception:  # noqa: BLE001
            ok = False
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
    return ok


async def _handle_llamacpp_install(req: Request) -> None:
    ok = await _stream_llamacpp_progress(
        req, lambda progress_cb: install_llamacpp(kodo_user_dir(), progress_cb=progress_cb)
    )
    if ok:
        # "installation procedure calls start titling" (doc/INTERNALS.md
        # §10c) — fire-and-forget so a first-run model download/subprocess
        # spin-up never delays this response. Resolves the user's persisted
        # housekeeper-LLM pick rather than always the compiled-in default, so
        # a previously-selected model survives a llama.cpp reinstall.
        asyncio.create_task(start_titling(kodo_user_dir(), _current_housekeeper_llm_id()))


def _parse_build_number(raw: object) -> int | None:
    """Parse a build-number field like ``"b12345"``/``"12345"``/``12345``.

    Returns ``None`` if *raw* doesn't match — callers distinguish "field
    absent" (also ``None``-producing) from "field present but malformed" by
    checking the raw value themselves.
    """
    match = re.match(r"^b?(\d+)$", str(raw).strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


async def _handle_llamacpp_update(req: Request) -> None:
    # Optional pinned version (Kōdo Settings panel's "Install specific
    # version" action, WS_PROTOCOL.md §7.6) — a malformed value fails fast
    # via the same progress-stream error shape a failed download would use,
    # rather than falling back to "latest" silently.
    raw_version = req.env.payload.get("version")
    version = _parse_build_number(raw_version) if raw_version is not None else None
    if raw_version is not None and version is None:
        msg = f'Invalid llama.cpp version {raw_version!r} — expected e.g. "b12345" or "12345".'
        await req.connection.send(
            Envelope.make_event(EVT_LLAMACPP_INSTALL_PROGRESS, {"percent": -1, "message": msg})
        )
        return

    kodo_dir = kodo_user_dir()
    installed = find_installed(kodo_dir)

    if version is None:
        # "Update llama.cpp" (to latest) — resolve and check *before* doing
        # anything else, so an already-current install neither triggers a
        # needless uninstall/reinstall cycle nor stops the titler.
        try:
            version = await asyncio.to_thread(fetch_latest_build_number)
        except Exception as exc:  # noqa: BLE001 — network/parse failure, reported not raised
            msg = f"Could not check the latest llama.cpp version: {exc}"
            await req.connection.send(
                Envelope.make_event(EVT_LLAMACPP_INSTALL_PROGRESS, {"percent": -1, "message": msg})
            )
            return
        if installed is not None and installed.build >= version:
            await req.connection.send(
                Envelope.make_event(
                    EVT_LLAMACPP_INSTALL_PROGRESS,
                    {
                        "percent": 100,
                        "message": f"llama.cpp b{installed.build} is already up to date.",
                        "up_to_date": True,
                    },
                )
            )
            return
    else:
        # Pinned version ("Install specific version…") — confirm the release
        # actually exists on GitHub *before* uninstalling the current build,
        # so a typo'd/nonexistent build number leaves the existing
        # installation intact and never touches the titler.
        exists = await asyncio.to_thread(build_exists, version)
        if not exists:
            msg = f"llama.cpp b{version} was not found on GitHub Releases."
            await req.connection.send(
                Envelope.make_event(EVT_LLAMACPP_INSTALL_PROGRESS, {"percent": -1, "message": msg})
            )
            return

    # Stop the titler's llama-server first — it runs off the same llama.cpp
    # install that update_llamacpp is about to replace (uninstall + fresh
    # install) — then let a successful update restart it, same as a fresh
    # install (doc/INTERNALS.md §10c).
    await stop_titling()

    def _update(progress_cb: Callable[[int, str], None]) -> LlamaInstall:
        return update_llamacpp(kodo_dir, version=version, progress_cb=progress_cb)

    ok = await _stream_llamacpp_progress(req, _update)
    if ok:
        asyncio.create_task(start_titling(kodo_dir, _current_housekeeper_llm_id()))


async def _handle_llamacpp_uninstall(req: Request) -> None:
    # Stop anything running off the install we're about to delete — the
    # titler's own llama-server (kodo.titling) and kodo's main chat
    # llama-server both run off the same binary files.
    await stop_titling()
    server = LlamaServer.get_active_llama_server()
    if server is not None:
        await server.stop()
    uninstall_llamacpp(kodo_user_dir())
    await req.connection.send(
        Envelope.make_event(EVT_LLAMA_STATE, {"running": False, "model": None})
    )
    await req.reply(
        {"type": "llamacpp.uninstall.ack", "llama_installed": False, "llama_version": None}
    )


async def _handle_llamacpp_version_info(req: Request) -> None:
    installed = find_installed(kodo_user_dir())
    installed_version = f"b{installed.build}" if installed is not None else None
    try:
        latest_build = await asyncio.to_thread(fetch_latest_build_number)
        latest_version: str | None = f"b{latest_build}"
        error: str | None = None
    except Exception as exc:  # noqa: BLE001 — network/parse failure, reported not raised
        latest_version = None
        error = str(exc)
    await req.reply(
        {
            "type": "llamacpp.version_info.ack",
            "installed_version": installed_version,
            "latest_version": latest_version,
            "error": error,
        }
    )


async def _request_hf_token(req: Request) -> str:
    """Request the active HF access token from the connected extension.

    Returns the token string, or empty string if no token is configured or
    the connection is session-bound (responses can't be routed back on
    session connections). The token is optional — downloads proceed without
    it for public repos.
    """
    connection = req.connection
    # On a session-bound connection the response can't be routed back
    # (ConnectionRegistry dispatches responses to the session channel,
    # not the connection). Bail out immediately — no token from this path.
    # We check both the explicit session from the request payload AND whether
    # the connection itself is bound to a session (the test harness sends
    # control messages on a session connection without a session_id in the
    # payload, so req.session alone is insufficient).
    if req.session is not None:
        return ""
    bound_session = req.manager.session_for_connection(connection.id)
    if bound_session is not None:
        return ""

    req_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    connection.register_response_future(req_id, future)

    try:
        await connection.send(
            Envelope(
                kind="request",
                id=req_id,
                payload={"type": SREQ_HF_TOKEN_REQUEST},
            )
        )

        # Wait with a timeout — extension should respond quickly
        try:
            payload = await asyncio.wait_for(future, timeout=10.0)
        except TimeoutError:
            future.cancel()
            return ""  # No token available — proceed without auth
        except asyncio.CancelledError:
            # Connection dropped mid-request (ConnectionRegistry.run_ws's
            # finally cancels every pending future on disconnect) — proceed
            # without a token rather than let this propagate out of the
            # fire-and-forget background download task.
            return ""

        error = payload.get("error")
        if error:
            return ""  # User cancelled or no token

        return str(payload.get("hf_token", ""))
    finally:
        # Clean up the future even if something went wrong (no-op if already
        # resolved — resolve_response never raises).
        connection.resolve_response(req_id, {})


def _run_background_download(
    model_id: str, work: Callable[[], Coroutine[object, object, object]], connection: Connection
) -> None:
    """Fire-and-forget an async download/resume call as a background task.

    ``work`` is native ``asyncio`` (:mod:`kodo.llms.local` runs its transfers
    as several concurrent ``aiohttp`` requests, not a blocking call), so this
    just schedules it as a task — no worker thread involved, and every other
    connection's requests are still serviced on the same event loop while it
    runs.

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
            await work()
        except LocalModelError as exc:
            _log.exception("Background download failed for %r", model_id)
            # A gated-repo rejection is a wrapped GatedRepoError (see
            # kodo.llms.local._hf.resolve_file/list_repo_files, both of which
            # `raise ShardResolutionError(...) from exc` to preserve it as
            # __cause__) — checked by type, not by sniffing the message text,
            # since substring-matching "gated" would also fire on unrelated
            # failures whose message happens to contain e.g. "aggregated".
            if isinstance(exc.__cause__, GatedRepoError):
                await connection.send(
                    Envelope.make_event(
                        EVT_HF_TOKEN_REVOKE,
                        {"message": "HF token was rejected for a gated repository"},
                    )
                )
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

    async def _download() -> None:
        # The HF token is requested from *inside* the background task, not
        # before it: ConnectionRegistry.run_ws reads one frame at a time and
        # awaits each handler in turn, so a synchronous await here (i.e.
        # before _run_background_download hands this off as an independent
        # task) would deadlock — the extension's response is itself the next
        # frame on this same connection, which the read loop can't reach
        # until this handler returns. Same reasoning applies to every other
        # _download() below.
        hf_token = await _request_hf_token(req)
        await manager.download_model(
            entry.name, entry.repo_id, entry.filename, token=hf_token or None
        )

    _run_background_download(name, _download, req.connection)


async def _handle_local_llm_resume(req: Request) -> None:
    name = str(req.env.payload.get("name", "")).strip()
    if not name:
        return
    manager = get_local_model_manager(kodo_user_dir())
    if manager.get_record(name) is None:
        await _reply_local_llm_error(req, f"No download record for {name!r} — nothing to resume")
        return

    await _send_registry_state(req)  # see the ordering note in _handle_local_llm_install

    async def _download() -> None:
        # See _handle_local_llm_install for why the token is requested here.
        hf_token = await _request_hf_token(req)
        await manager.resume_download(name, token=hf_token or None)

    _run_background_download(name, _download, req.connection)


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


async def _handle_local_llm_update(req: Request) -> None:
    """Re-fetch an installed model whose remote GGUF has changed.

    Deliberately composed from the exact same two calls
    ``local_llm.uninstall``/``local_llm.install`` already make —
    ``LocalModelManager.uninstall`` then ``.download_model`` via
    ``_run_background_download`` — rather than a new "atomic update" code
    path, so an update goes through the same manager-state transitions
    (uninstalled -> kickoff -> downloading -> installed/failed) a user
    manually clicking Uninstall then Install would produce. See
    doc/LOCAL_MODEL_MANAGER.md §12.
    """
    name = str(req.env.payload.get("name", "")).strip()
    if not name:
        return
    kodo_dir = kodo_user_dir()
    entry = get_local_registry(kodo_dir).get(name)
    if entry is None or entry.kind not in ("hardcoded_hf", "custom_hf"):
        await _reply_local_llm_error(req, f"Unknown or non-downloadable model: {name!r}")
        return
    manager = get_local_model_manager(kodo_dir)
    await asyncio.to_thread(manager.uninstall, name)
    _log.info("Uninstalled model %r for update", name)

    # Reflects the now-uninstalled entry — the same state a plain
    # local_llm.uninstall would send, and (since the model is already not
    # installed at this point) also the correct "kickoff" state for the
    # install half below. Must be sent before the background task is created,
    # same ordering requirement as _handle_local_llm_install.
    await _send_registry_state(req)

    async def _download() -> None:
        # See _handle_local_llm_install for why the token is requested here.
        hf_token = await _request_hf_token(req)
        await manager.download_model(
            entry.name, entry.repo_id, entry.filename, token=hf_token or None
        )

    _run_background_download(name, _download, req.connection)


async def _handle_local_llm_check_updates(req: Request) -> None:
    """Fire-and-forget background ETag scan — see MSG_LOCAL_LLM_CHECK_UPDATES.

    No synchronous reply: the client doesn't wait for one, and the scan
    itself (one metadata round trip per installed file, across however many
    names were sent) can take a while. ``EVT_LOCAL_LLM_UPDATES_AVAILABLE`` is
    pushed on this connection once every name has been checked.
    """
    raw_names = req.env.payload.get("names", [])
    names = (
        [str(n).strip() for n in raw_names if str(n).strip()] if isinstance(raw_names, list) else []
    )
    if not names:
        return
    kodo_dir = kodo_user_dir()
    registry = get_local_registry(kodo_dir)
    manager = get_local_model_manager(kodo_dir)
    connection = req.connection

    async def run() -> None:
        updatable: list[str] = []
        for name in names:
            entry = registry.get(name)
            if entry is None or entry.kind not in ("hardcoded_hf", "custom_hf"):
                continue  # not an HF-backed entry — no remote to compare against
            if await manager.check_for_update(name):
                updatable.append(name)
        await connection.send(
            Envelope.make_event(EVT_LOCAL_LLM_UPDATES_AVAILABLE, {"updatable": updatable})
        )

    asyncio.create_task(run())


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


def _parse_non_negative_int(raw: object) -> int:
    """Best-effort int parse for a numeric webview field — used for
    ``context_window`` (add_huggingface/add_file). Anything unparseable,
    missing, or falsy collapses to ``0`` ("unset"/"unknown")."""
    try:
        return int(cast(int, raw) or 0)
    except (TypeError, ValueError):
        return 0


def _entry_base_args(payload: dict[str, object]) -> dict[str, str]:
    """The ``base_llama_args`` for an entry being added from an "Add local LLM" modal.

    The launch args typed into that form are the one launch-arg input a
    user-added entry contributes. They become its ``base_llama_args`` — the
    floor its Default profile's knobs layer on top of — rather than a separate
    profile, so the entry gets the same knob-driven Configure modal every
    built-in LLM has. :func:`~kodo.llms.local_registry._entries._with_custom_entry_knobs`
    merges the shared base args (``--jinja`` and friends) under these on load,
    so an empty form still produces a working launch.
    """
    return parse_llama_args(payload.get("llama_args", {}))


async def _handle_local_llm_add_huggingface(req: Request) -> None:
    payload = req.env.payload
    entry = LocalLLMEntry(
        name=str(payload.get("name", "")).strip(),
        kind="custom_hf",
        description=str(payload.get("description", "")),
        repo_id=str(payload.get("repo_id", "")).strip(),
        filename=str(payload.get("filename", "")).strip(),
        context_window=_parse_non_negative_int(payload.get("context_window", 0)),
        base_llama_args=_entry_base_args(payload),
    )
    if not entry.name or not entry.repo_id or not entry.filename:
        await _reply_local_llm_error(req, "name, repo_id, and filename are all required")
        return
    kodo_dir = kodo_user_dir()
    try:
        add_local_entry(kodo_dir, entry)
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
        context_window=_parse_non_negative_int(payload.get("context_window", 0)),
        base_llama_args=_entry_base_args(payload),
    )
    if not entry.name or not entry.path:
        await _reply_local_llm_error(req, "name and path are both required")
        return
    kodo_dir = kodo_user_dir()
    try:
        add_local_entry(kodo_dir, entry)
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
        # Not a process kodo launches: no base args, no knobs, no profiles.
        base_llama_args={},
        knobs=(),
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


def _current_local_model_name() -> str:
    """The ``models.local`` entry name from settings.json, or ``""`` if unset/unreadable."""
    path = WorkspaceLayout().settings_json
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    models = data.get("models") if isinstance(data, dict) else None
    return str(models.get("local", "")) if isinstance(models, dict) else ""


async def _restart_llama_server_if_running(req: Request, entry_name: str) -> None:
    """Force a fresh llama-server launch for *entry_name* to pick up a config change.

    Only meaningful when llama-server is *actually currently running*
    *entry_name* — a profile switch or knob change for a
    selected-but-not-started model has nothing to restart. Stops it
    unconditionally first even though
    :func:`kodo.llms.llamacpp.ensure_llama_running` would normally treat a
    same-name request as "already running, nothing to do" — neither a profile
    switch nor a knob change alters ``entry.name``, so that shortcut would
    otherwise mask the new args entirely (see its docstring).
    """
    kodo_dir = kodo_user_dir()
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None or entry.kind == "custom_server_url":
        return
    server = LlamaServer.get_active_llama_server()
    if server is None or not server.is_running or server.model_name != entry_name:
        return
    await server.stop()
    try:
        server = await ensure_llama_running(entry, kodo_dir)
    except Exception as exc:  # noqa: BLE001 — startup failure surfaces as llama.state, not a crash
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


async def _handle_local_llm_add_profile(req: Request) -> None:
    payload = req.env.payload
    entry_name = str(payload.get("name", "")).strip()
    try:
        profile = add_profile(
            kodo_user_dir(),
            entry_name,
            str(payload.get("profile_name", "")).strip(),
            description=str(payload.get("description", "")),
            llama_args=parse_llama_args_text(payload.get("llama_args_text", "")),
        )
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    _log.info("Added profile %r (%r) to %r", profile.name, profile.id, entry_name)
    await _send_registry_state(req)


async def _handle_local_llm_update_profile(req: Request) -> None:
    payload = req.env.payload
    entry_name = str(payload.get("name", "")).strip()
    profile_id = str(payload.get("profile_id", "")).strip()
    kodo_dir = kodo_user_dir()
    # Only a restart-worthy change if this profile is the one currently
    # selected — editing an inactive profile changes nothing that is running.
    was_active = get_active_profile(kodo_dir, entry_name) == profile_id
    try:
        profile = update_profile(
            kodo_dir,
            entry_name,
            profile_id,
            str(payload.get("profile_name", "")).strip(),
            description=str(payload.get("description", "")),
            llama_args=parse_llama_args_text(payload.get("llama_args_text", "")),
        )
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    _log.info("Updated profile %r (%r) on %r", profile.name, profile.id, entry_name)
    if was_active and entry_name == _current_local_model_name():
        await _restart_llama_server_if_running(req, entry_name)
    await _send_registry_state(req)


async def _handle_local_llm_remove_profile(req: Request) -> None:
    entry_name = str(req.env.payload.get("name", "")).strip()
    profile_id = str(req.env.payload.get("profile_id", "")).strip()
    kodo_dir = kodo_user_dir()
    was_active = get_active_profile(kodo_dir, entry_name) == profile_id
    try:
        remove_profile(kodo_dir, entry_name, profile_id)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    if was_active and entry_name == _current_local_model_name():
        await _restart_llama_server_if_running(req, entry_name)
    await _send_registry_state(req)


async def _handle_local_llm_set_active_profile(req: Request) -> None:
    entry_name = str(req.env.payload.get("name", "")).strip()
    profile_id = str(req.env.payload.get("profile_id", "")).strip()
    kodo_dir = kodo_user_dir()
    changed = get_active_profile(kodo_dir, entry_name) != profile_id
    try:
        set_active_profile(kodo_dir, entry_name, profile_id)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    if changed and entry_name == _current_local_model_name():
        await _restart_llama_server_if_running(req, entry_name)
    await _send_registry_state(req)


async def _handle_local_llm_set_knobs(req: Request) -> None:
    """Apply the Configure modal's whole knob selection for one entry.

    Restarts llama-server only when the knobs actually resolve to different
    launch args *and* this entry is both the selected local model and the one
    currently running — re-applying an unchanged selection (the user opened
    Configure, changed nothing, hit Apply) must not interrupt a window that is
    mid-generation. Compared on the resolved args rather than on the selection
    map because the two are not equivalent: a selection can change from an
    absent key to an explicit one that means the same thing.
    """
    entry_name = str(req.env.payload.get("name", "")).strip()
    raw = req.env.payload.get("knobs")
    selections = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    kodo_dir = kodo_user_dir()
    entry = get_local_registry(kodo_dir).get(entry_name)
    before = resolve_default_profile_args(kodo_dir, entry) if entry is not None else {}
    try:
        set_knobs(kodo_dir, entry_name, selections)
    except ValueError as exc:
        await _reply_local_llm_error(req, str(exc))
        return
    after = resolve_default_profile_args(kodo_dir, entry) if entry is not None else {}
    _log.info("Set knobs on %r: %s", entry_name, selections)
    # A user-defined profile being active means the knobs aren't what launches.
    on_default = get_active_profile(kodo_dir, entry_name) == ""
    if on_default and before != after and entry_name == _current_local_model_name():
        await _restart_llama_server_if_running(req, entry_name)
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


def _make_server_shutdown_handler(conn_registry: ConnectionRegistry) -> HandlerFn:
    """Build the ``server.shutdown`` handler (doc/WS_PROTOCOL.md §7.6g).

    Acks first, then hands off to
    :meth:`ConnectionRegistry.request_shutdown`, which takes the ordinary
    graceful-stop path — so the llama-server teardown the caller cares about
    is :func:`_stop_background` (``on_shutdown``), not code duplicated here.
    Keeping it there means one teardown path for SIGTERM, the idle self-reap
    and this command alike; if you ever stop tearing llama-servers down in
    ``_stop_background``, this command stops covering them too.
    """

    async def _handle_server_shutdown(req: Request) -> None:
        reason = str(req.env.payload.get("reason") or "client request")
        _log.info("Client requested server shutdown: %s", reason)
        await req.reply({"type": "server.shutdown.ack", "ok": True, "pid": os.getpid()})
        conn_registry.request_shutdown(reason)

    return _handle_server_shutdown


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

    # Fire-and-forget (doc/INTERNALS.md §10c), and deliberately kicked off
    # *before* the ensure_all_utils await below rather than after: the titler
    # backs the session-opening greeting (kodo.titling.generate_greeting,
    # runtime._engine._greeting.SessionGreeter) which fires the moment the
    # very first hello creates a session, so the earlier this starts loading,
    # the more likely a first-run model download or subprocess health-check
    # has finished by then. Still never delays kodo itself from accepting
    # connections — titling (and the greeter riding it) is simply unavailable
    # until this finishes, same as before.
    llama_install = find_installed(user_dir)
    if llama_install is not None:
        asyncio.create_task(start_titling(user_dir, _current_housekeeper_llm_id()))

    # Ensure the bundled third-party utils (uv, ripgrep, fd) are present under
    # ~/.kodo/bin. Best-effort and idempotent: a no-op once already present,
    # so this only does real work on a first console-style launch. Off the
    # event loop (asyncio.to_thread) so a first-run download does not delay
    # server readiness.
    await asyncio.to_thread(ensure_all_utils, user_dir)

    running = find_running_server(user_dir)
    if running is not None:
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
    await stop_titling()
    await app[_MANAGER_KEY].shutdown()


async def _release_llama_gpu() -> None:
    server = LlamaServer.get_active_llama_server()
    if server is not None and server.is_running:
        await server.stop()


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
    conn_registry.set_gpu_release_hook(_release_llama_gpu)

    conn_registry.register_handler(MSG_HELLO, _make_hello_handler(config))
    conn_registry.register_handler(MSG_SESSION_LIST, _handle_session_list)
    conn_registry.register_handler(MSG_SESSION_RELEASE, _handle_session_release)
    conn_registry.register_handler(MSG_SESSION_DELETE, _handle_session_delete)
    conn_registry.register_handler(MSG_SESSION_DELETE_BY_ID, _handle_session_delete_by_id)
    conn_registry.register_handler(MSG_SECURITY_RULES_LIST, _handle_security_rules_list)
    conn_registry.register_handler(MSG_SECURITY_RULES_DELETE, _handle_security_rules_delete)
    conn_registry.register_handler(
        MSG_SESSION_SECURITY_RULES_LIST, _handle_session_security_rules_list
    )
    conn_registry.register_handler(
        MSG_SESSION_SECURITY_RULES_DELETE, _handle_session_security_rules_delete
    )
    conn_registry.register_handler(
        MSG_STUCK_DETECTION_GET, _make_stuck_detection_get_handler(config)
    )
    conn_registry.register_handler(MSG_STUCK_DETECTION_SET, _handle_stuck_detection_set)
    conn_registry.register_handler(
        MSG_HOUSEKEEPER_LLM_GET, _make_housekeeper_llm_get_handler(config)
    )
    conn_registry.register_handler(MSG_HOUSEKEEPER_LLM_SET, _handle_housekeeper_llm_set)
    conn_registry.register_handler(MSG_PROMPT_SUBMIT, _handle_prompt)
    conn_registry.register_handler(MSG_MODE_SET, _handle_mode)
    conn_registry.register_handler(MSG_WORKFLOW_SET, _handle_workflow)
    conn_registry.register_handler(MSG_EDIT_CONTROL_SET, _handle_edit_control)
    conn_registry.register_handler(MSG_COMMAND_CONTROL_SET, _handle_command_control)
    conn_registry.register_handler(MSG_THINKING_LEVEL_SET, _handle_thinking_level)
    conn_registry.register_handler(MSG_SAMPLING_SET, _handle_sampling_set)
    conn_registry.register_handler(MSG_WORKSPACE_FOLDERS, _handle_workspace_folders)
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
    conn_registry.register_handler(MSG_LLAMACPP_UPDATE, _handle_llamacpp_update)
    conn_registry.register_handler(MSG_LLAMACPP_UNINSTALL, _handle_llamacpp_uninstall)
    conn_registry.register_handler(MSG_LLAMACPP_VERSION_INFO, _handle_llamacpp_version_info)
    conn_registry.register_handler(MSG_LOCAL_LLM_INSTALL, _handle_local_llm_install)
    conn_registry.register_handler(MSG_LOCAL_LLM_RESUME, _handle_local_llm_resume)
    conn_registry.register_handler(MSG_LOCAL_LLM_PAUSE, _handle_local_llm_pause)
    conn_registry.register_handler(MSG_LOCAL_LLM_UPDATE, _handle_local_llm_update)
    conn_registry.register_handler(MSG_LOCAL_LLM_CHECK_UPDATES, _handle_local_llm_check_updates)
    conn_registry.register_handler(MSG_LOCAL_LLM_UNINSTALL, _handle_local_llm_uninstall)
    conn_registry.register_handler(MSG_LOCAL_LLM_REMOVE, _handle_local_llm_remove)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_HUGGINGFACE, _handle_local_llm_add_huggingface)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_FILE, _handle_local_llm_add_file)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_SERVER_URL, _handle_local_llm_add_server_url)
    conn_registry.register_handler(MSG_LOCAL_LLM_ADD_PROFILE, _handle_local_llm_add_profile)
    conn_registry.register_handler(MSG_LOCAL_LLM_UPDATE_PROFILE, _handle_local_llm_update_profile)
    conn_registry.register_handler(MSG_LOCAL_LLM_REMOVE_PROFILE, _handle_local_llm_remove_profile)
    conn_registry.register_handler(
        MSG_LOCAL_LLM_SET_ACTIVE_PROFILE, _handle_local_llm_set_active_profile
    )
    conn_registry.register_handler(MSG_LOCAL_LLM_SET_KNOBS, _handle_local_llm_set_knobs)
    conn_registry.register_handler(MSG_LLAMA_SERVER_OVERRIDE_SET, _handle_llama_server_override_set)
    conn_registry.register_handler(
        MSG_LLAMA_SERVER_OVERRIDE_REMOVE, _handle_llama_server_override_remove
    )
    conn_registry.register_handler(MSG_LLAMA_START, _make_llama_start_handler(config))
    conn_registry.register_handler(MSG_LLAMA_STOP, _handle_llama_stop)
    conn_registry.register_handler(
        MSG_SERVER_SHUTDOWN, _make_server_shutdown_handler(conn_registry)
    )
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
