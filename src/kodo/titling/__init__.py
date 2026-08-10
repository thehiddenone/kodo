"""Local session-titling summarizer.

Public surface for :mod:`kodo.runtime._engine._titling`'s
:class:`~kodo.runtime._engine._titling.SessionTitler`, which calls
:func:`generate_title` for any first prompt over 8 words, and for
``server/_app.py``, which calls :func:`start_titling`/:func:`stop_titling`
around kodo startup, llama.cpp install/update, and a `housekeeper_llm.set`
model switch. See ``doc/SESSIONS.md`` and ``doc/WS_PROTOCOL.md``
(``session.name``/``session.naming``) for the wire contract this feeds, and
doc/INTERNALS.md §10c for the titler's own dedicated-llama-server
architecture.

:data:`HOUSEKEEPER_LLM_OPTIONS` (a ``dict[str, HousekeeperLlmOption]``) and
:data:`DEFAULT_HOUSEKEEPER_LLM_ID` are the catalog of small models
:func:`start_titling` can run as "the housekeeper LLM" — the Kōdo Settings
panel's "General" section lists them and lets the user pick which one is
active (``housekeeper_llm.get``/``.set``, doc/WS_PROTOCOL.md §7.6f,
doc/SETTINGS.md §2.7).

:func:`generate_project_name` is an independent capability riding the same
dedicated llama-server: any caller may invent a short project name from a
description once the titler is up (used by
:mod:`kodo.runtime._engine._core`'s autonomous-mode project bootstrapping,
but not tied to it).

:func:`generate_greeting` is likewise independent: it writes a short, varied
opening greeting for a brand-new session, used by
:mod:`kodo.runtime._engine._greeting`'s
:class:`~kodo.runtime._engine._greeting.SessionGreeter`. See
``doc/WS_PROTOCOL.md`` (``session.greeting``) for the wire contract.
"""

from __future__ import annotations

from ._server import (
    DEFAULT_HOUSEKEEPER_LLM_ID,
    HOUSEKEEPER_LLM_OPTIONS,
    HousekeeperLlmOption,
    generate_greeting,
    generate_project_name,
    generate_title,
    start_titling,
    stop_titling,
    titler_home_dir,
)

__all__ = [
    "DEFAULT_HOUSEKEEPER_LLM_ID",
    "HOUSEKEEPER_LLM_OPTIONS",
    "HousekeeperLlmOption",
    "generate_greeting",
    "generate_project_name",
    "generate_title",
    "start_titling",
    "stop_titling",
    "titler_home_dir",
]
