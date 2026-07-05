"""Generic TTL key-value state store for the ``web_search`` agent (doc/WEB_SEARCH.md).

Replaces the old deterministic 30-minute per-engine ``CooldownStore``: the
``web_search`` agent now manages its own memory of which engines it has
flagged as bot-blocked and when it last queried each one, via the
``get_web_search_state``/``update_web_search_state`` tools. Each entry has a
:data:`TTL_SECONDS` TTL from its **last write** (refreshed on every
``update``, not just creation); :data:`TIME_MARK` is a special value that
records ``time.time()`` instead of a literal string — reading it back
returns the elapsed seconds (recomputed fresh on every read), not the
timestamp itself. See the tool specs for the protocol as explained to the
agent.

Persisted machine-wide (shared across every session), matching every other
piece of state in this package (``CooldownStore``/``browser_state.json``):
the TTL here is far longer than any single ``web_search`` call's timeout, so
this memory has to outlive the call that wrote it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TIME_MARK", "TTL_SECONDS", "WebSearchStateStore"]

_log = logging.getLogger(__name__)

# TTL for every entry, refreshed on each write to that key.
TTL_SECONDS: float = 12 * 60 * 60

# The special value `update_web_search_state` recognizes as "record now()".
TIME_MARK = "<time_mark>"


@dataclass
class _Entry:
    kind: str  # "value" | "time_mark"
    value: str
    ts: float


class WebSearchStateStore:
    """Read/write the ``web_search`` agent's persistent key-value memory.

    Args:
        path: The JSON state file (parent directories are created on first
            write).
    """

    __path: Path

    def __init__(self, path: Path) -> None:
        self.__path = path

    def get_all(self) -> dict[str, str]:
        """Evict expired entries, persist the cleanup, and return ``{key: value}``.

        A ``"time_mark"`` entry's returned value is the number of seconds
        elapsed since it was recorded (``time.time() - ts``), freshly
        computed on every call — never the timestamp itself.
        """
        entries = self.__load()
        now = time.time()
        live = {key: entry for key, entry in entries.items() if now - entry.ts < TTL_SECONDS}
        if len(live) != len(entries):
            self.__save(live)
        result: dict[str, str] = {}
        for key, entry in live.items():
            result[key] = str(now - entry.ts) if entry.kind == "time_mark" else entry.value
        return result

    def update(self, key: str, value: str) -> None:
        """Set, delete, or time-mark *key* per *value* (see module docstring).

        ``value == ""`` deletes *key*; ``value == TIME_MARK`` records the
        current time under *key*; anything else stores *value* verbatim.
        Every branch refreshes *key*'s TTL clock.
        """
        entries = self.__load()
        now = time.time()
        if value == "":
            entries.pop(key, None)
        elif value == TIME_MARK:
            entries[key] = _Entry(kind="time_mark", value="", ts=now)
        else:
            entries[key] = _Entry(kind="value", value=value, ts=now)
        self.__save(entries)

    def __load(self) -> dict[str, _Entry]:
        try:
            raw = json.loads(self.__path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            _log.warning("Unreadable web_search state file %s; treating as empty", self.__path)
            return {}
        if not isinstance(raw, dict):
            return {}
        entries: dict[str, _Entry] = {}
        for key, payload in raw.items():
            if not isinstance(key, str) or not isinstance(payload, dict):
                continue
            kind = payload.get("kind")
            ts = payload.get("ts")
            value = payload.get("value", "")
            if kind not in ("value", "time_mark") or not isinstance(ts, (int, float)):
                continue
            entries[key] = _Entry(
                kind=kind, value=value if isinstance(value, str) else "", ts=float(ts)
            )
        return entries

    def __save(self, entries: dict[str, _Entry]) -> None:
        try:
            self.__path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.__path.with_suffix(".tmp")
            raw = {
                key: {"kind": e.kind, "value": e.value, "ts": e.ts} for key, e in entries.items()
            }
            tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            os.replace(tmp, self.__path)
        except OSError:
            # Best effort — a failed save only means the memory is forgotten.
            _log.warning("Could not persist web_search state file %s", self.__path)
