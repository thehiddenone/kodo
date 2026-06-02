"""llama-server process manager.

Starts a ``llama-server`` subprocess, waits for it to pass its health check,
and provides clean shutdown.  The server exposes an OpenAI-compatible REST
API accessible via :attr:`LlamaServer.base_url`.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import logging
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

__all__ = ["LlamaServer", "LlamaServerConfig"]

_log = logging.getLogger(__name__)

_HEALTH_POLL_INTERVAL: float = 0.5  # seconds between /health polls
_HEALTH_TIMEOUT: float = 120.0  # seconds before giving up on startup
_STOP_GRACE: float = 5.0  # seconds between SIGTERM and SIGKILL


@dataclass(frozen=True)
class LlamaServerConfig:
    """Configuration for a :class:`LlamaServer` instance.

    Attributes:
        executable: Path to the ``llama-server`` binary.
        model_path: Path to the ``.gguf`` model file.
        host: Bind address.  Defaults to ``'127.0.0.1'``.
        port: TCP port.  Defaults to ``8080``.
        context_size: Model context window in tokens.  Defaults to ``262144``.
        n_gpu_layers: Layers to offload to GPU; ``-1`` means all layers.
        llama_args: Extra CLI flags passed verbatim to ``llama-server``.
        extra_args: Additional CLI arguments appended after all other flags.
    """

    executable: Path
    model_path: Path
    host: str = "127.0.0.1"
    port: int = 8080
    context_size: int = 262144
    n_gpu_layers: int = -1
    llama_args: dict[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()


class LlamaServer:
    """Manages a ``llama-server`` subprocess.

    Lifecycle: create → :meth:`start` → use :attr:`base_url` → :meth:`stop`.
    Re-starting after a stop is supported.

    Args:
        config (LlamaServerConfig): Server configuration.
    """

    __config: LlamaServerConfig
    __process: asyncio.subprocess.Process | None
    __drain_task: asyncio.Task[None] | None

    def __init__(self, config: LlamaServerConfig) -> None:
        """Initialise without starting the subprocess.

        Args:
            config (LlamaServerConfig): Server configuration.
        """
        self.__config = config
        self.__process = None
        self.__drain_task = None

    @property
    def is_running(self) -> bool:
        """``True`` if the server process is alive."""
        return self.__process is not None and self.__process.returncode is None

    @property
    def port(self) -> int:
        """TCP port the server is (or will be) listening on."""
        return self.__config.port

    @property
    def base_url(self) -> str:
        """Base URL of the OpenAI-compatible REST API."""
        return f"http://{self.__config.host}:{self.__config.port}"

    async def start(self) -> None:
        """Start the server process and wait until it reports healthy.

        Raises:
            RuntimeError: If the server is already running, or if the process
                exits before passing the health check.
            TimeoutError: If the server does not become ready within
                ``_HEALTH_TIMEOUT`` seconds.
        """
        if self.is_running:
            raise RuntimeError("llama-server is already running")

        cmd = self.__build_command()
        _log.debug("Starting llama-server: %s", " ".join(cmd))

        self.__process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.__drain_task = asyncio.get_running_loop().create_task(
            self.__drain_logs(),
            name="llama-server-logs",
        )

        await self.__wait_ready()
        _log.info("llama-server ready at %s (pid=%d)", self.base_url, self.__process.pid)

    async def stop(self) -> None:
        """Stop the server process.

        Sends SIGTERM (or ``TerminateProcess`` on Windows) and waits up to
        ``_STOP_GRACE`` seconds before issuing SIGKILL.
        """
        proc = self.__process
        if proc is None or proc.returncode is not None:
            return

        _log.debug("Stopping llama-server (pid=%d)", proc.pid)
        proc.terminate()

        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE)
        except TimeoutError:
            _log.warning("llama-server did not stop gracefully; killing")
            proc.kill()
            await proc.wait()

        self.__process = None

        if self.__drain_task is not None and not self.__drain_task.done():
            self.__drain_task.cancel()
        self.__drain_task = None

        _log.info("llama-server stopped")

    def __build_command(self) -> list[str]:
        cfg = self.__config
        cmd: list[str] = [
            str(cfg.executable),
            "--model",
            str(cfg.model_path),
            "--host",
            cfg.host,
            "--port",
            str(cfg.port),
            "--ctx-size",
            str(cfg.context_size),
            "--n-gpu-layers",
            str(cfg.n_gpu_layers),
        ]
        for k, v in cfg.llama_args.items():
            cmd.append(k)
            cmd.append(v)
        cmd.extend(cfg.extra_args)
        return cmd

    async def __drain_logs(self) -> None:
        proc = self.__process
        if proc is None or proc.stdout is None:
            return
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            _log.debug("[llama-server] %s", line_bytes.decode(errors="replace").rstrip())

    async def __wait_ready(self) -> None:
        url = f"{self.base_url}/health"
        elapsed = 0.0

        async with aiohttp.ClientSession() as session:
            while elapsed < _HEALTH_TIMEOUT:
                if not self.is_running:
                    rc = self.__process.returncode if self.__process is not None else "?"
                    raise RuntimeError(
                        f"llama-server exited before becoming ready (returncode={rc})"
                    )
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=1.0)) as resp:
                        if resp.status == 200:
                            return
                except Exception:
                    pass
                await asyncio.sleep(_HEALTH_POLL_INTERVAL)
                elapsed += _HEALTH_POLL_INTERVAL

        raise TimeoutError(f"llama-server did not become ready within {_HEALTH_TIMEOUT:.0f} s")
