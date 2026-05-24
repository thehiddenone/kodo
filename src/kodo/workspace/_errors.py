"""Workspace-specific exceptions."""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base class for all workspace errors."""


class WorkspaceValidationError(WorkspaceError):
    """Raised when a publish or read call violates a workspace rule."""


class ArtifactNotFoundError(WorkspaceError):
    """Raised when a referenced artifact ID does not exist in the live workspace."""
