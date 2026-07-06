"""On-demand llama-server lifecycle manager."""

from __future__ import annotations

from pathlib import Path

from kodo.llms import LocalLLMEntry, get_llama_server_override_path

from ._downloader import get_model_path
from ._installer import find_installed
from ._llama_server import LlamaServer, LlamaServerConfig

__all__ = ["ensure_llama_running"]


async def ensure_llama_running(entry: LocalLLMEntry, kodo_dir: Path) -> LlamaServer:
    """Start llama-server for *entry* if not already running.

    If a server is already running with the same model, return it immediately.
    If a server is running with a different model, stop it first then start
    fresh. Not valid for ``custom_server_url`` entries — those are not managed
    by kodo at all; callers must special-case that kind before reaching here
    (see :class:`kodo.llms.llamacpp.LlamaPlugin`).

    If a llama-server binary override is configured (see
    :func:`kodo.llms.set_llama_server_override_path`), it is used as the
    executable in place of the bundled llama.cpp build — the CLI-argument
    generation in :class:`LlamaServerConfig`/:class:`LlamaServer` is unchanged
    either way.

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
        model_path = get_model_path(entry.name, kodo_dir)
        if model_path is None:
            raise RuntimeError(f"Model {entry.name!r} is not installed")

    override = get_llama_server_override_path(kodo_dir)
    executable = Path(override) if override else install.executable

    cfg = LlamaServerConfig(
        executable=executable,
        model_path=model_path,
        kodo_dir=kodo_dir,
        model_name=entry.name,
        llama_args=entry.llama_args,
    )
    server = LlamaServer(cfg)
    await server.start()
    return server
