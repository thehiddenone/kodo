"""Shared dataclasses, enums, and exceptions for :mod:`kodo.llms.local`."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field


class FileRole(enum.StrEnum):
    """What role a single downloaded file plays within a model."""

    MAIN = "main"
    """The only file (unsharded model), or the first shard of a split GGUF —
    the path handed to llama-server."""

    SHARD = "shard"
    """A non-first shard of a split GGUF. llama-server discovers these itself
    from the MAIN file's directory; kodo never points at them directly."""

    MMPROJ = "mmproj"
    """A multimodal-projector companion GGUF for a vision-capable model."""


class FileStatus(enum.StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ModelFile:
    """One file belonging to a :class:`ModelRecord`.

    Attributes:
        filename: Path of the file within its HF repo (may include a
            subfolder, e.g. ``"subdir/model-00001-of-00003.gguf"``).
        role: See :class:`FileRole`.
        repo_id: HuggingFace repo this file was fetched from — recorded per
            file (not just per model) since a mmproj file may come from a
            different repo than the main model.
        revision: Git revision requested (branch/tag/commit) at download time.
        size: Expected size in bytes, once known from HF metadata.
        etag: HF ETag of the file, once known.
        downloaded_bytes: Best-effort byte count last persisted to disk.
            Informational only — actual resume position is read from the
            real ``.part`` file size on disk, not this field.
        status: Current lifecycle state.
        error: Last error message, if ``status == FAILED``.
    """

    filename: str
    role: FileRole
    repo_id: str
    revision: str = "main"
    size: int | None = None
    etag: str | None = None
    downloaded_bytes: int = 0
    status: FileStatus = FileStatus.PENDING
    error: str = ""

    @property
    def is_complete(self) -> bool:
        return self.status == FileStatus.COMPLETED


@dataclass
class ModelRecord:
    """Everything the manager knows about one downloaded/downloading model.

    ``model_id`` is a caller-chosen key with no meaning to this package —
    callers (e.g. the ``kodo.llms.llamacpp`` glue layer) decide what it maps
    to on their side (a local-registry entry name, typically).
    """

    model_id: str
    repo_id: str
    revision: str
    commit_hash: str | None
    files: list[ModelFile] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def main_files(self) -> list[ModelFile]:
        """The model's own file(s) — MAIN plus any SHARD parts, in order."""
        return [f for f in self.files if f.role in (FileRole.MAIN, FileRole.SHARD)]

    @property
    def primary_file(self) -> ModelFile | None:
        """The MAIN file — the path callers should hand to llama-server."""
        return next((f for f in self.files if f.role == FileRole.MAIN), None)

    @property
    def mmproj_file(self) -> ModelFile | None:
        return next((f for f in self.files if f.role == FileRole.MMPROJ), None)

    @property
    def is_installed(self) -> bool:
        """True once every MAIN/SHARD file has finished downloading.

        Deliberately ignores mmproj — a model is usable for text inference
        without its (optional) mmproj companion.
        """
        mains = self.main_files
        return bool(mains) and all(f.is_complete for f in mains)

    @property
    def has_resumable_work(self) -> bool:
        """True if some file isn't finished and no download is active for it.

        ``DOWNLOADING`` also counts as resumable here — from the state file's
        point of view (read after a restart), a file stuck at ``DOWNLOADING``
        can only mean the process died mid-transfer.
        """
        return any(not f.is_complete for f in self.files)


@dataclass(frozen=True)
class DownloadProgress:
    """One progress update, passed to a :data:`ProgressCallback`."""

    model_id: str
    filename: str
    file_index: int
    file_count: int
    bytes_downloaded: int
    bytes_total: int | None
    overall_bytes_downloaded: int
    overall_bytes_total: int | None
    message: str


ProgressCallback = Callable[[DownloadProgress], None]


class LocalModelError(Exception):
    """Base class for every error raised by :mod:`kodo.llms.local`."""


class ModelNotFoundError(LocalModelError):
    """Raised when an operation references a ``model_id`` the manager doesn't know."""


class ShardResolutionError(LocalModelError):
    """Raised when a repo's file listing doesn't match an expected shard set."""


class DownloadError(LocalModelError):
    """Raised on a network/IO/consistency failure while transferring a file."""


class DownloadPausedError(LocalModelError):
    """Internal control-flow signal raised to unwind out of a paused transfer.

    Never escapes the manager's public API — callers see the resulting
    ``ModelRecord`` with the affected file's status set to ``PAUSED`` instead.
    """
