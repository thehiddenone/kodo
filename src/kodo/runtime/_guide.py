"""Guide session marker — tracks the current Guide session_id on disk.

The marker file at ``<project>/.kodo/guide.session`` contains a single
line: the current Guide session_id.  Bootstrap Phase 4
(STATE_AND_LIFECYCLE.md §3) reads this to decide whether to resume an existing
Guide session or start a fresh one.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["GuideMarker"]

_log = logging.getLogger(__name__)


class GuideMarker:
    """Reads and writes the Guide session marker file.

    Args:
        kodo_dir (Path): The ``.kodo/`` directory of the project.
    """

    __path: Path

    def __init__(self, kodo_dir: Path) -> None:
        """Initialise the marker with the project's .kodo directory.

        Args:
            kodo_dir (Path): Path to ``<project>/.kodo/``.
        """
        self.__path = kodo_dir / "guide.session"

    @property
    def path(self) -> Path:
        """Absolute path to the marker file."""
        return self.__path

    def read(self) -> str | None:
        """Return the stored session_id, or ``None`` if no marker exists.

        Returns:
            str | None: The session_id from the marker file, or None.
        """
        if not self.__path.exists():
            return None
        session_id = self.__path.read_text(encoding="utf-8").strip()
        return session_id if session_id else None

    def write(self, session_id: str) -> None:
        """Write a session_id to the marker file, creating it if absent.

        Args:
            session_id (str): The new Guide session_id.
        """
        self.__path.parent.mkdir(parents=True, exist_ok=True)
        self.__path.write_text(session_id + "\n", encoding="utf-8")
        _log.debug("Guide marker written: %s", session_id)

    def clear(self) -> None:
        """Delete the marker file (used on rollback to force a fresh session).

        No-op if the file does not exist.
        """
        self.__path.unlink(missing_ok=True)
        _log.debug("Guide marker cleared")
