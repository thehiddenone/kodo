"""Resumable, pausable, progress-reporting chunked file downloader.

Deliberately bypasses :func:`huggingface_hub.hf_hub_download` for the actual
byte transfer: as of the ``huggingface_hub`` version this project pins,
``hf_hub_download`` writes every download to a process-unique temp file that
is unconditionally deleted on *any* failure or interruption (see
``file_download.py``'s ``_download_to_tmp_and_move`` — a deliberate change to
avoid shared-lock corruption on some network filesystems). That means calling
it again after an interruption restarts from zero — there is no library-level
resume left to lean on.

This module keeps its own ``<file>.part`` file across interruptions instead,
using a standard HTTP ``Range`` request to continue it — real pause/resume,
not just "retry from scratch". Built on :mod:`urllib.request` (stdlib, no new
dependency), matching the pattern already used by
``kodo.llms.llamacpp._installer``'s binary downloader.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from ._types import DownloadError, DownloadPausedError

__all__ = ["download_to_part_file"]

_CHUNK_SIZE = 65536
_USER_AGENT = "kodo-llm-manager/1.0 (github.com/thehiddenone/kodo)"


def download_to_part_file(
    url: str,
    part_path: Path,
    *,
    headers: dict[str, str] | None = None,
    expected_size: int | None = None,
    cancel_event: threading.Event | None = None,
    on_bytes: Callable[[int], None] | None = None,
    timeout: float = 30.0,
) -> int:
    """Stream *url* into *part_path*, resuming from any existing partial content.

    Args:
        url: The URL to GET.
        part_path: Destination for the (possibly partial) file. Any bytes
            already present are treated as a previous attempt's progress and
            extended via ``Range: bytes=<size>-``; never truncated except when
            the server doesn't honor the Range request (see below).
        headers: Extra request headers (e.g. ``authorization``).
        expected_size: Total size in bytes, if known — used to skip an
            already-complete file and to verify the result.
        cancel_event: Checked between chunks. When set, the transfer stops
            and raises :class:`DownloadPausedError`, leaving *part_path*
            intact so a later call can pick up where it left off.
        on_bytes: Called with the cumulative byte count after every chunk
            written to disk.
        timeout: Socket timeout in seconds.

    Returns:
        int: Total bytes now in *part_path*.

    Raises:
        DownloadPausedError: *cancel_event* was set mid-transfer.
        DownloadError: A network error, a server that reports a size that
            doesn't match *expected_size*, or a local I/O error.
    """
    part_path.parent.mkdir(parents=True, exist_ok=True)
    resume_from = part_path.stat().st_size if part_path.exists() else 0
    if expected_size is not None and resume_from >= expected_size:
        return resume_from

    req_headers = dict(headers or {})
    req_headers["User-Agent"] = _USER_AGENT
    if resume_from:
        req_headers["Range"] = f"bytes={resume_from}-"

    request = urllib.request.Request(url, headers=req_headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if resume_from and exc.code == 416:
            # Server says our range is beyond the resource — we already have it all.
            return resume_from
        raise DownloadError(f"HTTP {exc.code} downloading {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"Network error downloading {url}: {exc.reason}") from exc

    with response:
        if resume_from and response.status == 200:
            # Server ignored our Range header and is sending the full body from
            # byte 0 — restart the part file to avoid corrupting it with a
            # second copy appended to the first.
            resume_from = 0
            mode = "wb"
        else:
            mode = "ab" if resume_from else "wb"

        downloaded = resume_from
        try:
            with part_path.open(mode) as fh:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadPausedError(f"Paused at {downloaded} bytes")
                    chunk = response.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if on_bytes is not None:
                        on_bytes(downloaded)
        except DownloadPausedError:
            raise
        except OSError as exc:
            raise DownloadError(f"Local I/O error writing {part_path}: {exc}") from exc
        except TimeoutError as exc:
            raise DownloadError(f"Timed out downloading {url}: {exc}") from exc

    if expected_size is not None and downloaded != expected_size:
        raise DownloadError(
            f"Size mismatch downloading {url}: expected {expected_size}, got {downloaded}"
        )
    return downloaded
