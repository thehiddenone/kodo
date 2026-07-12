"""Resumable, pausable, progress-reporting parallel-chunked file downloader.

Deliberately bypasses :func:`huggingface_hub.hf_hub_download` for the actual
byte transfer: as of the ``huggingface_hub`` version this project pins,
``hf_hub_download`` writes every download to a process-unique temp file that
is unconditionally deleted on *any* failure or interruption (see
``file_download.py``'s ``_download_to_tmp_and_move`` — a deliberate change to
avoid shared-lock corruption on some network filesystems). That means calling
it again after an interruption restarts from zero — there is no library-level
resume left to lean on.

This module keeps its own ``<file>.part`` file across interruptions instead,
using standard HTTP ``Range`` requests to continue it — real pause/resume,
not just "retry from scratch". Built on :mod:`aiohttp` (already a project
dependency) so a single file's transfer can run as several concurrent
partial-GET streams rather than one.

Stand-alone: everything needed to resume a chunked transfer — which byte
ranges are already on disk — is recorded in a ``<file>.part.chunks`` sidecar
next to the ``.part`` file itself. A caller only ever needs to pass a URL and
a destination path; it never has to track chunk state on its own behalf.

Architecture — a best-effort work-sharing pool of async streams:

* The file is split into fixed-size ranges (``_CHUNK_SIZE``, 4 MiB), plus one
  shorter final range for the remainder. Ranges already recorded as done in
  the sidecar are dropped from the work list up front.
* ``.part`` is opened once as a single random-access file handle, truncated
  (extended) to the full expected size before any worker starts, so every
  worker can seek-and-write its chunk's offset immediately regardless of
  which chunks land first.
* Up to ``parallelism`` worker coroutines run concurrently, each pulling the
  next range off a shared in-memory list and issuing its own ``Range`` GET.
  Everything runs on one event-loop thread, so popping the next range and
  writing a finished chunk's bytes (``seek`` + ``write`` on the shared handle,
  never overlapping across workers since each chunk owns a disjoint byte
  range) need no locking — the only interleaving points are ``await``s inside
  the HTTP call itself.
* A shared "chunks remaining" count (implicitly, the sidecar's completed set)
  reaching every chunk is what finishes the transfer; when the in-memory work
  list is empty, a worker simply returns.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import aiohttp

from ._types import DownloadError, DownloadPausedError

__all__ = ["download_to_part_file"]

_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB per parallel range request
_DEFAULT_PARALLELISM = 8
_USER_AGENT = "kodo-llm-manager/1.0 (github.com/thehiddenone/kodo)"
_SIDECAR_SUFFIX = ".chunks"
_SIDECAR_FLUSH_INTERVAL = 1.0


def _sidecar_path(part_path: Path) -> Path:
    return part_path.with_name(part_path.name + _SIDECAR_SUFFIX)


def _chunk_ranges(total_size: int, chunk_size: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total_size:
        end = min(start + chunk_size, total_size) - 1
        ranges.append((start, end))
        start += chunk_size
    return ranges


def _load_sidecar(sidecar_path: Path, chunk_size: int, total_size: int) -> set[int] | None:
    """Return the completed-chunk-index set recorded in *sidecar_path*.

    Returns ``None`` (rather than an empty set) if the sidecar doesn't exist
    or doesn't match this exact ``(chunk_size, total_size)`` — the caller
    distinguishes "no sidecar bookkeeping yet" (may still need the legacy
    contiguous-prefix migration below) from "sidecar says zero chunks done".
    """
    if not sidecar_path.is_file():
        return None
    try:
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("chunk_size") != chunk_size or raw.get("total_size") != total_size:
        return None
    completed = raw.get("completed")
    if not isinstance(completed, list):
        return None
    try:
        return {int(i) for i in completed}
    except (TypeError, ValueError):
        return None


def _save_sidecar(
    sidecar_path: Path, chunk_size: int, total_size: int, completed: set[int]
) -> None:
    payload = {
        "chunk_size": chunk_size,
        "total_size": total_size,
        "completed": sorted(completed),
    }
    tmp_path = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(sidecar_path)


async def download_to_part_file(
    url: str,
    part_path: Path,
    *,
    headers: dict[str, str] | None = None,
    expected_size: int | None = None,
    cancel_event: asyncio.Event | None = None,
    on_bytes: Callable[[int], None] | None = None,
    timeout: float = 30.0,
    parallelism: int | None = None,
) -> int:
    """Fetch *url* into *part_path*, resuming from any previously-completed chunks.

    Args:
        url: The URL to GET.
        part_path: Destination for the (possibly partial) file. When
            *expected_size* is known, this is a fixed-size random-access file
            written to out of order by concurrent chunk workers — its size on
            disk is *not* a valid progress indicator (see below); actual
            resume state lives in ``<part_path>.chunks``.
        headers: Extra request headers (e.g. ``authorization``).
        expected_size: Total size in bytes, if known. Required for parallel,
            chunked transfer — without it (some HF metadata responses omit a
            size) this falls back to a single-stream sequential GET, exactly
            like an unsharded transfer.
        cancel_event: Checked before each worker starts a new chunk. When
            set, in-flight chunks are allowed to finish (so no chunk is ever
            partially written) and no new ones start; the transfer then
            raises :class:`DownloadPausedError`, leaving *part_path* and its
            sidecar intact for a later call to continue from.
        on_bytes: Called with the cumulative byte count after every chunk
            written to disk (including once immediately with any bytes
            already recorded as done from a previous run).
        timeout: Per-request timeout in seconds.
        parallelism: Maximum number of concurrent range requests for one
            file. Ignored (effectively 1) once fewer than two chunks remain.
            Defaults to ``_DEFAULT_PARALLELISM``, read fresh on every call
            (not baked in as an argument default) so it can be overridden
            per-process without restarting anything, e.g. in tests.

    Returns:
        int: Total bytes now in *part_path*.

    Raises:
        DownloadPausedError: *cancel_event* was set mid-transfer.
        DownloadError: A network error, a server that doesn't honor Range
            requests, a size mismatch, or a local I/O error.
    """
    part_path.parent.mkdir(parents=True, exist_ok=True)
    req_headers = dict(headers or {})
    req_headers["User-Agent"] = _USER_AGENT

    if expected_size is None:
        return await _download_sequential(
            url, part_path, req_headers, cancel_event, on_bytes, timeout
        )
    return await _download_parallel(
        url,
        part_path,
        req_headers,
        expected_size,
        cancel_event,
        on_bytes,
        timeout,
        _DEFAULT_PARALLELISM if parallelism is None else parallelism,
    )


async def _download_sequential(
    url: str,
    part_path: Path,
    headers: dict[str, str],
    cancel_event: asyncio.Event | None,
    on_bytes: Callable[[int], None] | None,
    timeout: float,
) -> int:
    """Single-stream fallback for a file whose size isn't known upfront."""
    resume_from = part_path.stat().st_size if part_path.exists() else 0
    req_headers = dict(headers)
    if resume_from:
        req_headers["Range"] = f"bytes={resume_from}-"

    downloaded = resume_from
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response,
        ):
            if response.status == 416 and resume_from:
                return resume_from
            if response.status not in (200, 206):
                raise DownloadError(f"HTTP {response.status} downloading {url}")

            mode = "wb"
            if resume_from and response.status == 206:
                mode = "ab"
            elif resume_from and response.status == 200:
                # Server ignored our Range header — it's sending the full body
                # from byte 0, so restart the part file to avoid corrupting it
                # with a second copy appended to the first.
                downloaded = 0

            with part_path.open(mode) as fh:
                async for data in response.content.iter_chunked(_CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadPausedError(f"Paused at {downloaded} bytes")
                    fh.write(data)
                    downloaded += len(data)
                    if on_bytes is not None:
                        on_bytes(downloaded)
    except DownloadPausedError:
        raise
    except aiohttp.ClientError as exc:
        raise DownloadError(f"Network error downloading {url}: {exc}") from exc
    except TimeoutError as exc:
        raise DownloadError(f"Timed out downloading {url}: {exc}") from exc
    except OSError as exc:
        raise DownloadError(f"Local I/O error writing {part_path}: {exc}") from exc
    return downloaded


async def _download_parallel(
    url: str,
    part_path: Path,
    headers: dict[str, str],
    total_size: int,
    cancel_event: asyncio.Event | None,
    on_bytes: Callable[[int], None] | None,
    timeout: float,
    parallelism: int,
) -> int:
    sidecar_path = _sidecar_path(part_path)

    if total_size == 0:
        part_path.open("wb").close()
        sidecar_path.unlink(missing_ok=True)
        return 0

    existing_size = part_path.stat().st_size if part_path.exists() else 0
    if not sidecar_path.is_file() and existing_size >= total_size:
        # No chunk bookkeeping and the file is already the right size: either a
        # previously fully-completed transfer whose sidecar was already cleaned
        # up, or a pre-upgrade file from the old single-stream downloader.
        return total_size

    chunk_ranges = _chunk_ranges(total_size, _CHUNK_SIZE)
    completed = _load_sidecar(sidecar_path, _CHUNK_SIZE, total_size)
    if completed is None:
        # No (usable) sidecar. A pre-existing `.part` file can only be a
        # contiguous prefix from a previous single-stream download (or an
        # earlier, incompatible chunk layout) — whole chunks fully inside that
        # prefix are safe to trust; a trailing partial chunk is simply
        # refetched in full.
        completed = set(range(existing_size // _CHUNK_SIZE))

    # Persist the sidecar *before* truncating the part file: once both exist,
    # the sidecar (never the file's size) is the sole authority on progress —
    # truncating first would make an interrupted, all-empty file
    # indistinguishable on-disk from a fully-downloaded one.
    _save_sidecar(sidecar_path, _CHUNK_SIZE, total_size, completed)
    with part_path.open("r+b" if part_path.exists() else "wb") as fh:
        fh.truncate(total_size)

    downloaded = sum(
        end - start + 1 for i, (start, end) in enumerate(chunk_ranges) if i in completed
    )
    remaining = deque(i for i in range(len(chunk_ranges)) if i not in completed)
    if on_bytes is not None:
        on_bytes(downloaded)
    if not remaining:
        sidecar_path.unlink(missing_ok=True)
        return total_size

    fh = part_path.open("r+b")
    last_flush = 0.0
    paused = False

    def flush_sidecar(*, force: bool = False) -> None:
        nonlocal last_flush
        now = time.monotonic()
        if not force and now - last_flush < _SIDECAR_FLUSH_INTERVAL:
            return
        last_flush = now
        _save_sidecar(sidecar_path, _CHUNK_SIZE, total_size, completed)

    async def worker(session: aiohttp.ClientSession) -> None:
        nonlocal downloaded, paused
        while remaining:
            if cancel_event is not None and cancel_event.is_set():
                paused = True
                return
            index = remaining.popleft()
            start, end = chunk_ranges[index]
            chunk_headers = dict(headers)
            chunk_headers["Range"] = f"bytes={start}-{end}"
            async with session.get(
                url, headers=chunk_headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                whole_file = len(chunk_ranges) == 1
                if response.status not in (206, 200) or (response.status == 200 and not whole_file):
                    raise DownloadError(
                        f"HTTP {response.status} downloading {url} range {start}-{end} — "
                        "server may not support parallel Range requests"
                    )
                data = await response.read()
            if len(data) != (end - start + 1):
                raise DownloadError(
                    f"Short read downloading {url} range {start}-{end}: "
                    f"expected {end - start + 1} bytes, got {len(data)}"
                )
            fh.seek(start)
            fh.write(data)
            completed.add(index)
            downloaded += len(data)
            if on_bytes is not None:
                on_bytes(downloaded)
            flush_sidecar()

    try:
        try:
            async with aiohttp.ClientSession() as session, asyncio.TaskGroup() as tg:
                for _ in range(min(parallelism, len(remaining))):
                    tg.create_task(worker(session))
        except* DownloadError as eg:
            raise eg.exceptions[0] from None
        except* aiohttp.ClientError as eg:
            raise DownloadError(f"Network error downloading {url}: {eg.exceptions[0]}") from None
        except* TimeoutError as eg:
            raise DownloadError(f"Timed out downloading {url}: {eg.exceptions[0]}") from None
        except* OSError as eg:
            raise DownloadError(
                f"Local I/O error writing {part_path}: {eg.exceptions[0]}"
            ) from None
    finally:
        fh.close()

    if paused:
        flush_sidecar(force=True)
        raise DownloadPausedError(f"Paused at {downloaded} bytes")

    sidecar_path.unlink(missing_ok=True)
    if downloaded != total_size:
        raise DownloadError(
            f"Size mismatch downloading {url}: expected {total_size}, got {downloaded}"
        )
    return downloaded
