"""Stateful HuggingFace GGUF model manager — download, pause/resume, uninstall.

Independent of every other ``kodo`` package by design (see
:class:`LocalModelManager`'s docstring) so that ``kodo.llms.llamacpp`` can
depend on this without a cycle. Nothing here knows about llama-server,
kodo's local-model *registry*, or ``~/.kodo`` — callers own translating their
own concepts (a registry entry, a settings-derived models directory) into
plain constructor/method arguments.

Full design: ``kodo/doc/LOCAL_MODEL_MANAGER.md``.
"""

from ._hf import ResolvedFile
from ._manager import LocalModelManager
from ._types import (
    DownloadError,
    DownloadProgress,
    FileRole,
    FileStatus,
    LocalModelError,
    ModelFile,
    ModelNotFoundError,
    ModelRecord,
    ProgressCallback,
    ShardResolutionError,
)

__all__ = [
    "DownloadError",
    "DownloadProgress",
    "FileRole",
    "FileStatus",
    "LocalModelError",
    "LocalModelManager",
    "ModelFile",
    "ModelNotFoundError",
    "ModelRecord",
    "ProgressCallback",
    "ResolvedFile",
    "ShardResolutionError",
]
