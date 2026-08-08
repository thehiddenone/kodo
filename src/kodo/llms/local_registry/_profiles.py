"""Profile CRUD, knob state, and launch-config resolution.

The two things an entry can be launched with (doc/LLM_REGISTRY.md §4.6):

- The **Default profile** — never stored as a profile at all. Its args are
  computed on demand from the entry's ``base_llama_args`` plus its knobs
  (:mod:`._knobs`), given the user's persisted ``{knob_id: selection}`` map.
  Every entry has one, it cannot be deleted, and it is what an entry runs
  under until the user picks something else.
- Zero or more **user-defined profiles** (:class:`~._types.LlmProfile`) — raw
  arg sets, created in the "Manage profiles" editor. Selecting one **fully
  replaces** the Default profile's args rather than layering onto them.

``active_profiles[entry_name]`` holds the selected profile's id, with ``""``
(or an absent key) meaning the Default profile. A stale id — a profile
removed since it was selected — resolves back to the Default profile rather
than to some other profile: falling through to an arbitrary neighbour is how
the old flavor model surprised people, and there is now always a real,
always-valid configuration to fall back to.

Depends on :mod:`._entries` for
:func:`~kodo.llms.local_registry._entries.get_local_registry` (looking up an
entry before operating on it) — :mod:`._entries` does not import back from
here, so this is one-directional.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ._entries import get_local_registry
from ._io import (
    _all_active_profiles,
    _all_knob_selections,
    _all_profiles,
    _load_raw,
    _save_raw,
    _slugify_profile_id,
    _write_knob_selections,
    _write_profiles,
)
from ._knobs import KnobKind, knob_selection_args, resolve_knob_selections
from ._reserved import strip_reserved_llama_args
from ._types import LlmProfile, LocalLLMEntry

_log = logging.getLogger(__name__)

__all__ = [
    "add_profile",
    "get_active_profile",
    "get_knob_selections",
    "get_profiles",
    "remove_profile",
    "resolve_context_window",
    "resolve_default_profile_args",
    "resolve_effective_llama_config",
    "set_active_profile",
    "set_knobs",
    "update_profile",
]


def _require_launchable(kodo_dir: Path, entry_name: str) -> LocalLLMEntry:
    """The entry named *entry_name*, if it exists and kodo actually launches it.

    Raises:
        ValueError: If *entry_name* is unknown, or is a ``custom_server_url``
            entry (kodo does not start that process, so it has no launch args
            to profile).
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if entry.kind == "custom_server_url":
        raise ValueError("custom_server_url entries do not support profiles")
    return entry


# ---------------------------------------------------------------------------
# User-defined profiles
# ---------------------------------------------------------------------------


def get_profiles(kodo_dir: Path, entry: LocalLLMEntry) -> tuple[LlmProfile, ...]:
    """*entry*'s user-defined profiles, in the order they were added.

    Does **not** include the Default profile — that is not an
    :class:`~._types.LlmProfile` and has no stored args (see
    :func:`resolve_default_profile_args`). An entry with no user-defined
    profiles returns ``()``, which is the normal state.
    """
    return tuple(_all_profiles(_load_raw(kodo_dir)).get(entry.name, []))


def add_profile(
    kodo_dir: Path,
    entry_name: str,
    name: str,
    *,
    description: str = "",
    llama_args: dict[str, str] | None = None,
) -> LlmProfile:
    """Add a new user-defined profile to *entry_name*, auto-assigning its ``id`` from *name*.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The registry entry (hardcoded or custom) to attach this
            profile to.
        name: Display name; also the source for the auto-generated ``id``
            (slugified, de-duplicated against this entry's existing profiles,
            e.g. ``my-profile``, ``my-profile-2``).
        description: Optional human-readable explanation.
        llama_args: CLI flags, same shape as ``LlmProfile.llama_args``. Any
            :data:`~kodo.llms.local_registry.RESERVED_LLAMA_ARGS` key is
            dropped (and logged) — those are kodo's to set per launch.

    Returns:
        LlmProfile: The created profile, with its assigned ``id``.

    Raises:
        ValueError: If *entry_name* is unknown or is a ``custom_server_url``,
            *name* is blank, or a profile named *name* already exists for
            *entry_name*.
    """
    entry = _require_launchable(kodo_dir, entry_name)
    name = name.strip()
    if not name:
        raise ValueError("Profile name is required")

    existing = get_profiles(kodo_dir, entry)
    if any(p.name == name for p in existing):
        raise ValueError(f"A profile named {name!r} already exists for {entry_name!r}")

    existing_ids = {p.id for p in existing}
    base_id = _slugify_profile_id(name)
    profile_id = base_id
    suffix = 2
    while profile_id in existing_ids:
        profile_id = f"{base_id}-{suffix}"
        suffix += 1

    profile = LlmProfile(
        id=profile_id,
        name=name,
        description=description,
        llama_args=strip_reserved_llama_args(dict(llama_args or {})),
    )
    data = _load_raw(kodo_dir)
    all_profiles = _all_profiles(data)
    all_profiles.setdefault(entry_name, []).append(profile)
    _write_profiles(data, all_profiles)
    _save_raw(kodo_dir, data)
    return profile


