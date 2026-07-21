"""The shared "no project bound" error every ``requires_project`` tool returns.

Mirrors :mod:`kodo.toolspecs._intent`'s shape: the message is defined once
here so it can never drift between call sites. :class:`~kodo.tools.ToolDispatcher`
returns it verbatim (wrapped as ``{"error": NO_PROJECT_ERROR}``) instead of
dispatching, for any tool whose spec sets ``requires_project=True`` when no
project is bound and the call isn't scoped to the private scratch directory
(``temporary: true``).
"""

from __future__ import annotations

__all__ = ["NO_PROJECT_ERROR"]

NO_PROJECT_ERROR = (
    "No project is open in this session. Call `create_new_project` to create "
    "one before making this tool call."
)
