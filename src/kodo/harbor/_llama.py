"""The host llama-server a local-model benchmark attaches to (Harbor option A′).

The model runs on the host under ``kodo-llama-server`` (doc/HEADLESS.md §1);
each trial's ``kodo-headless`` reaches it from inside its container at
``http://host.docker.internal:<port>``:

- **macOS / Windows (Docker Desktop):** ``host.docker.internal`` already maps
  to the host's loopback, so the server binds ``127.0.0.1`` — nothing is
  exposed beyond the machine.
- **Linux:** the server binds the docker bridge address (``docker0``, usually
  ``172.17.0.1``), and a compose overlay adds
  ``host.docker.internal:host-gateway`` to the task's ``main`` service.

A server already serving the same model on the port is reused and left
running; one this run started is stopped when the run ends.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import cast

from ._errors import HarborRunError

__all__ = ["CONTAINER_HOST", "HostLlama", "LlamaAccess"]

#: The name a task container uses for the host.
CONTAINER_HOST = "host.docker.internal"

_DOCKER0_DEFAULT = "172.17.0.1"
_INET = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)/")
_LINUX_OVERLAY = f"""services:
  main:
    extra_hosts:
      - "{CONTAINER_HOST}:host-gateway"
"""


class LlamaAccess:
    """How the host llama-server is reached, from a container and from the host."""

    __entry: str
    __port: int
    __bind: str
    __context_per_slot: int

    def __init__(self, entry: str, port: int, bind: str, context_per_slot: int) -> None:
        """Bind the endpoint's facts.

        Args:
            entry (str): The local-registry entry it serves (also its alias).
            port (int): Its port.
            bind (str): The host address it listens on.
            context_per_slot (int): ``n_ctx`` each request gets.
        """
        self.__entry = entry
        self.__port = port
        self.__bind = bind
        self.__context_per_slot = context_per_slot

    @property
    def entry(self) -> str:
        """The model the server serves."""
        return self.__entry

    @property
    def container_url(self) -> str:
        """The URL a task container uses (``--llama-url``)."""
        return f"http://{CONTAINER_HOST}:{self.__port}"

    @property
    def host_url(self) -> str:
        """The URL a host process uses (a ``terminus-2`` control arm)."""
        return f"http://{self.__bind}:{self.__port}"

    @property
    def context_per_slot(self) -> int:
        """Tokens of context each request gets."""
        return self.__context_per_slot


class HostLlama:
    """Starts (or reuses) and stops the host's ``kodo-llama-server``."""

    __entry: str
    __port: int
    __bind: str
    __started: bool

    def __init__(self, entry: str, port: int, bind: str) -> None:
        """Bind the model and where it is served.

        Args:
            entry (str): Local-registry entry to serve.
            port (int): Host port.
            bind (str): Host address to listen on (:meth:`default_bind`).
        """
        self.__entry = entry
        self.__port = port
        self.__bind = bind
        self.__started = False

    @staticmethod
    def default_bind() -> str:
        """The address containers reach the host on, for this platform.

        Returns:
            str: ``127.0.0.1`` with Docker Desktop, else the ``docker0`` address.
        """
        if sys.platform != "linux":
            return "127.0.0.1"
        try:
            out = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "docker0"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return _DOCKER0_DEFAULT
        match = _INET.search(out)
        return match.group(1) if match else _DOCKER0_DEFAULT

    @staticmethod
    def compose_overlay() -> str | None:
        """The compose overlay a container needs to resolve the host, if any.

        Returns:
            str | None: YAML for Linux; ``None`` where Docker Desktop resolves it.
        """
        return _LINUX_OVERLAY if sys.platform == "linux" else None

    @property
    def started(self) -> bool:
        """Whether this run started the server (and so will stop it)."""
        return self.__started

    def start(self) -> LlamaAccess:
        """Reuse a server already serving the model on the port, or start one.

        Returns:
            LlamaAccess: How to reach it.

        Raises:
            HarborRunError: The port serves another model, or the server
            would not start.
        """
        running = self.__status()
        if running is not None and running.get("model") != self.__entry:
            raise HarborRunError(
                f"Port {self.__port} already serves {running.get('model')!r}; "
                f"stop it (kodo-llama-server stop --port {self.__port}) or pass --llama-port"
            )
        if running is None:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kodo.llamaserver",
                    "start",
                    "--model",
                    self.__entry,
                    "--port",
                    str(self.__port),
                    "--host",
                    self.__bind,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise HarborRunError(f"kodo-llama-server did not start: {detail}")
            self.__started = True
        return LlamaAccess(self.__entry, self.__port, self.__bind, self.__context_per_slot())

    def stop(self) -> None:
        """Stop the server if this run started it; a reused one keeps running."""
        if not self.__started:
            return
        subprocess.run(
            [sys.executable, "-m", "kodo.llamaserver", "stop", "--port", str(self.__port)],
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.__started = False

    def __status(self) -> dict[str, object] | None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "kodo.llamaserver",
                "status",
                "--port",
                str(self.__port),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            rows: object = json.loads(completed.stdout or "[]")
        except ValueError:
            return None
        if not isinstance(rows, list):
            return None
        for row in cast(list[object], rows):
            if isinstance(row, dict) and cast(dict[str, object], row).get("healthy"):
                return cast(dict[str, object], row)
        return None

    def __context_per_slot(self) -> int:
        url = f"http://{self.__bind}:{self.__port}/props"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                props: object = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HarborRunError(f"Cannot read {url}: {exc}") from exc
        if isinstance(props, dict):
            data = cast(dict[str, object], props)
            settings = data.get("default_generation_settings")
            for source in (settings if isinstance(settings, dict) else {}, data):
                value = cast(dict[str, object], source).get("n_ctx")
                if isinstance(value, int) and value > 0:
                    return value
        raise HarborRunError(f"{url} reports no n_ctx")
