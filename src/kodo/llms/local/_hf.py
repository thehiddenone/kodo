"""HuggingFace Hub metadata resolution: URLs, sizes, and GGUF shard groups.

Only *metadata* goes through :mod:`huggingface_hub` here — the actual byte
transfer is done by :mod:`kodo.llms.local._http`'s own resumable downloader
(see that module's docstring for why). This module's job is to turn a
``(repo_id, filename)`` pair into a :class:`ResolvedFile` — a plain URL plus
the headers that are safe to send to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import huggingface_hub
from huggingface_hub.errors import (
    EntryNotFoundError,
    GatedRepoError,
    HfHubHTTPError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)

from ._types import ShardResolutionError

__all__ = ["ResolvedFile", "detect_shard_group", "list_repo_files", "resolve_file"]

# llama.cpp's split-GGUF naming convention, e.g. "model-00001-of-00003.gguf".
_SHARD_RE = re.compile(
    r"^(?P<prefix>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE
)


@dataclass(frozen=True)
class ResolvedFile:
    """Where and how to download one file, resolved from HF metadata.

    Attributes:
        filename: The file's path within the repo, unchanged from the input.
        url: The concrete URL to issue the (possibly Range-restricted) GET
            against — may be a huggingface.co URL or a signed CDN URL.
        headers: Extra request headers safe to send to *url*. Contains an
            ``authorization`` entry only when *url* is still on the HF Hub
            host itself; a signed CDN redirect never gets the HF token (it
            doesn't need it, and the token shouldn't leak to a third party).
        etag: HF ETag for the file, if the server reported one.
        size: Expected size in bytes, if the server reported one.
        commit_hash: The concrete commit the requested revision resolved to.
    """

    filename: str
    url: str
    headers: dict[str, str]
    etag: str | None
    size: int | None
    commit_hash: str | None


def resolve_file(
    repo_id: str,
    filename: str,
    *,
    revision: str = "main",
    token: str | None = None,
) -> ResolvedFile:
    """Resolve a single repo file to a downloadable URL plus metadata.

    Args:
        repo_id: HuggingFace repository ID, e.g. ``"unsloth/gpt-oss-20b-GGUF"``.
        filename: Path of the file within the repo.
        revision: Git revision (branch/tag/commit). Defaults to ``"main"``.
        token: HF access token, for gated/private repos. ``None`` for
            anonymous access.

    Returns:
        ResolvedFile: URL, headers, and metadata for the file.

    Raises:
        ShardResolutionError: If the repo, revision, or file doesn't exist,
            or the repo is gated/private and *token* doesn't grant access.
    """
    hub_url = huggingface_hub.hf_hub_url(repo_id, filename, revision=revision)
    try:
        meta = huggingface_hub.get_hf_file_metadata(hub_url, token=token)
    except GatedRepoError as exc:
        raise ShardResolutionError(
            f"{repo_id!r} is a gated repository — provide an HF access token with access to it"
        ) from exc
    except RepositoryNotFoundError as exc:
        raise ShardResolutionError(f"HuggingFace repository not found: {repo_id!r}") from exc
    except RevisionNotFoundError as exc:
        raise ShardResolutionError(f"Revision {revision!r} not found in {repo_id!r}") from exc
    except EntryNotFoundError as exc:
        raise ShardResolutionError(f"File not found in {repo_id!r}: {filename!r}") from exc
    except HfHubHTTPError as exc:
        raise ShardResolutionError(f"Could not resolve {filename!r} in {repo_id!r}: {exc}") from exc

    if meta.etag is None:
        raise ShardResolutionError(
            f"{repo_id!r}/{filename!r} has no ETag — cannot reliably download it"
        )

    headers: dict[str, str] = {}
    location = meta.location
    if token and urlparse(hub_url).netloc == urlparse(location).netloc:
        headers["authorization"] = f"Bearer {token}"

    return ResolvedFile(
        filename=filename,
        url=location,
        headers=headers,
        etag=meta.etag,
        size=meta.size,
        commit_hash=meta.commit_hash,
    )


def list_repo_files(repo_id: str, *, revision: str = "main", token: str | None = None) -> list[str]:
    """Return every file path in a repo at *revision*.

    Args:
        repo_id: HuggingFace repository ID.
        revision: Git revision (branch/tag/commit).
        token: HF access token, for gated/private repos.

    Returns:
        list[str]: Repo-relative file paths (``ModelInfo.siblings[*].rfilename``).

    Raises:
        ShardResolutionError: If the repo/revision can't be listed.
    """
    try:
        info = huggingface_hub.model_info(repo_id, revision=revision, token=token)
    except GatedRepoError as exc:
        raise ShardResolutionError(
            f"{repo_id!r} is a gated repository — provide an HF access token with access to it"
        ) from exc
    except (RepositoryNotFoundError, RevisionNotFoundError) as exc:
        raise ShardResolutionError(f"Could not find {repo_id!r} at revision {revision!r}") from exc
    except HfHubHTTPError as exc:
        raise ShardResolutionError(f"Could not list files for {repo_id!r}: {exc}") from exc
    return [s.rfilename for s in (info.siblings or [])]


def detect_shard_group(filename: str, available_files: list[str]) -> list[str]:
    """Deduce every sibling shard of a split GGUF, given just one of its files.

    Recognizes llama.cpp's split-GGUF convention: ``<prefix>-NNNNN-of-MMMMM.gguf``.
    If *filename* doesn't match that pattern, it is assumed to be a
    self-contained single-file model and returned unchanged.

    Args:
        filename: One filename from the set (any shard index — not
            necessarily the first).
        available_files: All files present in the repo (from
            :func:`list_repo_files`), used to confirm every deduced sibling
            actually exists before committing to downloading the set.

    Returns:
        list[str]: Filenames in shard order (index 1 first), or ``[filename]``
        if it isn't part of a split GGUF.

    Raises:
        ShardResolutionError: If *filename* matches the split-GGUF pattern
            but one or more deduced sibling files are missing from
            *available_files*.
    """
    match = _SHARD_RE.match(filename)
    if match is None:
        return [filename]

    prefix = match.group("prefix")
    total_str = match.group("total")
    total = int(total_str)
    available = set(available_files)

    shards = [f"{prefix}-{i:0{len(total_str)}d}-of-{total_str}.gguf" for i in range(1, total + 1)]
    missing = [s for s in shards if s not in available]
    if missing:
        raise ShardResolutionError(
            f"{filename!r} looks like part of a {total}-way split GGUF, but "
            f"{len(missing)} sibling file(s) are missing from the repo (e.g. {missing[0]!r})"
        )
    return shards
