"""The stateful HuggingFace GGUF model manager."""

from __future__ import annotations

import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ._hf import detect_shard_group, list_repo_files, resolve_file
from ._http import download_to_part_file
from ._state import load_state, save_state
from ._types import (
    DownloadError,
    DownloadPausedError,
    DownloadProgress,
    FileRole,
    FileStatus,
    LocalModelError,
    ModelFile,
    ModelNotFoundError,
    ModelRecord,
    ProgressCallback,
)

__all__ = ["LocalModelManager"]

_STATE_FILE = "manager-state.json"
_PART_SUFFIX = ".part"

# How often an in-flight transfer persists its byte count to manager-state.json.
# kodo-vsix polls that file directly off disk (doc/LOCAL_MODEL_MANAGER.md §11)
# rather than over a WS push, so this interval is effectively the UI's refresh
# rate for a live download — frequent enough to feel live, coarse enough that a
# multi-GB transfer isn't rewriting the state file on every 64 KiB chunk.
_FLUSH_INTERVAL_SECONDS = 1.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sanitize_model_id(model_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", model_id).strip("._")
    return cleaned or "model"


class LocalModelManager:
    """Downloads, tracks, pauses/resumes, and removes GGUF models from HF Hub.

    Self-contained: no dependency on any other ``kodo`` package. Everything
    needed to resume after a process restart is either the real byte-count of
    the ``<file>.part`` files on disk (the source of truth for *where* a
    resumed download continues from) or this instance's own
    ``<root_dir>/manager-state.json`` (the source of truth for *what* files
    belong to a model and their last-known status).

    Independent from any model *registry* — this class has no notion of
    "known downloadable models"; it only knows about models a caller has
    already asked it to download at least once, keyed by a caller-chosen
    ``model_id`` opaque to this package. Wiring a registry entry (e.g.
    ``kodo.llms.LocalLLMEntry``) to a call here is entirely the caller's job.

    Not a singleton — this class enforces nothing about how many instances
    exist. Construct one per ``root_dir`` and reuse it for the process
    lifetime: reusing the instance is what makes :meth:`pause_download`
    correlate with an in-flight :meth:`download_model` call, since the
    pause signal is an in-memory ``threading.Event`` that does not survive
    across separate instances (a fresh instance after a restart can still
    resume any incomplete download via :meth:`resume_download`, just not
    *pause* a download it never started).
    """

    __root_dir: Path
    __state_path: Path
    __lock: threading.RLock
    __cancel_events: dict[str, threading.Event]

    def __init__(self, root_dir: Path) -> None:
        """Construct a manager rooted at *root_dir*.

        Args:
            root_dir (Path): Directory where model files and
                ``manager-state.json`` are stored. Each model gets its own
                subdirectory (``root_dir/<sanitized model_id>/``), so files
                never collide across models even when two models happen to
                share a filename.
        """
        self.__root_dir = Path(root_dir)
        self.__state_path = self.__root_dir / _STATE_FILE
        self.__lock = threading.RLock()
        self.__cancel_events = {}
        self.__reconcile_stale_downloads()

    def __reconcile_stale_downloads(self) -> None:
        """Force any file stuck ``DOWNLOADING`` in the state file to ``PAUSED``.

        Runs once, here in ``__init__`` — at the moment a fresh instance is
        constructed, ``__cancel_events`` is empty by definition, so no
        transfer this instance itself started can be in flight yet. Any file
        the loaded state still calls ``DOWNLOADING`` is therefore necessarily
        left over from a *previous* process that died mid-transfer (killed
        server, crashed host, ...) — never a live transfer this instance
        forgot about. Surfacing it as ``PAUSED`` instead of a stale
        "downloading" is what lets a caller offer "resume" rather than
        silently showing progress that will never move again.
        """

        with self.__lock:
            records = load_state(self.__state_path)
            stale = any(
                f.status == FileStatus.DOWNLOADING for r in records.values() for f in r.files
            )
            if not stale:
                return  # nothing to rewrite — don't create manager-state.json out of thin air

            for record in records.values():
                for file in record.files:
                    if file.status == FileStatus.DOWNLOADING:
                        file.status = FileStatus.PAUSED
                        record.updated_at = _now()
            save_state(self.__state_path, records)

    @property
    def root_dir(self) -> Path:
        return self.__root_dir

    def __model_dir(self, model_id: str) -> Path:
        return self.__root_dir / _sanitize_model_id(model_id)

    # ------------------------------------------------------------------
    # State I/O
    # ------------------------------------------------------------------

    def __mutate_state(self, mutate: Callable[[dict[str, ModelRecord]], None]) -> None:
        """Load state, apply *mutate* in place, and save — one atomic step.

        If *mutate* raises, nothing is written (the exception propagates
        before ``save_state`` runs).
        """
        with self.__lock:
            records = load_state(self.__state_path)
            mutate(records)
            save_state(self.__state_path, records)

    def __set_file_status(self, model_id: str, filename: str, **updates: object) -> None:
        def mutate(records: dict[str, ModelRecord]) -> None:
            record = records.get(model_id)
            if record is None:
                return
            for file in record.files:
                if file.filename == filename:
                    for key, value in updates.items():
                        setattr(file, key, value)
                    break
            record.updated_at = _now()

        self.__mutate_state(mutate)

    def get_record(self, model_id: str) -> ModelRecord | None:
        """Return everything known about *model_id*, or ``None`` if never downloaded.

        Args:
            model_id (str): Caller-chosen model key.

        Returns:
            ModelRecord | None: The model's current state, or ``None``.
        """
        with self.__lock:
            return load_state(self.__state_path).get(model_id)

    def __require_record(self, model_id: str) -> ModelRecord:
        record = self.get_record(model_id)
        if record is None:
            raise ModelNotFoundError(f"No download record for {model_id!r}")
        return record

    def list_models(self) -> list[ModelRecord]:
        """Return every model this manager has ever been asked to download.

        Returns:
            list[ModelRecord]: All known model records.
        """
        with self.__lock:
            return list(load_state(self.__state_path).values())

    def list_resumable(self) -> list[str]:
        """Return ``model_id``s with incomplete files and no active transfer.

        Meant for a caller to offer "resume interrupted download?" after a
        process restart — everything returned here is safe to pass to
        :meth:`resume_download` immediately.

        Returns:
            list[str]: Resumable model IDs.
        """
        with self.__lock:
            records = load_state(self.__state_path)
            active = set(self.__cancel_events)
        return [
            model_id
            for model_id, record in records.items()
            if record.has_resumable_work and model_id not in active
        ]

    def get_model_path(self, model_id: str) -> Path | None:
        """Path to the file to hand to llama-server, once fully downloaded.

        For a split GGUF this is the first shard — llama.cpp discovers the
        remaining shards itself from files alongside it, so only the first
        path is ever needed by a caller.

        Args:
            model_id (str): Caller-chosen model key.

        Returns:
            Path | None: The path, or ``None`` if not fully downloaded.
        """
        record = self.get_record(model_id)
        if record is None or not record.is_installed:
            return None
        primary = record.primary_file
        if primary is None:
            return None
        return self.__model_dir(model_id) / primary.filename

    def get_mmproj_path(self, model_id: str) -> Path | None:
        """Path to *model_id*'s mmproj companion file, once fully downloaded.

        Args:
            model_id (str): Caller-chosen model key.

        Returns:
            Path | None: The path, or ``None`` if no mmproj is attached, or
            it hasn't finished downloading.
        """
        record = self.get_record(model_id)
        if record is None:
            return None
        mmproj = record.mmproj_file
        if mmproj is None or not mmproj.is_complete:
            return None
        return self.__model_dir(model_id) / mmproj.filename

    # ------------------------------------------------------------------
    # Download / resume / pause / uninstall
    # ------------------------------------------------------------------

    def download_model(
        self,
        model_id: str,
        repo_id: str,
        filename: str,
        *,
        revision: str = "main",
        token: str | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ModelRecord:
        """Download a model's file(s), deducing and fetching every shard if split.

        Idempotent: if *model_id* is already fully downloaded for the same
        ``(repo_id, filename)``, returns immediately without re-downloading.
        If a previous call left it partially downloaded, continues from
        there — this *is* the interrupted-download-resume path as well as
        the first-download path, since both end up calling the shared
        transfer loop over whatever files aren't yet complete.

        Args:
            model_id (str): Caller-chosen key identifying this model.
            repo_id (str): HuggingFace repository ID.
            filename (str): One file's path within the repo. If it matches
                llama.cpp's split-GGUF naming convention
                (``<prefix>-NNNNN-of-MMMMM.gguf``), every sibling shard is
                deduced and downloaded too (see
                :func:`kodo.llms.local._hf.detect_shard_group`).
            revision (str): Git revision (branch/tag/commit).
            token (str | None): HF access token, for gated/private repos.
            progress_cb (ProgressCallback | None): Called with a
                :class:`DownloadProgress` after every chunk written to disk.

        Returns:
            ModelRecord: The resulting state, whether fully installed,
            paused (if :meth:`pause_download` was called concurrently), or
            partially downloaded.

        Raises:
            ShardResolutionError: The repo/revision/file doesn't exist, is
                gated without a valid *token*, or a split GGUF is missing an
                expected shard.
            DownloadError: A network, I/O, or size-mismatch failure.
        """
        available = list_repo_files(repo_id, revision=revision, token=token)
        shard_filenames = detect_shard_group(filename, available)

        def mutate(records: dict[str, ModelRecord]) -> None:
            record = records.get(model_id)
            if record is None or record.repo_id != repo_id or record.revision != revision:
                record = ModelRecord(
                    model_id=model_id,
                    repo_id=repo_id,
                    revision=revision,
                    commit_hash=None,
                    created_at=_now(),
                    updated_at=_now(),
                )
            existing = {
                f.filename: f for f in record.files if f.role in (FileRole.MAIN, FileRole.SHARD)
            }
            new_files: list[ModelFile] = []
            for index, name in enumerate(shard_filenames):
                role = FileRole.MAIN if index == 0 else FileRole.SHARD
                prior = existing.get(name)
                new_files.append(
                    prior
                    if prior is not None and prior.role == role
                    else ModelFile(filename=name, role=role, repo_id=repo_id, revision=revision)
                )
            mmproj = record.mmproj_file
            record.files = new_files + ([mmproj] if mmproj else [])
            record.updated_at = _now()
            records[model_id] = record

        self.__mutate_state(mutate)
        targets = [
            f
            for f in self.__require_record(model_id).files
            if f.role in (FileRole.MAIN, FileRole.SHARD) and not f.is_complete
        ]
        self.__run_transfer(model_id, targets, token=token, progress_cb=progress_cb)
        return self.__require_record(model_id)

    def resume_download(
        self,
        model_id: str,
        *,
        token: str | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> ModelRecord:
        """Resume every incomplete file of a previously-started download.

        Distinct entry point from :meth:`download_model` for callers that
        only have a ``model_id`` and don't want to re-specify ``repo_id``/
        ``filename`` — e.g. after a process restart, driven by
        :meth:`list_resumable`. Covers files left ``PAUSED`` (user-paused),
        ``FAILED`` (a previous network error), or stuck ``DOWNLOADING``
        (the process died mid-transfer) — all three are equally resumable
        since the real resume point is the ``.part`` file's size on disk.

        Args:
            model_id (str): A model previously passed to :meth:`download_model`.
            token (str | None): HF access token, for gated/private repos.
            progress_cb (ProgressCallback | None): Called with a
                :class:`DownloadProgress` after every chunk written to disk.

        Returns:
            ModelRecord: The resulting state.

        Raises:
            ModelNotFoundError: *model_id* was never downloaded.
            DownloadError: A network, I/O, or size-mismatch failure.
        """
        record = self.__require_record(model_id)
        pending = [f for f in record.files if not f.is_complete]
        self.__run_transfer(model_id, pending, token=token, progress_cb=progress_cb)
        return self.__require_record(model_id)

    def pause_download(self, model_id: str) -> None:
        """Signal an in-flight download of *model_id* to stop.

        A no-op if nothing is currently downloading for *model_id* — safe to
        call speculatively. The download stops between chunks (at most one
        chunk's worth of extra data), leaving the ``.part`` file in place for
        :meth:`resume_download` to continue later.

        Args:
            model_id (str): Caller-chosen model key.
        """
        with self.__lock:
            event = self.__cancel_events.get(model_id)
        if event is not None:
            event.set()

    def download_mmproj(
        self,
        model_id: str,
        repo_id: str,
        filename: str,
        *,
        revision: str = "main",
        token: str | None = None,
        progress_cb: ProgressCallback | None = None,
    ) -> Path:
        """Download a multimodal-projector companion file for an existing model.

        *model_id* must already have a download record (i.e.
        :meth:`download_model` must have been called for it at least once,
        though it need not have finished) — an mmproj file is always
        attached to a specific model, never downloaded standalone.

        Args:
            model_id (str): A model previously passed to :meth:`download_model`.
            repo_id (str): HuggingFace repository ID for the mmproj file —
                often the same repo as the model, but not necessarily.
            filename (str): The mmproj file's path within that repo.
            revision (str): Git revision (branch/tag/commit).
            token (str | None): HF access token, for gated/private repos.
            progress_cb (ProgressCallback | None): Called with a
                :class:`DownloadProgress` after every chunk written to disk.

        Returns:
            Path: Local path to the downloaded mmproj file.

        Raises:
            ModelNotFoundError: *model_id* was never downloaded.
            ShardResolutionError: The repo/revision/file doesn't exist or is
                gated without a valid *token*.
            DownloadError: A network, I/O, or size-mismatch failure, or the
                transfer was paused before completing.
        """

        def mutate(records: dict[str, ModelRecord]) -> None:
            record = records.get(model_id)
            if record is None:
                raise ModelNotFoundError(
                    f"Cannot attach an mmproj file to {model_id!r} — download the model first"
                )
            others = [f for f in record.files if f.role != FileRole.MMPROJ]
            existing = record.mmproj_file
            if (
                existing is not None
                and existing.filename == filename
                and existing.repo_id == repo_id
                and existing.is_complete
            ):
                record.files = others + [existing]
            else:
                record.files = others + [
                    ModelFile(
                        filename=filename, role=FileRole.MMPROJ, repo_id=repo_id, revision=revision
                    )
                ]
            record.updated_at = _now()

        self.__mutate_state(mutate)
        mmproj = self.__require_record(model_id).mmproj_file
        assert mmproj is not None  # just set by mutate() above
        if not mmproj.is_complete:
            self.__run_transfer(model_id, [mmproj], token=token, progress_cb=progress_cb)
        path = self.get_mmproj_path(model_id)
        if path is None:
            raise DownloadError(
                f"mmproj download for {model_id!r} did not complete (paused or failed)"
            )
        return path

    def uninstall(self, model_id: str) -> None:
        """Delete every file belonging to *model_id* and drop its state entry.

        A no-op if *model_id* is unknown. Best-effort pauses any in-flight
        download first so the transfer thread stops writing before its
        directory disappears out from under it.

        Args:
            model_id (str): Caller-chosen model key.
        """
        self.pause_download(model_id)
        with self.__lock:
            records = load_state(self.__state_path)
            if model_id in records:
                del records[model_id]
                save_state(self.__state_path, records)
            self.__cancel_events.pop(model_id, None)
        shutil.rmtree(self.__model_dir(model_id), ignore_errors=True)

    # ------------------------------------------------------------------
    # Internal: the shared transfer loop
    # ------------------------------------------------------------------

    def __run_transfer(
        self,
        model_id: str,
        targets: list[ModelFile],
        *,
        token: str | None,
        progress_cb: ProgressCallback | None,
    ) -> None:
        if not targets:
            return

        cancel_event = threading.Event()
        with self.__lock:
            self.__cancel_events[model_id] = cancel_event
        try:
            running_bytes = {f.filename: f.downloaded_bytes for f in targets}
            running_sizes = {f.filename: f.size for f in targets}
            file_count = len(targets)

            def overall_downloaded() -> int:
                return sum(running_bytes.values())

            def overall_total() -> int | None:
                if any(size is None for size in running_sizes.values()):
                    return None
                return sum(size for size in running_sizes.values() if size is not None)

            for index, file in enumerate(targets, start=1):
                self.__set_file_status(model_id, file.filename, status=FileStatus.DOWNLOADING)
                try:
                    resolved = resolve_file(
                        file.repo_id, file.filename, revision=file.revision, token=token
                    )
                except LocalModelError as exc:
                    self.__set_file_status(
                        model_id, file.filename, status=FileStatus.FAILED, error=str(exc)
                    )
                    raise
                running_sizes[file.filename] = resolved.size

                part_path = self.__model_dir(model_id) / f"{file.filename}{_PART_SUFFIX}"
                final_path = self.__model_dir(model_id) / file.filename

                last_flush = [0.0]

                def on_bytes(
                    n: int,
                    _filename: str = file.filename,
                    _index: int = index,
                    _last_flush: list[float] = last_flush,
                ) -> None:
                    running_bytes[_filename] = n
                    now = time.monotonic()
                    if now - _last_flush[0] >= _FLUSH_INTERVAL_SECONDS:
                        _last_flush[0] = now
                        # Unconditional, regardless of progress_cb — this is what lets
                        # kodo-vsix follow a live download purely by polling
                        # manager-state.json (doc/LOCAL_MODEL_MANAGER.md §11), no WS
                        # push required.
                        self.__set_file_status(
                            model_id,
                            _filename,
                            status=FileStatus.DOWNLOADING,
                            downloaded_bytes=n,
                            size=running_sizes[_filename],
                        )
                    if progress_cb is not None:
                        progress_cb(
                            DownloadProgress(
                                model_id=model_id,
                                filename=_filename,
                                file_index=_index,
                                file_count=file_count,
                                bytes_downloaded=n,
                                bytes_total=running_sizes[_filename],
                                overall_bytes_downloaded=overall_downloaded(),
                                overall_bytes_total=overall_total(),
                                message=f"Downloading {_filename} ({_index}/{file_count})",
                            )
                        )

                try:
                    downloaded = download_to_part_file(
                        resolved.url,
                        part_path,
                        headers=resolved.headers,
                        expected_size=resolved.size,
                        cancel_event=cancel_event,
                        on_bytes=on_bytes,
                    )
                except DownloadPausedError:
                    partial = (
                        part_path.stat().st_size
                        if part_path.exists()
                        else running_bytes[file.filename]
                    )
                    self.__set_file_status(
                        model_id,
                        file.filename,
                        status=FileStatus.PAUSED,
                        downloaded_bytes=partial,
                        size=resolved.size,
                        etag=resolved.etag,
                    )
                    return
                except DownloadError as exc:
                    partial = part_path.stat().st_size if part_path.exists() else 0
                    self.__set_file_status(
                        model_id,
                        file.filename,
                        status=FileStatus.FAILED,
                        error=str(exc),
                        downloaded_bytes=partial,
                        size=resolved.size,
                        etag=resolved.etag,
                    )
                    raise

                part_path.replace(final_path)
                running_bytes[file.filename] = downloaded
                self.__set_file_status(
                    model_id,
                    file.filename,
                    status=FileStatus.COMPLETED,
                    downloaded_bytes=downloaded,
                    size=resolved.size,
                    etag=resolved.etag,
                    error="",
                )
        finally:
            with self.__lock:
                self.__cancel_events.pop(model_id, None)
