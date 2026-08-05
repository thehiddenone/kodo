"""``~/.kodo/etc/local-llm-registry.json`` file I/O and JSON (de)serialization.

The external collection (``custom_*`` entries) plus the global llama-server
binary override path, and the two sibling ``flavors``/``active_flavors``
top-level keys (see :mod:`kodo.llms.local_registry._flavors`) all live in
this one file, owned (read + written) entirely by this package — the
kodo-vsix extension only ever reads it indirectly, via the WS protocol (see
doc/LLM_REGISTRY.md).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import cast

from ._types import LlamaFlavor, LlamaFlavorPlatform, LocalLLMEntry

__all__ = [
    "parse_llama_args",
    "parse_llama_args_text",
]

_log = logging.getLogger(__name__)

_REGISTRY_RELATIVE_PATH = ("etc", "local-llm-registry.json")

#: Every ``LocalLLMEntry.kind`` a user (rather than kodo itself) can add.
_CUSTOM_KINDS = frozenset({"custom_hf", "custom_file", "custom_server_url"})


def _registry_file(kodo_dir: Path) -> Path:
    return kodo_dir.joinpath(*_REGISTRY_RELATIVE_PATH)


def parse_llama_args(raw: object) -> dict[str, str]:
    """Coerce a WS-payload/JSON value into the ``llama_args`` shape.

    Anything that isn't a ``dict`` (missing field, wrong type from a
    malformed request) is treated as "no extra args" rather than raising —
    callers are parsing untrusted request payloads.
    """
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def parse_llama_args_text(raw: object) -> dict[str, str]:
    """Parse the "manage flavors" modal's raw multi-line text box into ``llama_args``.

    One flag per line, e.g. ``--ctx-size 1048576``. Each non-blank line is
    split on the first run of whitespace into ``(flag, value)``; a line with
    no value (a bare flag) gets an empty-string value, which
    :class:`~kodo.llms.llamacpp.LlamaServerConfig`'s command builder then
    emits without a following empty argument. Lines that don't start with
    ``-`` are silently skipped rather than rejected outright — this is a
    convenience parser for pasted llama.cpp command lines, not a strict
    format; the kodo-vsix modal does its own live validation before sending.

    Args:
        raw: The WS payload value, expected to be a ``str`` (anything else —
            missing field, wrong type — is treated as empty text).

    Returns:
        dict[str, str]: The parsed ``{flag: value}`` mapping.
    """
    if not isinstance(raw, str):
        return {}
    args: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("-"):
            continue
        parts = line.split(None, 1)
        args[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return args


def _entry_from_json(raw: dict[str, object]) -> LocalLLMEntry | None:
    name = str(raw.get("name", "")).strip()
    kind = str(raw.get("kind", "")).strip()
    if not name or kind not in _CUSTOM_KINDS:
        _log.warning("Skipping invalid local-llm-registry.json entry: %r", raw)
        return None
    return LocalLLMEntry(
        name=name,
        kind=kind,
        description=str(raw.get("description", "")),
        repo_id=str(raw.get("repo_id", "")),
        filename=str(raw.get("filename", "")),
        context_window=int(cast(int, raw.get("context_window", 0)) or 0),
        flavors=(),
        path=str(raw.get("path", "")),
        url=str(raw.get("url", "")),
    )


def _entry_to_json(entry: LocalLLMEntry) -> dict[str, object]:
    return {
        "name": entry.name,
        "kind": entry.kind,
        "description": entry.description,
        "repo_id": entry.repo_id,
        "filename": entry.filename,
        "context_window": entry.context_window,
        "path": entry.path,
        "url": entry.url,
    }


def _load_raw(kodo_dir: Path) -> dict[str, object]:
    """The whole ``local-llm-registry.json`` as a plain dict, ``{}`` if absent/unreadable.

    Shared low-level accessor for every top-level key in the file (``entries``,
    ``llama_server_override_path``, ``flavors``, ``active_flavors``) — callers
    that only care about one key still go through this so a round trip never
    clobbers keys it doesn't know about (see :func:`_save_external`,
    :func:`~kodo.llms.local_registry._flavors.add_flavor`, etc., all of which
    load-modify-save the same dict).
    """
    path = _registry_file(kodo_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("Could not load %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(kodo_dir: Path, data: dict[str, object]) -> None:
    path = _registry_file(kodo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_external(kodo_dir: Path) -> tuple[list[LocalLLMEntry], str | None]:
    data = _load_raw(kodo_dir)
    raw_entries = data.get("entries", [])
    entries: list[LocalLLMEntry] = []
    if isinstance(raw_entries, list):
        for raw in raw_entries:
            if isinstance(raw, dict):
                entry = _entry_from_json(raw)
                if entry is not None:
                    entries.append(entry)
    override_raw = data.get("llama_server_override_path")
    override = str(override_raw) if isinstance(override_raw, str) and override_raw else None
    return entries, override


def _save_external(kodo_dir: Path, entries: list[LocalLLMEntry], override_path: str | None) -> None:
    data = _load_raw(kodo_dir)
    data["entries"] = [_entry_to_json(e) for e in entries]
    data["llama_server_override_path"] = override_path
    _save_raw(kodo_dir, data)


# ---------------------------------------------------------------------------
# Flavors: custom (user-added) definitions + active-flavor selection.
#
# Stored as two sibling top-level keys in the same local-llm-registry.json,
# both keyed by *entry name* (any kind except custom_server_url — a flavor is
# meaningless for a server kodo doesn't launch): ``flavors: {entry_name:
# [flavor...]}`` for custom flavor definitions, ``active_flavors: {entry_name:
# flavor_id}`` for which one (if any) is currently selected. Predefined
# flavors live in code instead, on the hardcoded entry's own ``flavors``
# tuple — see LlamaFlavor and _flavors.get_flavors().
# ---------------------------------------------------------------------------


def _parse_flavor_platform(raw: object) -> LlamaFlavorPlatform:
    """Best-effort :class:`LlamaFlavorPlatform` parse for a persisted/wire value.

    Falls back to ``BOTH`` ("no known restriction — runnable everywhere") for
    anything missing or unrecognized, same permissive spirit as
    :func:`kodo.server._app._parse_non_negative_int`.
    """
    try:
        return LlamaFlavorPlatform(str(raw))
    except ValueError:
        return LlamaFlavorPlatform.BOTH


def _flavor_from_json(raw: dict[str, object]) -> LlamaFlavor | None:
    flavor_id = str(raw.get("id", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not flavor_id or not name:
        return None
    return LlamaFlavor(
        id=flavor_id,
        name=name,
        description=str(raw.get("description", "")),
        llama_args=parse_llama_args(raw.get("llama_args", {})),
        min_ram=int(cast(int, raw.get("min_ram", 0)) or 0),
        min_vram=int(cast(int, raw.get("min_vram", 0)) or 0),
        platform=_parse_flavor_platform(raw.get("platform")),
    )


def _flavor_to_json(flavor: LlamaFlavor) -> dict[str, object]:
    return {
        "id": flavor.id,
        "name": flavor.name,
        "description": flavor.description,
        "llama_args": flavor.llama_args,
        "min_ram": flavor.min_ram,
        "min_vram": flavor.min_vram,
        "platform": flavor.platform.value,
    }


def _all_custom_flavors(data: dict[str, object]) -> dict[str, list[LlamaFlavor]]:
    raw = data.get("flavors")
    result: dict[str, list[LlamaFlavor]] = {}
    if not isinstance(raw, dict):
        return result
    for entry_name, raw_list in raw.items():
        if not isinstance(raw_list, list):
            continue
        flavors = [
            f
            for f in (_flavor_from_json(item) for item in raw_list if isinstance(item, dict))
            if f is not None
        ]
        if flavors:
            result[str(entry_name)] = flavors
    return result


def _write_custom_flavors(
    data: dict[str, object], all_flavors: dict[str, list[LlamaFlavor]]
) -> None:
    data["flavors"] = {
        entry_name: [_flavor_to_json(f) for f in flavors]
        for entry_name, flavors in all_flavors.items()
    }


def _all_active_flavors(data: dict[str, object]) -> dict[str, str]:
    raw = data.get("active_flavors")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def _slugify_flavor_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "flavor"
