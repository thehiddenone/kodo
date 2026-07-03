"""Per-engine anti-bot cooldown state, persisted as one small JSON file.

When an engine serves a captcha / anti-bot wall, ``web_search`` stops querying
it for :data:`COOLDOWN_SECONDS` (30 minutes). The state must survive across
tool calls, sessions, and server restarts, so it lives on disk — the caller
passes the file path (the ``web_search`` tool uses
``~/.kodo/websearch/engine_cooldowns.json``), keeping this package a pure leaf
with no knowledge of the Kodo home layout.

The file maps engine name → unix timestamp until which the engine is blocked:

    {"google": 1751536800.0}

Reads and writes are best-effort: a missing or corrupt file reads as "no
cooldowns", and writes go through a same-directory temp file + ``os.replace``
so a crash can never leave a half-written file behind.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

__all__ = ["COOLDOWN_SECONDS", "CooldownStore"]

_log = logging.getLogger(__name__)

# How long an engine is left alone after serving an anti-bot / captcha wall.
COOLDOWN_SECONDS: float = 30 * 60


class CooldownStore:
    """Read/write the per-engine cooldown file.

    Args:
        path: The JSON state file (parent directories are created on first
            write).
    """

    __path: Path

    def __init__(self, path: Path) -> None:
        self.__path = path

    def remaining(self, engine: str) -> float:
        """Seconds left on *engine*'s cooldown; ``0.0`` when it may be queried."""
        until = self.__load().get(engine, 0.0)
        return max(0.0, until - time.time())

    def trip(self, engine: str, seconds: float = COOLDOWN_SECONDS) -> None:
        """Record that *engine* served an anti-bot wall: block it for *seconds*."""
        state = self.__load()
        state[engine] = time.time() + seconds
        self.__save(state)

    def __load(self) -> dict[str, float]:
        try:
            raw = json.loads(self.__path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            _log.warning("Unreadable cooldown file %s; treating as empty", self.__path)
            return {}
        if not isinstance(raw, dict):
            return {}
        state: dict[str, float] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, (int, float)):
                state[key] = float(value)
        return state

    def __save(self, state: dict[str, float]) -> None:
        try:
            self.__path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.__path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self.__path)
        except OSError:
            # Best effort — a failed save only means the cooldown is forgotten.
            _log.warning("Could not persist cooldown file %s", self.__path)
