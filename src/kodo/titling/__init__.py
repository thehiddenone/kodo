"""Local session-titling summarizer.

Public surface for :mod:`kodo.runtime._engine._titling`'s
:class:`~kodo.runtime._engine._titling.SessionTitler`, which calls
:func:`generate_title` for any first prompt over 8 words, and for
``server/_app.py``, which calls :func:`start_titling`/:func:`stop_titling`
around kodo startup and llama.cpp install/update. See ``doc/SESSIONS.md`` and
``doc/WS_PROTOCOL.md`` (``session.name``/``session.naming``) for the wire
contract this feeds, and doc/INTERNALS.md §10c for the titler's own
dedicated-llama-server architecture.

:func:`generate_project_name` is an independent capability riding the same
dedicated llama-server: any caller may invent a short project name from a
description once the titler is up (used by
:mod:`kodo.runtime._engine._core`'s autonomous-mode project bootstrapping,
but not tied to it).
"""

from __future__ import annotations

from ._server import (
    generate_project_name,
    generate_title,
    start_titling,
    stop_titling,
    titler_home_dir,
)

__all__ = [
    "generate_project_name",
    "generate_title",
    "start_titling",
    "stop_titling",
    "titler_home_dir",
]
