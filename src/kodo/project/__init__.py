"""Kodo project layout conventions and ``kodo.md`` manifest parser."""

from ._layout import (
    ProjectLayout,
    ProjectLayoutError,
    SessionWorkspace,
    WorkspaceLayout,
    kodo_user_dir,
    session_attachments_dir,
    session_temp_dir,
)
from ._manifest import Manifest, ManifestError, parse_manifest

__all__ = [
    "ProjectLayout",
    "ProjectLayoutError",
    "SessionWorkspace",
    "WorkspaceLayout",
    "kodo_user_dir",
    "session_attachments_dir",
    "session_temp_dir",
    "Manifest",
    "ManifestError",
    "parse_manifest",
]
