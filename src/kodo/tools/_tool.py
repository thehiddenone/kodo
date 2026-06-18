"""Abstract base class for every dispatchable tool.

A :class:`Tool` instance is bound to one agent run's :class:`ToolContext` (the
collaborators it may touch plus that run's mutable state). Subclasses implement
:meth:`handle`, reading the context through the read-only :attr:`context`
property. One concrete subclass lives in each ``_<tool_name>.py`` module,
mirroring the ``kodo.toolspecs`` one-file-per-tool convention.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ._context import ToolContext

__all__ = ["Tool"]


class Tool(ABC):
    """Base class for a single dispatchable tool.

    Args:
        context: The per-run tool context injected by the
            :class:`~kodo.tools.ToolDispatcher`.
    """

    __context: ToolContext

    def __init__(self, context: ToolContext) -> None:
        self.__context = context

    @property
    def context(self) -> ToolContext:
        """The per-run tool context (collaborators + mutable run state)."""
        return self.__context

    @abstractmethod
    async def handle(self, tool_input: dict[str, object]) -> str:
        """Execute the tool and return a JSON-encoded result.

        Args:
            tool_input: Parsed JSON input from the LLM tool-use block.

        Returns:
            str: JSON-encoded result returned to the LLM as a tool result.
        """
        ...
