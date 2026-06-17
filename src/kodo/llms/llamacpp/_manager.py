"""On-demand llama-server lifecycle manager."""

from __future__ import annotations

from pathlib import Path

from ._downloader import get_model_path
from ._installer import find_installed
from ._llama_server import LlamaServer, LlamaServerConfig

__all__ = ["ensure_llama_running"]


async def ensure_llama_running(
    model_name: str,
    kodo_dir: Path,
    llama_args: dict[str, str] | None = None,
) -> LlamaServer:
    """Start llama-server for *model_name* if not already running.

    If a server is already running with the same model, return it immediately.
    If a server is running with a different model, stop it first then start fresh.

    Args:
        model_name (str): Registry name of the model to serve.
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        llama_args (dict[str, str] | None): Extra CLI flags passed verbatim to
            ``llama-server``. Defaults to empty.

    Returns:
        LlamaServer: The running server instance.

    Raises:
        RuntimeError: If llama.cpp is not installed, the model is not
            downloaded, or the server fails to start.
    """
    server = LlamaServer.get_active_llama_server()
    if server is not None and server.is_running:
        if server.model_name == model_name:
            return server
        await server.stop()

    install = find_installed(kodo_dir)
    if install is None:
        raise RuntimeError("llama.cpp is not installed")

    model_path = get_model_path(model_name, kodo_dir)
    if model_path is None:
        raise RuntimeError(f"Model {model_name!r} is not installed")

    cfg = LlamaServerConfig(
        executable=install.executable,
        model_path=model_path,
        kodo_dir=kodo_dir,
        model_name=model_name,
        llama_args=llama_args or {},
    )
    server = LlamaServer(cfg)
    await server.start()
    return server
