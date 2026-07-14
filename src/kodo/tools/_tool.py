"""Abstract base class for every dispatchable tool.

A :class:`Tool` instance is bound to one agent run's :class:`ToolContext` (the
collaborators it may touch plus that run's mutable state). Subclasses implement
:meth:`handle`, reading the context through the read-only :attr:`context`
property. One concrete subclass lives in each ``_<tool_name>.py`` module,
mirroring the ``kodo.toolspecs`` one-file-per-tool convention.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from kodo.project import session_temp_dir

from ._context import ToolContext
from ._paths import resolve_within

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

    def resolve_path(self, path: str, *, temporary: bool = False) -> Path:
        """Resolve *path* through the run's active resolver, or the session's
        private scratch directory when *temporary*.

        ``temporary=True`` confines *path* under
        ``~/.kodo/sessions/<session_id>/tmp`` (:func:`kodo.project.session_temp_dir`)
        instead of the project root/workspace folders — relative paths land
        inside it, absolute paths must already resolve inside it (or the OS
        temp directory), or a :class:`PermissionError` is raised, exactly like
        :attr:`ToolContext.resolver`'s own containment guard. Callers pass the
        raw ``temporary`` tool-input value straight through; the coordinator
        that would otherwise checkpoint the mutation, and the security layer's
        gate, both special-case this same flag (see doc/SECURITY.md).
        """
        if temporary:
            return resolve_within(session_temp_dir(self.context.session_id), path)
        return self.context.resolver.resolve(path)

    @abstractmethod
    async def handle(self, tool_input: dict[str, object]) -> str:
        """Execute the tool and return a JSON-encoded result.

        Args:
            tool_input: Parsed JSON input from the LLM tool-use block.

        Returns:
            str: JSON-encoded result returned to the LLM as a tool result.
        """
        ...
