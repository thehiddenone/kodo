"""``~/.kodo/etc/local-llm-registry.json`` file I/O and JSON (de)serialization.

The external collection (``custom_*`` entries) plus the global llama-server
binary override path, and the three sibling ``profiles``/``active_profiles``/
``knob_selections`` top-level keys (see
:mod:`kodo.llms.local_registry._profiles`) all live in this one file, owned
(read + written) entirely by this package — the kodo-vsix extension only ever
reads it indirectly, via the WS protocol (see doc/LLM_REGISTRY.md).

The ``flavors``/``active_flavors`` keys this file used to carry are **gone**,
not migrated: predefined flavors became knobs, and a custom flavor's raw arg
set is exactly what an :class:`~kodo.llms.local_registry.LlmProfile` is, but
matching them up automatically would have to guess which knob selection
reproduces a hand-edited arg dict. A pre-existing file's old keys are left
untouched on disk (:func:`_load_raw`/:func:`_save_raw` never drop keys they
don't know about) and simply ignored — an install that had custom flavors
starts fresh on the Default profile, with the old definitions recoverable by
hand from the file if anyone wants them.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import cast

from ._types import LlmProfile, LocalLLMEntry

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
    """Parse the profile editor's raw multi-line text box into ``llama_args``.

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
        base_llama_args=parse_llama_args(raw.get("base_llama_args", {})),
        # Knobs are code, never persisted data — a custom entry's knob tuple is
        # attached on load by _entries.get_local_registry(), which is also what
        # lets a kodo upgrade change the shared knob set without a file rewrite.
        knobs=(),
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
        "base_llama_args": entry.base_llama_args,
        "path": entry.path,
        "url": entry.url,
    }


def _load_raw(kodo_dir: Path) -> dict[str, object]:
    """The whole ``local-llm-registry.json`` as a plain dict, ``{}`` if absent/unreadable.

    Shared low-level accessor for every top-level key in the file (``entries``,
    ``llama_server_override_path``, ``profiles``, ``active_profiles``,
    ``knob_selections``) — callers that only care about one key still go
    through this so a round trip never clobbers keys it doesn't know about
    (see :func:`_save_external`,
    :func:`~kodo.llms.local_registry._profiles.add_profile`, etc., all of which
    load-modify-save the same dict). That is also what preserves a legacy
    ``flavors`` key rather than deleting it — see the module docstring.
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
# Profiles: user-defined definitions, active selection, and knob state.
#
# Three sibling top-level keys in the same local-llm-registry.json, all keyed
# by *entry name* (any kind except custom_server_url — launch args are
# meaningless for a server kodo doesn't launch):
#
#   profiles:        {entry_name: [profile...]}  user-defined arg sets
#   active_profiles: {entry_name: profile_id}    "" / absent = Default profile
#   knob_selections: {entry_name: {knob_id: selection}}  Default profile state
#
# knob_selections is stored sparsely — only knobs the user actually moved off
# their default appear, so changing a knob's default in a later kodo release
# takes effect for everyone who never touched it. See _profiles.set_knobs.
# ---------------------------------------------------------------------------


def _profile_from_json(raw: dict[str, object]) -> LlmProfile | None:
    profile_id = str(raw.get("id", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not profile_id or not name:
        return None
    return LlmProfile(
        id=profile_id,
        name=name,
        description=str(raw.get("description", "")),
        llama_args=parse_llama_args(raw.get("llama_args", {})),
    )


def _profile_to_json(profile: LlmProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "llama_args": profile.llama_args,
    }


def _all_profiles(data: dict[str, object]) -> dict[str, list[LlmProfile]]:
    raw = data.get("profiles")
    result: dict[str, list[LlmProfile]] = {}
    if not isinstance(raw, dict):
        return result
    for entry_name, raw_list in raw.items():
        if not isinstance(raw_list, list):
            continue
        profiles = [
            p
            for p in (_profile_from_json(item) for item in raw_list if isinstance(item, dict))
            if p is not None
        ]
        if profiles:
            result[str(entry_name)] = profiles
    return result


def _write_profiles(data: dict[str, object], all_profiles: dict[str, list[LlmProfile]]) -> None:
    data["profiles"] = {
        entry_name: [_profile_to_json(p) for p in profiles]
        for entry_name, profiles in all_profiles.items()
    }


def _all_active_profiles(data: dict[str, object]) -> dict[str, str]:
    raw = data.get("active_profiles")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def _all_knob_selections(data: dict[str, object]) -> dict[str, dict[str, str]]:
    """``{entry_name: {knob_id: selection}}``, skipping anything malformed.

    Values are plain strings whatever the knob's kind — an option id for a
    checkbox/dropdown, the number as text for a NUMBER knob (see
    :mod:`kodo.llms.local_registry._knobs`).
    """
    raw = data.get("knob_selections")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for entry_name, selections in raw.items():
        if not isinstance(selections, dict):
            continue
        parsed = {str(k): str(v) for k, v in selections.items() if isinstance(v, str)}
        if parsed:
            result[str(entry_name)] = parsed
    return result


def _write_knob_selections(
    data: dict[str, object], all_selections: dict[str, dict[str, str]]
) -> None:
    data["knob_selections"] = all_selections


def _slugify_profile_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "profile"
