"""Mirror git repository and checkpoint management.

Stub for M1; full implementation in M3.
"""

from ._checkpoints import CheckpointManager
from ._repo import CheckpointInfo, MirrorRepo, MirrorRepoError

__all__: list[str] = ["CheckpointInfo", "CheckpointManager", "MirrorRepo", "MirrorRepoError"]