def update_profile(
    kodo_dir: Path,
    entry_name: str,
    profile_id: str,
    name: str,
    *,
    description: str = "",
    llama_args: dict[str, str] | None = None,
) -> LlmProfile:
    """Overwrite an existing profile's definition in place, keeping its ``id``.

    Every profile is user-defined and therefore editable — unlike the flavor
    model this replaces, there is no read-only predefined variant to reject
    here. What used to be a predefined flavor is a knob on the Default
    profile now (:func:`set_knobs`), which is edited through its own path.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry the profile belongs to.
        profile_id: The existing profile's id (from :func:`get_profiles`) —
            unlike :func:`add_profile`, never re-derived from *name*.
        name: New display name.
        description: New description.
        llama_args: New CLI flags. Any
            :data:`~kodo.llms.local_registry.RESERVED_LLAMA_ARGS` key is
            dropped, same as in :func:`add_profile`.

    Returns:
        LlmProfile: The updated profile.

    Raises:
        ValueError: If *entry_name* is unknown or is a ``custom_server_url``,
            *name* is blank, *profile_id* isn't one of *entry_name*'s
            profiles, or another profile of *entry_name* already has *name*.
    """
    entry = _require_launchable(kodo_dir, entry_name)
    name = name.strip()
    if not name:
        raise ValueError("Profile name is required")

    existing = get_profiles(kodo_dir, entry)
    if not any(p.id == profile_id for p in existing):
        raise ValueError(f"Unknown profile {profile_id!r} for {entry_name!r}")
    if any(p.name == name and p.id != profile_id for p in existing):
        raise ValueError(f"A profile named {name!r} already exists for {entry_name!r}")

    profile = LlmProfile(
        id=profile_id,
        name=name,
        description=description,
        llama_args=strip_reserved_llama_args(dict(llama_args or {})),
    )
    data = _load_raw(kodo_dir)
    all_profiles = _all_profiles(data)
    all_profiles[entry_name] = [
        profile if p.id == profile_id else p for p in all_profiles.get(entry_name, [])
    ]
    _write_profiles(data, all_profiles)
    _save_raw(kodo_dir, data)
    return profile


def remove_profile(kodo_dir: Path, entry_name: str, profile_id: str) -> None:
    """Remove a user-defined profile.

    If *profile_id* was the active selection for *entry_name*, the selection
    resets to ``""`` — the Default profile. The caller is responsible for
    restarting llama-server if *entry_name* is the currently running model
    (mirrors :func:`set_active_profile`).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry the profile belongs to.
        profile_id: The profile to remove.

    Raises:
        ValueError: If *entry_name* has no profile with that id.
    """
    data = _load_raw(kodo_dir)
    all_profiles = _all_profiles(data)
    current = all_profiles.get(entry_name, [])
    remaining = [p for p in current if p.id != profile_id]
    if len(remaining) == len(current):
        raise ValueError(f"No profile {profile_id!r} for {entry_name!r}")
    if remaining:
        all_profiles[entry_name] = remaining
    else:
        all_profiles.pop(entry_name, None)
    _write_profiles(data, all_profiles)

    active = _all_active_profiles(data)
    if active.get(entry_name) == profile_id:
        active.pop(entry_name, None)
        data["active_profiles"] = active
    _save_raw(kodo_dir, data)


