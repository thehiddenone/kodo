"""The one error the model importer raises."""

from __future__ import annotations

__all__ = ["ModelImportError"]


class ModelImportError(ValueError):
    """A request the importer refused or could not complete.

    The message is written for the agent that made the request: it names what
    was wrong and, where there is one, the value that would have been accepted.
    A :class:`ValueError` because that is what ``kodo.tools``' ``LocalCatalogLike``
    protocol promises — the tools sit below this package and cannot name it.
    """
