"""On-demand llama-server lifecycle manager, plus the shared LocalModelManager accessor."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from kodo.llms import (
    REASONING_BUDGET_MESSAGE,
    LocalLLMEntry,
    get_active_profile,
    get_llama_server_override_path,
    local_thinking_family,
    prune_unknown_model_state,
    resolve_effective_llama_config,
)
from kodo.llms.local import LocalModelManager

from ._installer import find_installed
from ._llama_server import LlamaServer, LlamaServerConfig

__all__ = [
    "LlamaLaunch",
    "ensure_llama_running",
    "find_installed_model_path",
    "get_local_model_manager",
    "purge_unknown_local_models",
    "resolve_llama_launch",
]

_log = logging.getLogger(__name__)

_manager_cache: dict[Path, LocalModelManager] = {}
_manager_cache_lock = threading.Lock()


def _models_dir(kodo_dir: Path) -> Path:
    """Return the directory where GGUF model files are stored.

    Reads ``llm_models_dir`` from ``kodo_dir/etc/settings.json``; falls back
    to ``kodo_dir/llama.cpp/models``.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        Path: Directory for model files.
    """
    settings_file = kodo_dir / "etc" / "settings.json"
    if settings_file.is_file():
        try:
            parsed = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and "llm_models_dir" in parsed:
                return Path(str(parsed["llm_models_dir"]))
        except Exception:
            pass
    return kodo_dir / "llama.cpp" / "models"


def get_local_model_manager(kodo_dir: Path) -> LocalModelManager:
    """Return the process-wide :class:`LocalModelManager` for *kodo_dir*'s model directory.

    :class:`LocalModelManager` itself is a plain, freely-instantiable class
    (not a singleton) — this cache exists purely so that a
    :meth:`LocalModelManager.pause_download` call (a future WS handler) can
    reach the same in-memory cancellation event as the
    :meth:`LocalModelManager.download_model` call it's meant to interrupt,
    which requires reusing the same instance across separate WS request
    handlers within this one server process.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        LocalModelManager: The shared manager instance for this directory.
    """
    root = _models_dir(kodo_dir)
    with _manager_cache_lock:
        manager = _manager_cache.get(root)
        if manager is None:
            manager = LocalModelManager(root)
            _manager_cache[root] = manager
        return manager


def purge_unknown_local_models(kodo_dir: Path, *, keep: Iterable[str] = ()) -> tuple[str, ...]:
    """Delete every downloaded model, and every stored setting, kodo no longer knows.

    The glue between the two halves of the problem a renamed or retired
    catalog entry leaves behind: its stored knob selections and profiles (the
    registry's side, :func:`kodo.llms.prune_unknown_model_state`) and its
    downloaded GGUF plus ``manager-state.json`` record (this manager's side,
    which has no notion of a registry at all and would otherwise keep those
    files forever, unreachable from any UI — tens of GB per model).

    Meant to be called exactly once per server start, off the event loop: a
    purge deletes whole model directories, so it blocks for as long as the
    filesystem takes. Blocks nothing else — everything it deletes is by
    definition unreachable from the current catalog.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        keep (Iterable[str]): Model ids to leave alone even when unknown —
            the model of an already-running llama-server, whose file is open
            (deleting it mid-flight fails outright on Windows). A kept model
            is simply purged by the next start that finds it idle.

    Returns:
        tuple[str, ...]: The model ids purged, sorted. Empty when there was
        nothing stale to remove.
    """
    manager = get_local_model_manager(kodo_dir)
    installed = {record.model_id for record in manager.list_models()}
    spared = set(keep)
    unknown = [
        name for name in prune_unknown_model_state(kodo_dir, installed) if name not in spared
    ]
    for name in unknown:
        if name in installed:
            manager.uninstall(name)
    if unknown:
        _log.info("Purged local models kodo no longer knows: %s", ", ".join(unknown))
    return tuple(unknown)


async def ensure_llama_running(entry: LocalLLMEntry, kodo_dir: Path) -> LlamaServer:
    """Start llama-server for *entry* if not already running.

    If a server is already running with the same model, return it immediately
    — this does **not** re-check whether *entry*'s launch configuration
    changed since that server was launched, since neither a profile switch nor
    a knob change alters ``entry.name``. Callers that just changed the
    currently-running entry's configuration (the ``local_llm.set_active_profile``
    and ``local_llm.set_knobs`` handlers) must explicitly stop the server
    themselves before calling this, or the change silently
    won't take effect until some other reason forces a restart. If a server
    is running with a different model, stop it first then start fresh. Not
    valid for ``custom_server_url`` entries — those are not managed by kodo
    at all; callers must special-case that kind before reaching here (see
    :class:`kodo.llms.llamacpp.LlamaPlugin`).

    Resolves *entry*'s effective ``llama_args`` fresh on every call via
    :func:`kodo.llms.resolve_effective_llama_config` (the active user-defined
    profile's args, or the Default profile's base args plus its current knob
    selection — see doc/LLM_REGISTRY.md §4.6) — so a restart triggered for
    any other reason (a plain model switch, a crash recovery, etc.) always
    launches with whatever is currently selected, not a stale snapshot. The
    resolved numeric ``context_window`` is not used here — only
    :func:`kodo.llms.get_context_window` (compaction budgeting) reads it; the
    actual launched context size lives inside ``llama_args`` itself (a context
    knob's, or a profile's own, ``--ctx-size``).

    If a llama-server binary override is configured (see
    :func:`kodo.llms.set_llama_server_override_path`), it is used as the
    executable in place of the bundled llama.cpp build — the CLI-argument
    generation in :class:`LlamaServerConfig`/:class:`LlamaServer` is unchanged
    either way.

    Also passes *entry*'s active profile id (:func:`kodo.llms.get_active_profile`,
    ``""`` for the Default profile) to :class:`LlamaServer` purely for crash
    messaging: if the process exits before becoming ready while a user-defined
    profile is active, :class:`LlamaServer` suggests switching back to the
    Default profile, since hand-written launch args are the most likely cause.

    Args:
        entry (LocalLLMEntry): The local registry entry to serve — either a
            ``hardcoded_hf``/``custom_hf`` entry (resolved via the download
            index) or a ``custom_file`` entry (its own ``path``, not indexed).
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        LlamaServer: The running server instance.

    Raises:
        RuntimeError: If *entry* is a ``custom_server_url``, llama.cpp is not
            installed, the model is not downloaded/present, or the server
            fails to start.
    """
    if entry.kind == "custom_server_url":
        raise RuntimeError(
            "custom_server_url entries are not managed by kodo — connect to entry.url directly"
        )

    server = LlamaServer.get_active_llama_server()
    if server is not None and server.is_running:
        if server.model_name == entry.name:
            return server
        await server.stop()

    launch = resolve_llama_launch(entry, kodo_dir)

    if entry.kind == "custom_file":
        model_path: Path | None = Path(entry.path)
        if model_path is None or not model_path.is_file():
            raise RuntimeError(f"Model file not found: {entry.path!r}")
    else:
        model_path = get_local_model_manager(kodo_dir).get_model_path(entry.name)
        if model_path is None:
            raise RuntimeError(f"Model {entry.name!r} is not installed")

    cfg = LlamaServerConfig(
        executable=launch.executable,
        model_path=model_path,
        kodo_dir=kodo_dir,
        model_name=entry.name,
    )
    server = LlamaServer(cfg, launch.llama_args, profile_id=launch.profile_id)
    await server.start()
    return server


@dataclass(frozen=True)
class LlamaLaunch:
    """Everything needed to launch llama-server for one entry, except the GGUF path.

    Attributes:
        executable: The llama-server binary — the user's override when one
            is set (:func:`kodo.llms.get_llama_server_override_path`), else
            the installed llama.cpp build's.
        llama_args: The complete resolved launch flags for the entry's active
            profile, including any flag the engine forces regardless of it.
        profile_id: The active user-defined profile's id, ``""`` for Default.
    """

    executable: Path
    llama_args: dict[str, str]
    profile_id: str


def resolve_llama_launch(entry: LocalLLMEntry, kodo_dir: Path) -> LlamaLaunch:
    """Resolve how llama-server must be launched for *entry*.

    The single source of launch configuration for both the kodo server's own
    managed llama-server (:func:`ensure_llama_running`) and the standalone
    ``kodo-llama-server`` — so a model launched by either gets identical flags.
    The GGUF path is deliberately not resolved here: the two callers look it
    up differently (the standalone one must not construct a
    :class:`~kodo.llms.local.LocalModelManager`, see
    :func:`find_installed_model_path`).

    Args:
        entry (LocalLLMEntry): The local registry entry to launch.
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        LlamaLaunch: Executable, resolved flags and active profile id.

    Raises:
        RuntimeError: If llama.cpp is not installed.
    """
    install = find_installed(kodo_dir)
    if install is None:
        raise RuntimeError("llama.cpp is not installed")

    override = get_llama_server_override_path(kodo_dir)
    executable = Path(override) if override else install.executable

    # The active profile — user-defined, or the knob-driven Default one —
    # supplies the complete llama_args; see resolve_effective_llama_config.
    llama_args, _ = resolve_effective_llama_config(kodo_dir, entry)
    profile_id = get_active_profile(kodo_dir, entry.name)
    if local_thinking_family(entry.base_llm) == "qwen_reasoning_budget":
        # Forced (plain assignment), not defaulted: -1 is mandatory here, not
        # just the default — it's what makes the per-request
        # `thinking_budget_tokens` override in _llama.py take effect at all,
        # and any other explicit CLI value would lock it out. A profile must
        # never be able to override or lock this out — add_profile/
        # update_profile already strip RESERVED_LLAMA_ARGS before a profile is
        # even saved; this is the second, load-bearing line of defense for
        # anything saved before that existed, and for the Default profile,
        # whose knobs never write these flags in the first place.
        llama_args["--reasoning-budget"] = "-1"
        llama_args["--reasoning-budget-message"] = REASONING_BUDGET_MESSAGE
    return LlamaLaunch(executable=executable, llama_args=llama_args, profile_id=profile_id)


def find_installed_model_path(entry: LocalLLMEntry, kodo_dir: Path) -> Path | None:
    """The GGUF to launch for *entry*, looked up without writing any state.

    ``custom_file`` entries point at their own file; downloaded entries are
    read from the models directory's ``manager-state.json`` via
    :meth:`kodo.llms.local.LocalModelManager.peek_model_path`, which — unlike
    :func:`get_local_model_manager` — never rewrites an in-flight download's
    status, so it is safe to call from a process that does not own the
    downloads (``kodo-llama-server`` running next to a kodo server).

    Args:
        entry (LocalLLMEntry): The local registry entry.
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        Path | None: The model file, or ``None`` if it is not present.
    """
    if entry.kind == "custom_file":
        path = Path(entry.path)
        return path if path.is_file() else None
    return LocalModelManager.peek_model_path(_models_dir(kodo_dir), entry.name)