def get_active_profile(kodo_dir: Path, entry_name: str) -> str:
    """The active profile id for *entry_name*, or ``""`` for the Default profile.

    Resolves a stale id — one whose profile has since been removed — back to
    ``""`` rather than reporting it, so every caller sees the configuration
    that would actually be launched (see :func:`resolve_effective_llama_config`).
    """
    data = _load_raw(kodo_dir)
    profile_id = _all_active_profiles(data).get(entry_name, "")
    if not profile_id:
        return ""
    if any(p.id == profile_id for p in _all_profiles(data).get(entry_name, [])):
        return profile_id
    _log.info(
        "Active profile %r of %r no longer exists; falling back to the Default profile",
        profile_id,
        entry_name,
    )
    return ""


def set_active_profile(kodo_dir: Path, entry_name: str, profile_id: str) -> None:
    """Select *profile_id* (or ``""`` for the Default profile) for *entry_name*.

    Purely a persistence op — it does not touch a running llama-server.
    Callers that just changed the *currently active local model*'s profile
    are responsible for restarting it (see ``local_llm.set_active_profile``'s
    handler in ``kodo/server/_app.py``, doc/WS_PROTOCOL.md §7.6).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry to set the active profile for.
        profile_id: A profile id from :func:`get_profiles`, or ``""`` for the
            Default profile.

    Raises:
        ValueError: If *entry_name* is unknown or is a ``custom_server_url``,
            or *profile_id* is non-empty and not one of *entry_name*'s
            profiles.
    """
    entry = _require_launchable(kodo_dir, entry_name)
    if profile_id and not any(p.id == profile_id for p in get_profiles(kodo_dir, entry)):
        raise ValueError(f"Unknown profile {profile_id!r} for {entry_name!r}")

    data = _load_raw(kodo_dir)
    active = _all_active_profiles(data)
    if profile_id:
        active[entry_name] = profile_id
    else:
        active.pop(entry_name, None)
    data["active_profiles"] = active
    _save_raw(kodo_dir, data)


# ---------------------------------------------------------------------------
# Knobs (the Default profile's state)
# ---------------------------------------------------------------------------


def get_knob_selections(kodo_dir: Path, entry: LocalLLMEntry) -> dict[str, str]:
    """*entry*'s **resolved** knob state — one entry per knob, defaults filled in.

    What the Configure modal renders, and what
    :func:`resolve_default_profile_args` builds the Default profile's args
    from. Never sparse and never stale: a stored selection naming an option a
    knob no longer has is replaced by that knob's resolved default (see
    :func:`~kodo.llms.local_registry._knobs.resolve_knob_selections`), so the
    UI can bind a ``<select>`` straight to it.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to read knob state for.

    Returns:
        dict[str, str]: ``{knob_id: selection}`` covering every knob in
        ``entry.knobs``, in that order. ``{}`` for an entry with no knobs.
    """
    stored = _all_knob_selections(_load_raw(kodo_dir)).get(entry.name, {})
    return resolve_knob_selections(entry.knobs, stored, entry.knob_defaults)


def set_knobs(kodo_dir: Path, entry_name: str, selections: dict[str, str]) -> dict[str, str]:
    """Apply a whole knob selection for *entry_name*'s Default profile.

    Bulk, not per-knob: this is the "Apply" button of the Configure modal, and
    replacing the entry's whole selection in one write means a modal that was
    opened before some other window changed a knob cannot resurrect half of
    the old state.

    Stored **sparsely** — any selection equal to the knob's currently resolved
    default is dropped rather than written. That is what lets a later kodo
    release change a knob's default (or an entry's ``knob_defaults``) and have
    it take effect for every user who never deliberately moved that knob,
    while still respecting the choice of everyone who did.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry whose Default profile is being configured.
        selections: ``{knob_id: selection}``. Need not be complete — a knob
            absent from this map is reset to its default, since this replaces
            the whole selection. A key naming a knob *entry_name* does not
            offer is ignored with a log line (a stale client, or a knob
            removed in this release).

    Returns:
        dict[str, str]: The resolved selection after the write, exactly as
        :func:`get_knob_selections` would return it.

    Raises:
        ValueError: If *entry_name* is unknown or is a ``custom_server_url``,
            a value names an option its knob does not have, or a ``NUMBER``
            knob's value is neither blank nor a number.
    """
    entry = _require_launchable(kodo_dir, entry_name)
    knobs_by_id = {knob.id: knob for knob in entry.knobs}

    cleaned: dict[str, str] = {}
    for knob_id, raw in selections.items():
        knob = knobs_by_id.get(knob_id)
        if knob is None:
            _log.info("Ignoring unknown knob %r for %r", knob_id, entry_name)
            continue
        value = str(raw).strip()
        if knob.kind is KnobKind.NUMBER:
            if value:
                try:
                    float(value)
                except ValueError:
                    raise ValueError(f"Knob {knob_id!r} takes a number, got {raw!r}") from None
        elif value and knob.option(value) is None:
            raise ValueError(f"Knob {knob_id!r} has no option {value!r}")
        cleaned[knob_id] = value

    # Sparse: only what differs from the default this entry would resolve to.
    defaults = resolve_knob_selections(entry.knobs, {}, entry.knob_defaults)
    sparse = {k: v for k, v in cleaned.items() if v != defaults.get(k, "")}

    data = _load_raw(kodo_dir)
    all_selections = _all_knob_selections(data)
    if sparse:
        all_selections[entry_name] = sparse
    else:
        all_selections.pop(entry_name, None)
    _write_knob_selections(data, all_selections)
    _save_raw(kodo_dir, data)
    return resolve_knob_selections(entry.knobs, sparse, entry.knob_defaults)


