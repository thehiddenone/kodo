"""The long-lived process that owns one standalone llama-server.

``kodo-llama-server start`` detaches a copy of itself in ``--foreground``
mode; that copy is the supervisor. It has to outlive the CLI invocation
because asyncio kills a child process when its subprocess transport closes —
a plain "spawn llama-server and exit" would take the server down with it.

The supervisor launches llama-server with exactly the flags the kodo server
would use for the same entry (:func:`kodo.llms.llamacpp.resolve_llama_launch`),
stamps the state file once it is healthy, then waits until it is asked to stop
or llama-server exits on its own — and always cleans up after itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from kodo import __version__
from kodo.llms import LocalLLMEntry, get_local_registry
from kodo.llms.llamacpp import (
    LlamaServer,
    LlamaServerConfig,
    find_installed_model_path,
    resolve_llama_launch,
)

from ._state import StandaloneState

__all__ = ["StandaloneError", "Supervisor", "resolve_standalone_entry"]

_log = logging.getLogger(__name__)

_LIVENESS_POLL_SECONDS = 0.5


class StandaloneError(RuntimeError):
    """A standalone llama-server cannot be started as requested."""


def resolve_standalone_entry(kodo_dir: Path, model: str) -> tuple[LocalLLMEntry, Path]:
    """Validate that *model* can be launched, before anything is spawned.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.
        model (str): Local-registry entry name (LLM + quant).

    Returns:
        tuple[LocalLLMEntry, Path]: The entry and its GGUF file.

    Raises:
        StandaloneError: Unknown entry, a ``custom_server_url`` entry (not a
            model kodo can launch), or a model that is not downloaded.
    """
    entry = get_local_registry(kodo_dir).get(model)
    if entry is None:
        raise StandaloneError(
            f"Unknown local model {model!r}. Pick a name from the Local Inference "
            "Settings panel (the local-LLM registry)."
        )
    if entry.kind == "custom_server_url":
        raise StandaloneError(
            f"{model!r} is a link to an external server ({entry.url}), not a model "
            "kodo-llama-server can launch."
        )
    model_path = find_installed_model_path(entry, kodo_dir)
    if model_path is None:
        raise StandaloneError(
            f"Model {model!r} is not installed. Install it from the Local Inference "
            "Settings panel first."
        )
    return entry, model_path


class Supervisor:
    """Owns one llama-server for its whole life."""

    __kodo_dir: Path
    __model: str
    __host: str
    __port: int

    def __init__(self, *, kodo_dir: Path, model: str, host: str, port: int) -> None:
        """Bind the server to launch; nothing starts until :meth:`run`.

        Args:
            kodo_dir (Path): User-level ``~/.kodo`` directory.
            model (str): Local-registry entry name to serve.
            host (str): Bind address.
            port (int): TCP port.
        """
        self.__kodo_dir = kodo_dir
        self.__model = model
        self.__host = host
        self.__port = port

    @property
    def state_path(self) -> Path:
        """This server's state file."""
        return StandaloneState.path_for(self.__kodo_dir, self.__port)

    async def run(self, stop: asyncio.Event) -> int:
        """Launch llama-server, wait until *stop* is set or it exits, clean up.

        Args:
            stop (asyncio.Event): Set to request a graceful shutdown.

        Returns:
            int: ``0`` when stopped on request, ``1`` when llama-server exited
            on its own.

        Raises:
            StandaloneError: The model cannot be launched.
            RuntimeError: llama-server exited before becoming ready (the
                message carries its output).
            TimeoutError: llama-server did not become ready in time.
        """
        entry, model_path = resolve_standalone_entry(self.__kodo_dir, self.__model)
        launch = resolve_llama_launch(entry, self.__kodo_dir)
        state_path = self.state_path
        config = LlamaServerConfig(
            executable=launch.executable,
            model_path=model_path,
            kodo_dir=self.__kodo_dir,
            model_name=entry.name,
            host=self.__host,
            port=self.__port,
            runtime_file=state_path,
            log_file=self.__kodo_dir / "logs" / f"llama-server-{self.__port}.log",
            alias=entry.name,
        )
        server = LlamaServer(config, launch.llama_args, profile_id=launch.profile_id)
        try:
            await server.start()
            StandaloneState(
                supervisor_pid=os.getpid(),
                llama_pid=server.pid or 0,
                host=self.__host,
                port=self.__port,
                model=entry.name,
                profile_id=launch.profile_id,
                kodo_version=__version__,
            ).write(state_path)
            _log.info("Serving %s at %s:%d", entry.name, self.__host, self.__port)
            while server.is_running and not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_LIVENESS_POLL_SECONDS)
            if not stop.is_set():
                _log.warning("llama-server on port %d exited on its own", self.__port)
                return 1
            return 0
        finally:
            await server.stop()
            state_path.unlink(missing_ok=True)
