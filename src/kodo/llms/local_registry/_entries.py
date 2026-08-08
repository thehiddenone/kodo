"""Public registry API: the merged entry map, custom-entry CRUD, override path.

References the hardcoded catalog via ``_catalog._HARDCODED_LOCAL_MODELS``
(qualified module attribute access rather than ``from ._catalog import
_HARDCODED_LOCAL_MODELS``) specifically so tests can monkeypatch
``_catalog._HARDCODED_LOCAL_MODELS`` and have every function here observe
the patched value.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from . import _catalog
from ._io import (
    _CUSTOM_KINDS,
    _all_active_profiles,
    _all_knob_selections,
    _all_profiles,
    _load_external,
    _load_raw,
    _save_external,
    _save_raw,
    _write_knob_selections,
    _write_profiles,
)
from ._knobs_shared import BASE_LLAMA_ARGS, SHARED_KNOBS
from ._types import LocalLLMEntry

_log = logging.getLogger(__name__)

__all__ = [
    "add_local_entry",
    "clear_llama_server_override_path",
    "get_llama_server_override_path",
    "get_local_registry",
    "remove_local_entry",
    "set_llama_server_override_path",
]


def _with_custom_entry_knobs(entry: LocalLLMEntry) -> LocalLLMEntry:
    """Attach the shared knobs (and the shared base args) to a user-added entry.

    A ``custom_*`` entry has no knob declaration of its own — nothing in
    ``local-llm-registry.json`` stores knobs, deliberately, since knobs are
    code and a persisted copy would freeze whatever set existed when the entry
    was added. Instead every launchable custom entry gets
    :data:`~kodo.llms.local_registry._knobs_shared.SHARED_KNOBS` here, on load,
    so a kodo release that adds or changes a shared knob reaches existing
    custom entries with no file migration.

    Its ``base_llama_args`` — the args typed into the "Add local LLM" form —
    are layered *over*
    :data:`~kodo.llms.local_registry._knobs_shared.BASE_LLAMA_ARGS` so the
    entry still gets ``--jinja``/``--reasoning-format`` (without which tool
    calling does not work at all) unless the form deliberately overrode them.

    ``custom_server_url`` is left alone: kodo does not launch that process, so
    it has neither knobs nor base args.
    """
    if entry.kind == "custom_server_url":
        return entry
    return replace(
        entry,
        base_llama_args={**BASE_LLAMA_ARGS, **entry.base_llama_args},
        knobs=SHARED_KNOBS,
    )


def get_local_registry(kodo_dir: Path) -> dict[str, LocalLLMEntry]:
    """Return the merged local registry: hardcoded entries + the user's custom ones.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        dict[str, LocalLLMEntry]: Map of entry name to :class:`LocalLLMEntry`.
    """
    merged: dict[str, LocalLLMEntry] = {e.name: e for e in _catalog._HARDCODED_LOCAL_MODELS}
    external, _ = _load_external(kodo_dir)
    for entry in external:
        if entry.name in merged:
            _log.warning("Custom local LLM %r shadows a hardcoded entry — skipping", entry.name)
            continue
        merged[entry.name] = _with_custom_entry_knobs(entry)
    return merged


def add_local_entry(kodo_dir: Path, entry: LocalLLMEntry) -> None:
    """Add a custom entry to the external collection.

    Forces ``entry.knobs`` to ``()`` before persisting, regardless of what the
    caller passed in: knobs are code, never stored data. They are re-attached
    on every load by :func:`_with_custom_entry_knobs`, which is what lets a
    later kodo release change the shared knob set without rewriting anyone's
    ``local-llm-registry.json``. ``entry.base_llama_args`` — the args from the
    "Add local LLM" form — *is* persisted, and is the one launch-arg input a
    custom entry contributes.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to add; ``entry.kind`` must be one of the custom kinds.

    Raises:
        ValueError: If ``entry.kind`` is not a custom kind, or ``entry.name``
            already exists (hardcoded or custom).
    """
    if entry.kind not in _CUSTOM_KINDS:
        raise ValueError(f"Cannot add a local LLM entry of kind {entry.kind!r}")
    if entry.name in get_local_registry(kodo_dir):
        raise ValueError(f"A local LLM named {entry.name!r} already exists")
    if entry.knobs:
        entry = replace(entry, knobs=())
    external, override = _load_external(kodo_dir)
    external.append(entry)
    _save_external(kodo_dir, external, override)


def remove_local_entry(kodo_dir: Path, name: str) -> None:
    """Remove a custom entry from the external collection.

    Does not touch any downloaded GGUF file on disk — callers that want to
    free disk space should uninstall first via
    :func:`kodo.llms.llamacpp.get_local_model_manager`'s ``uninstall`` method
    before removing. Also drops every profile, active-profile selection and
    knob selection stored for *name* — they would otherwise be permanently
    orphaned (nothing else ever cleans them up, and a future custom entry
    added under the same name would silently inherit them).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        name: Entry name to remove.

    Raises:
        ValueError: If *name* is a hardcoded entry or does not exist.
    """
    if any(e.name == name for e in _catalog._HARDCODED_LOCAL_MODELS):
        raise ValueError(f"{name!r} is a built-in local LLM and cannot be removed")
    external, override = _load_external(kodo_dir)
    remaining = [e for e in external if e.name != name]
    if len(remaining) == len(external):
        raise ValueError(f"No custom local LLM named {name!r}")
    _save_external(kodo_dir, remaining, override)

    data = _load_raw(kodo_dir)
    all_profiles = _all_profiles(data)
    active = _all_active_profiles(data)
    selections = _all_knob_selections(data)
    changed = False
    if all_profiles.pop(name, None) is not None:
        _write_profiles(data, all_profiles)
        changed = True
    if active.pop(name, None) is not None:
        data["active_profiles"] = active
        changed = True
    if selections.pop(name, None) is not None:
        _write_knob_selections(data, selections)
        changed = True
    if changed:
        _save_raw(kodo_dir, data)


def get_llama_server_override_path(kodo_dir: Path) -> str | None:
    """Return the global llama-server binary override path, or ``None``."""
    _, override = _load_external(kodo_dir)
    return override


def set_llama_server_override_path(kodo_dir: Path, path: str) -> None:
    """Set the global llama-server binary override path.

    Kept entirely separate from the model list — this replaces the
    *executable* kodo launches (keeping its own CLI-argument-generation logic
    intact), it is not itself a model.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        path: Absolute path to a llama-server-compatible executable/script.

    Raises:
        ValueError: If *path* does not exist.
    """
    if not Path(path).is_file():
        raise ValueError(f"No such file: {path}")
    external, _ = _load_external(kodo_dir)
    _save_external(kodo_dir, external, path)


def clear_llama_server_override_path(kodo_dir: Path) -> None:
    """Clear the global llama-server binary override, reverting to the bundled binary."""
    external, _ = _load_external(kodo_dir)
    _save_external(kodo_dir, external, None)
