"""Virtual artifact workspace for Kodo projects.

Also hosts the git **mirror** subsystem (formerly ``kodo.mirror``): the
checkpoint repository, artifact promotion, and checkpoint management. The mirror
is the durable, on-disk counterpart of the in-memory :class:`ProjectIndex` —
promotion writes accepted artifacts into both the live project tree and the
mirror, and rollback restores from it — so it lives under the workspace umbrella.
"""

from ._checkpoints import CheckpointManager
from ._component_registry import ComponentRegistry
from ._errors import ArtifactNotFoundError, WorkspaceError, WorkspaceValidationError
from ._index import ArtifactState, IndexEntry, ProjectIndex
from ._materialization import dematerialize, materialization_path, materialize
from ._models import Artifact, ArtifactType, Concern, Verdict
from ._promoter import Promoter, PromoterError
from ._repo import CheckpointInfo, MirrorRepo, MirrorRepoError
from ._workspace import Workspace

__all__ = [
    "Artifact",
    "ArtifactNotFoundError",
    "ArtifactState",
    "ArtifactType",
    "CheckpointInfo",
    "CheckpointManager",
    "ComponentRegistry",
    "Concern",
    "IndexEntry",
    "MirrorRepo",
    "MirrorRepoError",
    "ProjectIndex",
    "Promoter",
    "PromoterError",
    "Verdict",
    "Workspace",
    "WorkspaceError",
    "WorkspaceValidationError",
    "dematerialize",
    "materialization_path",
    "materialize",
]