# ---------------------------------------------------------------------------
# Launch-config resolution
# ---------------------------------------------------------------------------


def resolve_default_profile_args(kodo_dir: Path, entry: LocalLLMEntry) -> dict[str, str]:
    """*entry*'s Default profile args: its base args with knob args layered on top.

    The one place the base-args-are-the-floor rule is applied (see
    :mod:`._knobs`). Knob args win on conflict, which is what lets a context
    knob's ``--ctx-size 524288`` override the base ``--ctx-size 0``.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to resolve.

    Returns:
        dict[str, str]: A fresh dict, safe for the caller to mutate.
    """
    stored = _all_knob_selections(_load_raw(kodo_dir)).get(entry.name, {})
    args = dict(entry.base_llama_args)
    args.update(knob_selection_args(entry.knobs, stored, entry.knob_defaults))
    return args


def resolve_context_window(entry: LocalLLMEntry, llama_args: dict[str, str]) -> int:
    """The effective context window (tokens) for *entry* launched with *llama_args*.

    Deduced from *llama_args*' own ``--ctx-size``/``-c`` when that parses to a
    positive integer; otherwise (absent, ``0``, or unparseable — e.g.
    ``--ctx-size 0``'s "read the GGUF's own trained context length" sentinel)
    falls back to *entry*'s own ``context_window``. The single place launch
    args are turned into a token-budgeting number (see
    :func:`kodo.llms.get_context_window`, which reaches it via
    :func:`resolve_effective_llama_config`).

    Takes the resolved args rather than a profile, because the Default profile
    has no object to pass — its args are computed, not stored.

    Args:
        entry: The registry entry supplying the fallback value.
        llama_args: The launch args about to be used.

    Returns:
        int: The effective context window in tokens.
    """
    value = LlmProfile(id="", name="", llama_args=llama_args).get_context_size()
    return value if value > 0 else entry.context_window


def resolve_effective_llama_config(
    kodo_dir: Path, entry: LocalLLMEntry
) -> tuple[dict[str, str], int]:
    """The ``(llama_args, context_window)`` actually launched for *entry*.

    The active user-defined profile's args verbatim if one is selected (a
    profile fully replaces the Default profile — it is never merged with it),
    otherwise the Default profile's computed args
    (:func:`resolve_default_profile_args`). A ``custom_server_url`` entry —
    never launched this way — resolves to ``({}, entry.context_window)``.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry about to be launched.

    Returns:
        tuple[dict[str, str], int]: ``(llama_args, context_window)`` for this
        launch.
    """
    if entry.kind == "custom_server_url":
        return {}, entry.context_window
    profile_id = get_active_profile(kodo_dir, entry.name)
    if profile_id:
        profile = next((p for p in get_profiles(kodo_dir, entry) if p.id == profile_id), None)
        if profile is not None:
            args = dict(profile.llama_args)
            return args, resolve_context_window(entry, args)
    args = resolve_default_profile_args(kodo_dir, entry)
    return args, resolve_context_window(entry, args)
