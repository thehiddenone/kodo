"""Ensuring a validation run's named local LLMs are installed before use.

:class:`~kodo.validator._scenario.Scenario` mandates two named local-model
registry entries: ``llm_under_test`` (pinned as the run's active model,
driving the session) and ``validation_llm`` (a fixed model reserved for the
not-yet-built Phase 2 evaluator — see ``doc/VALIDATOR.md`` §9). Both must be
installed before a run proceeds.

This module drives the same ``local_llm.install`` WS command the Local
Inference Settings webview uses (``doc/WS_PROTOCOL.md`` §7.6), then polls
``manager-state.json`` on disk for completion — the same disk-polled pattern
``doc/LOCAL_MODEL_MANAGER.md`` §11 documents for kodo-vsix. ``local_llm.
install`` never sends a correlated ``response`` (only ``event`` frames), and
disk-polling keeps this package from importing engine-side ``kodo.llms`` code
(``doc/VALIDATOR.md``'s "never import engine internals" rule) — everything
here is plain JSON read off the wire (``hello.ack``) or off disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from kodo.transport import MSG_LOCAL_LLM_INSTALL

from ._client import ValidatorClient

__all__ = ["LocalModelUnavailableError", "ensure_local_llms_installed", "missing_local_llms"]

_log = logging.getLogger(__name__)

_POLL_SECONDS = 1.0
_DOWNLOADABLE_KINDS = frozenset({"hardcoded_hf", "custom_hf"})
_MAIN_FILE_ROLES = frozenset({"main", "shard"})


class LocalModelUnavailableError(RuntimeError):
    """A required local LLM is unknown, not auto-downloadable, or failed to install."""


async def ensure_local_llms_installed(
    client: ValidatorClient,
    kodo_dir: Path,
    local_registry: Sequence[dict[str, object]],
    names: Sequence[str],
    *,
    poll_timeout: float = 1800.0,
) -> None:
    """Make sure every name in *names* is installed, downloading if needed.

    Args:
        client (ValidatorClient): The connected client, post-``hello``.
        kodo_dir (Path): The run's isolated ``.kodo`` (locates ``manager-state.json``).
        local_registry (Sequence[dict[str, object]]): The ``local_registry`` list
            from ``hello.ack`` (or a later ``local_llm.registry_state``).
        names (Sequence[str]): Local registry entry names that must end up installed.
        poll_timeout (float): Seconds to wait for each missing download to finish.

    Raises:
        LocalModelUnavailableError: A name is absent from the registry, its
            entry kind cannot be auto-downloaded, its download fails, or
            installation does not finish within *poll_timeout*.
    """
    by_name = {str(entry.get("name")): entry for entry in local_registry}
    pending: list[str] = []
    for name in names:
        entry = by_name.get(name)
        if entry is None:
            raise LocalModelUnavailableError(
                f"Unknown local model {name!r} — not present in the local registry"
            )
        if bool(entry.get("installed")):
            continue
        if str(entry.get("kind")) not in _DOWNLOADABLE_KINDS:
            raise LocalModelUnavailableError(
                f"Local model {name!r} is not installed and its kind "
                f"({entry.get('kind')!r}) cannot be auto-downloaded"
            )
        pending.append(name)

    if not pending:
        return

    for name in pending:
        _log.info("Requesting install of missing local LLM: %s", name)
        await client.send(MSG_LOCAL_LLM_INSTALL, name=name)

    models_dir = _models_dir(kodo_dir)
    deadline = time.monotonic() + poll_timeout
    remaining = set(pending)
    while remaining:
        await asyncio.sleep(_POLL_SECONDS)
        state = _read_manager_state(models_dir)
        for name in list(remaining):
            record = state.get(name)
            if record is None:
                continue
            status = _record_status(record)
            if status == "installed":
                _log.info("Local LLM installed: %s", name)
                remaining.discard(name)
            elif status == "failed":
                raise LocalModelUnavailableError(
                    f"Download failed for local model {name!r}: {_record_error(record)}"
                )
        if remaining and time.monotonic() >= deadline:
            raise LocalModelUnavailableError(
                f"Timed out waiting for local model(s) to install: {sorted(remaining)}"
            )


def missing_local_llms(kodo_dir: Path, names: Sequence[str]) -> list[str]:
    """Names among *names* not installed on disk under *kodo_dir* (no server).

    A pure disk pre-flight — no running server, no WS, no download. It mirrors
    :func:`_record_status`'s notion of "installed" (every main/shard file
    ``completed`` in ``manager-state.json``), so it agrees with what the
    per-run installer would report, and lets a batch runner fail fast before
    spinning up any scenario rather than downloading multi-GB models
    implicitly.

    Args:
        kodo_dir (Path): The ``.kodo`` home to check (e.g. the template home).
        names (Sequence[str]): Local registry entry names to require installed.

    Returns:
        list[str]: The subset of *names* that are not installed, in input
            order and de-duplicated.
    """
    state = _read_manager_state(_models_dir(kodo_dir))
    missing: list[str] = []
    for name in names:
        if name in missing:
            continue
        record = state.get(name)
        if not isinstance(record, dict) or _record_status(record) != "installed":
            missing.append(name)
    return missing


def _models_dir(kodo_dir: Path) -> Path:
    """Resolve the GGUF models directory the same way the server would.

    Mirrors ``kodo.llms.llamacpp._manager._models_dir`` (``llm_models_dir``
    setting, else ``kodo_dir/llama.cpp/models``) without importing it, since
    this is plain settings-JSON/path logic, not engine behavior.

    Args:
        kodo_dir (Path): The run's isolated ``.kodo``.

    Returns:
        Path: Directory expected to hold ``manager-state.json``.
    """
    settings_path = kodo_dir / "etc" / "settings.json"
    if settings_path.is_file():
        try:
            parsed = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            parsed = None
        if isinstance(parsed, dict) and "llm_models_dir" in parsed:
            return Path(str(parsed["llm_models_dir"]))
    return kodo_dir / "llama.cpp" / "models"


def _read_manager_state(models_dir: Path) -> dict[str, dict[str, object]]:
    """Read ``manager-state.json``, tolerating absence or a mid-write read.

    Args:
        models_dir (Path): Directory containing ``manager-state.json``.

    Returns:
        dict[str, dict[str, object]]: Parsed state, or empty on any read issue
            (the file is rewritten atomically, but a poll can still land
            between the old file vanishing and appearing under load).
    """
    path = models_dir / "manager-state.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _record_status(record: dict[str, object]) -> str:
    """Classify one ``manager-state.json`` record as installed/failed/pending.

    Args:
        record (dict[str, object]): One ``ModelRecord`` JSON entry.

    Returns:
        str: ``"installed"`` (every main/shard file completed), ``"failed"``
            (any main/shard file failed), or ``"pending"`` otherwise.
    """
    files = record.get("files")
    if not isinstance(files, list):
        return "pending"
    mains = [f for f in files if isinstance(f, dict) and f.get("role") in _MAIN_FILE_ROLES]
    if not mains:
        return "pending"
    if any(f.get("status") == "failed" for f in mains):
        return "failed"
    if all(f.get("status") == "completed" for f in mains):
        return "installed"
    return "pending"


def _record_error(record: dict[str, object]) -> str:
    """Extract the first failed main/shard file's error message, if any.

    Args:
        record (dict[str, object]): One ``ModelRecord`` JSON entry.

    Returns:
        str: The error message, or ``""`` if none is recorded.
    """
    files = record.get("files")
    if isinstance(files, list):
        for f in files:
            if not isinstance(f, dict) or f.get("role") not in _MAIN_FILE_ROLES:
                continue
            if f.get("status") == "failed":
                return str(f.get("error", ""))
    return ""
