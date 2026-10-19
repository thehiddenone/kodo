"""What the Kodo agent did in each trial of a Harbor job, read as it happens.

``kodo.harbor.agent`` sends ``kodo-headless``'s JSONL events (doc/HEADLESS.md
§2.1) to ``/logs/agent/kodo.jsonl`` in the trial's container. Harbor's
``docker`` environment bind-mounts ``/logs/agent`` to ``<trial>/agent/`` on
the host, so the file grows there while the agent runs. In a multi-step task
Harbor moves ``agent/``'s contents to ``steps/<step>/agent/`` after each step,
and the next step writes a new ``agent/kodo.jsonl``.

:class:`TrialActivity` follows these files across those moves. It names each
log by a digest of its first line (the ``run.start`` event, whose timestamp
and temporary home are unique to the run), so a log keeps its name, and its
read position, when Harbor moves it. A log is never held open between reads,
and on Windows a read opens it with ``FILE_SHARE_DELETE``: Windows refuses to
move a file someone holds open without it, and Harbor would fail the step.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

__all__ = ["TrialActivity"]

_LOG_NAME = "kodo.jsonl"
_RESULT_NAME = "result.json"


@dataclass
class _Cursor:
    """How far one log has been read."""

    offset: int = 0
    count: int = 0
    step: str | None = None


class TrialActivity:
    """Reads the Kodo agent's event log of every trial in a Harbor job directory, as it grows."""

    __job_dir: Path
    __cursors: dict[tuple[str, str], _Cursor]
    __ended: set[str]

    def __init__(self, job_dir: Path) -> None:
        """Start before the first event of every trial.

        Args:
            job_dir (Path): A Harbor job directory (it need not exist yet).
        """
        self.__job_dir = job_dir
        self.__cursors = {}
        self.__ended = set()

    def read(self) -> list[dict[str, object]]:
        """Read what every trial's logs gained since the last call.

        The first call returns every log in full. A trial whose ``result.json``
        was there before a read is not read again after it: Harbor files every
        log before it writes that.

        Returns:
            list[dict[str, object]]: One chunk per log that gained events, or
            whose step became known: ``{trial, log, step, started, first,
            events}``. ``log`` names the log within its trial; ``events`` are
            its new events, in order, and ``first`` is the position of
            ``events[0]`` among all of the log's events. ``started`` is the
            ``ts`` of its first event. ``step`` is the step's name once
            Harbor has filed the log under ``steps/``, else ``None``.
        """
        try:
            trial_dirs = sorted(path for path in self.__job_dir.iterdir() if path.is_dir())
        except OSError:
            return []
        chunks: list[dict[str, object]] = []
        for trial_dir in trial_dirs:
            trial = trial_dir.name
            if trial in self.__ended:
                continue
            ended = (trial_dir / _RESULT_NAME).is_file()
            for path, step in _trial_logs(trial_dir):
                chunk = self.__read_log(trial, path, step)
                if chunk is not None:
                    chunks.append(chunk)
            if ended:
                self.__ended.add(trial)
        return chunks

    def __read_log(self, trial: str, path: Path, step: str | None) -> dict[str, object] | None:
        try:
            with _open_shared(path) as stream:
                head = stream.readline()
                if not head.endswith(b"\n"):
                    return None  # not even its first event yet
                log = hashlib.blake2b(head, digest_size=8).hexdigest()
                cursor = self.__cursors.setdefault((trial, log), _Cursor())
                stream.seek(cursor.offset)
                data = stream.read()
        except OSError:
            return None
        end = data.rfind(b"\n") + 1  # a half-written last line waits for the next read
        events = [event for line in data[:end].splitlines() if (event := _event(line)) is not None]
        first = cursor.count
        cursor.offset += end
        cursor.count += len(events)
        filed = step is not None and cursor.step is None
        if filed:
            cursor.step = step
        if not events and not filed:
            return None
        started = _event(head)
        return {
            "trial": trial,
            "log": log,
            "step": cursor.step,
            "started": str(started.get("ts", "")) if started is not None else "",
            "first": first,
            "events": events,
        }


def _trial_logs(trial_dir: Path) -> list[tuple[Path, str | None]]:
    """The trial's logs: each step's filed one, then the one being written."""
    logs: list[tuple[Path, str | None]] = []
    steps_dir = trial_dir / "steps"
    if steps_dir.is_dir():
        try:
            step_dirs = sorted(path for path in steps_dir.iterdir() if path.is_dir())
        except OSError:
            step_dirs = []
        for step_dir in step_dirs:
            path = step_dir / "agent" / _LOG_NAME
            if path.is_file():
                logs.append((path, step_dir.name))
    current = trial_dir / "agent" / _LOG_NAME
    if current.is_file():
        logs.append((current, None))
    return logs


def _event(line: bytes) -> dict[str, object] | None:
    """One JSONL line → its event; ``None`` for anything else (``KODO-RESULT``)."""
    try:
        loaded: object = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if isinstance(loaded, dict) and "type" in loaded:
        return cast(dict[str, object], loaded)
    return None


if sys.platform == "win32":
    import ctypes
    import msvcrt

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete (which includes rename)
    _OPEN_EXISTING = 3
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = ctypes.c_void_p
    _kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]

    def _open_shared(path: Path) -> BinaryIO:
        """Open *path* for reading without stopping anyone moving or deleting it."""
        handle = _kernel32.CreateFileW(
            str(path), _GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING, 0, None
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        return os.fdopen(descriptor, "rb")

else:

    def _open_shared(path: Path) -> BinaryIO:
        """Open *path* for reading (POSIX never stops a move of an open file)."""
        return path.open("rb")
