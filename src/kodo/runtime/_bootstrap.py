"""Guide session location (workspace tier).

:func:`locate_guide_session` locates or creates the session from the
workspace-level marker + ``sessions/`` dir. Runs at server start, before any
project is bound. There is no project-tier bootstrap anymore: a document's
state lives entirely in its own ``.jsonl`` evolution log (see
``kodo.guided_state``), read on demand — there is no in-memory index to
rebuild.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kodo.state import new_session_id

from ._guide import GuideMarker

_log = logging.getLogger(__name__)


def locate_guide_session(marker_dir: Path, sessions_dir: Path) -> tuple[str, bool]:
    """Locate or create the Guide session (workspace tier).

    The session is workspace-scoped (mode-agnostic — Guide and Problem
    Solver share it), so its marker and store live under
    ``.kodo-workspace/`` regardless of which project is later bound.

    Args:
        marker_dir (Path): Directory holding the guide session marker
            (``.kodo-workspace/``).
        sessions_dir (Path): Directory holding per-session stores
            (``.kodo-workspace/sessions/``).

    Returns:
        tuple[str, bool]: ``(session_id, resumed)`` where ``resumed`` is
        ``True`` if an existing session directory was found.
    """
    marker = GuideMarker(marker_dir)
    existing = marker.read()

    if existing:
        session_dir = sessions_dir / existing
        if session_dir.is_dir():
            _log.info("Guide session resumed: %s", existing)
            return existing, True
        _log.warning(
            "Guide marker points to missing session dir %s — discarding marker and starting fresh",
            existing,
        )
        marker.clear()

    session_id = new_session_id()
    marker.write(session_id)
    _log.info("Guide session started: %s", session_id)
    return session_id, False
