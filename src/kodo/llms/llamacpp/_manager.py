"""On-demand llama-server lifecycle manager, plus the shared LocalModelManager accessor."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from kodo.llms import (
    REASONING_BUDGET_MESSAGE,
    LocalLLMEntry,
    get_effective_flavor_id,
    get_llama_server_override_path,
    local_thinking_family,
    resolve_effective_llama_config,
)
from kodo.llms.local import LocalModelManager

from ._installer import find_installed
from ._llama_server import LlamaServer, LlamaServerConfig

__all__ = ["ensure_llama_running", "get_local_model_manager"]

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


async def ensure_llama_running(entry: LocalLLMEntry, kodo_dir: Path) -> LlamaServer:
    """Start llama-server for *entry* if not already running.

    If a server is already running with the same model, return it immediately
    — this does **not** re-check whether *entry*'s active flavor changed since
    that server was launched, since flavors don't change ``entry.name``.
    Callers that just changed the currently-running entry's active flavor
    (``local_llm.set_active_flavor``'s handler) must explicitly stop the
    server themselves before calling this, or the flavor change silently
    won't take effect until some other reason forces a restart. If a server
    is running with a different model, stop it first then start fresh. Not
    valid for ``custom_server_url`` entries — those are not managed by kodo
    at all; callers must special-case that kind before reaching here (see
    :class:`kodo.llms.llamacpp.LlamaPlugin`).

    Resolves *entry*'s effective ``llama_args`` fresh on every call via
    :func:`kodo.llms.resolve_effective_llama_config` (applies the active
    flavor, or the entry's first/default one — flavors are the only source
    of launch args now, see :class:`kodo.llms.LlamaFlavor`) — so a restart
    triggered for any other reason (a plain model switch, a crash recovery,
    etc.) always launches with whatever flavor is currently selected, not a
    stale snapshot. The resolved numeric ``context_window`` is not used
    here — only :func:`kodo.llms.get_context_window` (compaction budgeting)
    reads it; the actual launched context size lives inside ``llama_args``
    itself (e.g. a flavor's own ``--ctx-size``).

    If a llama-server binary override is configured (see
    :func:`kodo.llms.set_llama_server_override_path`), it is used as the
    executable in place of the bundled llama.cpp build — the CLI-argument
    generation in :class:`LlamaServerConfig`/:class:`LlamaServer` is unchanged
    either way.

    Also resolves *entry*'s effective flavor id (:func:`kodo.llms.get_effective_flavor_id`)
    and passes it to :class:`LlamaServer` purely for crash messaging: if the
    process exits before becoming ready and the flavor isn't ``"default"``,
    :class:`LlamaServer` suggests trying the default flavor, since a bad
    custom flavor is the most likely cause.

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

    install = find_installed(kodo_dir)
    if install is None:
        raise RuntimeError("llama.cpp is not installed")

    if entry.kind == "custom_file":
        model_path: Path | None = Path(entry.path)
        if model_path is None or not model_path.is_file():
            raise RuntimeError(f"Model file not found: {entry.path!r}")
    else:
        model_path = get_local_model_manager(kodo_dir).get_model_path(entry.name)
        if model_path is None:
            raise RuntimeError(f"Model {entry.name!r} is not installed")

    override = get_llama_server_override_path(kodo_dir)
    executable = Path(override) if override else install.executable

    # entry's resolved flavor (active, or its first/default one) supplies the
    # complete llama_args — see LlamaFlavor/resolve_effective_llama_config.
    llama_args, _ = resolve_effective_llama_config(kodo_dir, entry)
    flavor_id = get_effective_flavor_id(kodo_dir, entry)
    if local_thinking_family(entry.base_llm) == "qwen_reasoning_budget":
        # Forced (plain assignment), not defaulted: -1 is mandatory here, not
        # just the default — it's what makes the per-request
        # `thinking_budget_tokens` override in _llama.py take effect at all,
        # and any other explicit CLI value would lock it out. A flavor must
        # never be able to override or lock this out — add_flavor/
        # update_flavor already strip RESERVED_REASONING_CAP_ARGS before a
        # flavor is even saved; this is the second, load-bearing line of
        # defense for anything saved before that existed.
        llama_args["--reasoning-budget"] = "-1"
        llama_args["--reasoning-budget-message"] = REASONING_BUDGET_MESSAGE

    cfg = LlamaServerConfig(
        executable=executable,
        model_path=model_path,
        kodo_dir=kodo_dir,
        model_name=entry.name,
    )
    server = LlamaServer(cfg, llama_args, flavor_id=flavor_id)
    await server.start()
    return server
