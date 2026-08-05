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
    _all_active_flavors,
    _all_custom_flavors,
    _load_external,
    _load_raw,
    _save_external,
    _save_raw,
    _write_custom_flavors,
)
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
        merged[entry.name] = entry
    return merged


def add_local_entry(kodo_dir: Path, entry: LocalLLMEntry) -> None:
    """Add a custom entry to the external collection.

    Forces ``entry.flavors`` to ``()`` regardless of what the caller passed
    in — a custom entry's dataclass field default would otherwise silently
    attach the built-in ``"default"`` :class:`~kodo.llms.local_registry.LlamaFlavor`
    (meant for ``hardcoded_hf`` entries that don't override it), which would
    then shadow (and permanently hide) any *custom* flavor later added under
    the same ``"default"`` id — see
    :func:`~kodo.llms.local_registry._flavors.get_flavors`'s predefined-wins
    collision rule. This is the single enforcement point for that
    invariant; every ``local_llm.add_*`` handler in ``kodo/server/_app.py``
    relies on it rather than repeating ``flavors=()`` itself.

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
    if entry.flavors:
        entry = replace(entry, flavors=())
    external, override = _load_external(kodo_dir)
    external.append(entry)
    _save_external(kodo_dir, external, override)


def remove_local_entry(kodo_dir: Path, name: str) -> None:
    """Remove a custom entry from the external collection.

    Does not touch any downloaded GGUF file on disk — callers that want to
    free disk space should uninstall first via
    :func:`kodo.llms.llamacpp.get_local_model_manager`'s ``uninstall`` method
    before removing. Also drops any custom flavors and active-flavor
    selection stored for *name* — they would otherwise be permanently
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
    all_flavors = _all_custom_flavors(data)
    active = _all_active_flavors(data)
    changed = False
    if all_flavors.pop(name, None) is not None:
        _write_custom_flavors(data, all_flavors)
        changed = True
    if active.pop(name, None) is not None:
        data["active_flavors"] = active
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
