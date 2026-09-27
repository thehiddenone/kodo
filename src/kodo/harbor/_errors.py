"""The one error ``kodo-harbor`` reports to the user instead of a traceback."""

from __future__ import annotations

__all__ = ["HarborRunError"]


class HarborRunError(RuntimeError):
    """A benchmark run cannot start or finish; the message says how to fix it."""
