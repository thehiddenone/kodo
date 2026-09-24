"""On-disk state of standalone llama-servers — one JSON file per port.

Deliberately *not* ``~/.kodo/llama.cpp/llama-server.json``: that file is the
kodo server's own runtime record, which its startup adopts (and later stops).
A standalone server lives in ``~/.kodo/llama.cpp/standalone/<port>.json`` so
no kodo server ever takes ownership of it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

__all__ = ["StandaloneState"]

_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})


@dataclass(frozen=True)
class StandaloneState:
    """What one standalone llama-server's state file records.

    Attributes:
        supervisor_pid: The ``kodo-llama-server`` supervisor process owning the
            llama-server, or ``0`` while only llama-server's own runtime record
            has been written (the window between it passing ``/health`` and the
            supervisor stamping the full record).
        llama_pid: The llama-server process.
        host: Bind address.
        port: TCP port.
        model: Local-registry entry name being served (also its ``--alias``).
        profile_id: Active user-defined profile at launch, ``""`` for Default.
        kodo_version: ``kodo.__version__`` of the supervisor that launched it.
    """

    supervisor_pid: int
    llama_pid: int
    host: str
    port: int
    model: str
    profile_id: str = ""
    kodo_version: str = ""

    @staticmethod
    def path_for(kodo_dir: Path, port: int) -> Path:
        """The state file for *port*.

        Args:
            kodo_dir (Path): User-level ``~/.kodo`` directory.
            port (int): The server's TCP port.

        Returns:
            Path: ``kodo_dir/llama.cpp/standalone/<port>.json``.
        """
        return kodo_dir / "llama.cpp" / "standalone" / f"{port}.json"

    @classmethod
    def read(cls, path: Path) -> StandaloneState | None:
        """Parse a state file; ``None`` when missing or unreadable.

        Also accepts llama-server's own bare runtime record
        (``{pid, host, port, model}``), reported with ``supervisor_pid == 0``,
        so a server caught between becoming healthy and being stamped is still
        visible to ``status`` and stoppable by ``stop``.

        Args:
            path (Path): The state file.

        Returns:
            StandaloneState | None: The parsed state.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        data = cast(dict[str, object], raw)
        try:
            llama_pid = data.get("llama_pid", data.get("pid"))
            return cls(
                supervisor_pid=int(cast(int, data.get("supervisor_pid", 0))),
                llama_pid=int(cast(int, llama_pid)),
                host=str(data.get("host", "127.0.0.1")),
                port=int(cast(int, data["port"])),
                model=str(data.get("model", "")),
                profile_id=str(data.get("profile_id", "")),
                kodo_version=str(data.get("kodo_version", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def read_all(cls, kodo_dir: Path) -> list[tuple[Path, StandaloneState]]:
        """Every parseable state file, ordered by port.

        Args:
            kodo_dir (Path): User-level ``~/.kodo`` directory.

        Returns:
            list[tuple[Path, StandaloneState]]: ``(state file, state)`` pairs.
        """
        directory = cls.path_for(kodo_dir, 0).parent
        if not directory.is_dir():
            return []
        found: list[tuple[Path, StandaloneState]] = []
        for path in sorted(directory.glob("*.json")):
            state = cls.read(path)
            if state is not None:
                found.append((path, state))
        return sorted(found, key=lambda pair: pair[1].port)

    @property
    def is_stamped(self) -> bool:
        """``True`` once the supervisor has written the full record."""
        return self.supervisor_pid > 0

    @property
    def base_url(self) -> str:
        """URL a local client connects to (a wildcard bind maps to loopback)."""
        host = "127.0.0.1" if self.host in _WILDCARD_HOSTS else self.host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def write(self, path: Path) -> None:
        """Atomically write this record to *path*.

        Args:
            path (Path): The state file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "supervisor_pid": self.supervisor_pid,
                    "llama_pid": self.llama_pid,
                    "host": self.host,
                    "port": self.port,
                    "model": self.model,
                    "profile_id": self.profile_id,
                    "kodo_version": self.kodo_version,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
