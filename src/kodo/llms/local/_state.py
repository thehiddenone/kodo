"""JSON (de)serialization for :class:`~kodo.llms.local._types.ModelRecord`.

The state file is the manager's single source of truth for what it has ever
started downloading — including partially-downloaded and failed files, which
is what makes :meth:`LocalModelManager.resume_download` and
:meth:`LocalModelManager.list_models` possible across process restarts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import cast

from ._types import FileRole, FileStatus, ModelFile, ModelRecord

__all__ = ["load_state", "save_state"]

_log = logging.getLogger(__name__)


def _file_to_json(file: ModelFile) -> dict[str, object]:
    return {
        "filename": file.filename,
        "role": file.role.value,
        "repo_id": file.repo_id,
        "revision": file.revision,
        "size": file.size,
        "etag": file.etag,
        "downloaded_bytes": file.downloaded_bytes,
        "status": file.status.value,
        "error": file.error,
    }


def _file_from_json(raw: dict[str, object]) -> ModelFile:
    size = raw.get("size")
    etag = raw.get("etag")
    return ModelFile(
        filename=str(raw["filename"]),
        role=FileRole(str(raw["role"])),
        repo_id=str(raw["repo_id"]),
        revision=str(raw.get("revision", "main")),
        size=int(cast(int, size)) if size is not None else None,
        etag=str(etag) if etag is not None else None,
        downloaded_bytes=int(cast(int, raw.get("downloaded_bytes", 0)) or 0),
        status=FileStatus(str(raw.get("status", "pending"))),
        error=str(raw.get("error", "")),
    )


def _record_to_json(record: ModelRecord) -> dict[str, object]:
    return {
        "model_id": record.model_id,
        "repo_id": record.repo_id,
        "revision": record.revision,
        "commit_hash": record.commit_hash,
        "files": [_file_to_json(f) for f in record.files],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _record_from_json(model_id: str, raw: dict[str, object]) -> ModelRecord:
    commit_hash = raw.get("commit_hash")
    files = cast(list[object], raw.get("files", []))
    return ModelRecord(
        model_id=model_id,
        repo_id=str(raw["repo_id"]),
        revision=str(raw.get("revision", "main")),
        commit_hash=str(commit_hash) if commit_hash is not None else None,
        files=[_file_from_json(cast(dict[str, object], f)) for f in files if isinstance(f, dict)],
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
    )


def load_state(path: Path) -> dict[str, ModelRecord]:
    """Load every :class:`ModelRecord` from *path*.

    Returns an empty dict — never raises — if the file is missing, corrupt,
    or contains an entry that doesn't parse; a corrupt individual entry is
    dropped with a warning rather than discarding the whole file.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("Could not read %s: %s — starting with empty state", path, exc)
        return {}
    if not isinstance(raw, dict):
        _log.warning("%s does not contain a JSON object — starting with empty state", path)
        return {}

    records: dict[str, ModelRecord] = {}
    for model_id, data in raw.items():
        if not isinstance(data, dict):
            continue
        try:
            records[model_id] = _record_from_json(model_id, data)
        except (KeyError, ValueError, TypeError) as exc:
            _log.warning("Skipping corrupt state entry %r in %s: %s", model_id, path, exc)
    return records


def save_state(path: Path, records: dict[str, ModelRecord]) -> None:
    """Persist *records* to *path*, atomically (write-tmp-then-replace).

    Flushes and ``fsync``s the temp file before the replace — kodo-vsix polls
    this file directly off disk (see doc/LOCAL_MODEL_MANAGER.md §11) rather
    than over a WS push, so a reader racing the write should see either the
    old or the fully-written new content, never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {model_id: _record_to_json(record) for model_id, record in records.items()}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(path)
