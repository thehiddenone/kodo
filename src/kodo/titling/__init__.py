"""Local session-titling summarizer.

Public surface for :mod:`kodo.runtime._engine._titling`'s
:class:`~kodo.runtime._engine._titling.SessionTitler`, which fires this off in
a background thread rather than running it as a sub-agent LLM turn. See
``doc/SESSIONS.md`` and ``doc/WS_PROTOCOL.md`` (``session.name``/
``session.naming``) for the wire contract this feeds.
"""

from __future__ import annotations

from ._summarizer import generate_title, titler_home_dir, warm_up_titler_cache

__all__ = ["generate_title", "titler_home_dir", "warm_up_titler_cache"]
