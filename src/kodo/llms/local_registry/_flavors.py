"""Flavor CRUD (custom flavors, active-flavor selection) and launch-config resolution.

Depends on :mod:`kodo.llms.local_registry._entries` for
:func:`~kodo.llms.local_registry._entries.get_local_registry` (looking up an
entry by name before operating on its flavors) — :mod:`._entries` does not
import back from here, so this is one-directional.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import _types
from ._entries import get_local_registry
from ._io import (
    _all_active_flavors,
    _all_custom_flavors,
    _load_raw,
    _save_raw,
    _slugify_flavor_id,
    _write_custom_flavors,
)
from ._thinking import _strip_reasoning_cap_args
from ._types import LlamaFlavor, LlamaFlavorPlatform, LocalLLMEntry, _flavor_compatible_with_host

_log = logging.getLogger(__name__)

__all__ = [
    "add_flavor",
    "get_active_flavor",
    "get_effective_flavor_id",
    "get_flavors",
    "has_compatible_flavor",
    "remove_flavor",
    "resolve_context_window",
    "resolve_effective_llama_config",
    "set_active_flavor",
    "update_flavor",
]


def get_flavors(kodo_dir: Path, entry: LocalLLMEntry) -> tuple[LlamaFlavor, ...]:
    """Predefined + custom flavors available for *entry*, predefined slots first.

    A custom flavor whose ``id`` matches a predefined one is an **override**
    — its definition would be used in place of the predefined one (same list
    position), rather than being dropped. Nothing in the public API can
    create one any more: :func:`add_flavor` always auto-generates an id that
    can't collide with a predefined one, and :func:`update_flavor` rejects a
    predefined ``flavor_id`` outright (predefined flavors are strictly
    read-only). This merge is kept purely for resilience against a
    same-id override written to ``~/.kodo/etc/local-llm-registry.json`` by
    an older kodo version, before that restriction existed — new ones can't
    be created going forward. Custom flavors that don't collide with any
    predefined id are appended after, in the order they were added.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to look up flavors for.

    Returns:
        tuple[LlamaFlavor, ...]: Ordered, predefined slots first (each
        possibly override-replaced), then any additional custom flavors.
    """
    custom = _all_custom_flavors(_load_raw(kodo_dir)).get(entry.name, [])
    custom_by_id = {f.id: f for f in custom}
    predefined_ids = {f.id for f in entry.flavors}
    merged = tuple(custom_by_id.get(f.id, f) for f in entry.flavors)
    extra = tuple(f for f in custom if f.id not in predefined_ids)
    return merged + extra


def add_flavor(
    kodo_dir: Path,
    entry_name: str,
    name: str,
    *,
    description: str = "",
    llama_args: dict[str, str] | None = None,
    min_ram: int = 0,
    min_vram: int = 0,
    platform: LlamaFlavorPlatform = LlamaFlavorPlatform.BOTH,
) -> LlamaFlavor:
    """Add a brand-new custom flavor to *entry_name*, auto-assigning its ``id`` from *name*.

    Always creates a new flavor slot, never an override of an existing one —
    the "Add" side of the "manage flavors" modal. Use :func:`update_flavor`
    to change an *existing custom* flavor's definition in place (predefined
    flavors are read-only, see that function's docstring).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The registry entry (hardcoded or custom) to attach this
            flavor to.
        name: Display name; also the source for the auto-generated ``id``
            (slugified, de-duplicated against every flavor — predefined or
            custom — *entry_name* already has, e.g. ``my-flavor``,
            ``my-flavor-2``).
        description: Optional human-readable explanation.
        llama_args: CLI flags, same shape as ``LlamaFlavor.llama_args``. Any
            :data:`~kodo.llms.local_registry.RESERVED_REASONING_CAP_ARGS` key
            is silently dropped — those are managed automatically per
            session, not by flavors.
        min_ram: See ``LlamaFlavor.min_ram``. Defaults to ``0`` (unknown/no
            requirement — the hardware-fit check stays inactive).
        min_vram: See ``LlamaFlavor.min_vram``. Same default as *min_ram*.
        platform: See ``LlamaFlavor.platform``. Defaults to ``BOTH``.

    Returns:
        LlamaFlavor: The created flavor, with its assigned ``id``.

    Raises:
        ValueError: If *entry_name* is unknown, is a ``custom_server_url``
            (flavors are meaningless for a server kodo doesn't launch),
            *name* is blank, or a flavor named *name* already exists for
            *entry_name*.
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if entry.kind == "custom_server_url":
        raise ValueError("custom_server_url entries do not support flavors")
    name = name.strip()
    if not name:
        raise ValueError("Flavor name is required")

    existing_flavors = get_flavors(kodo_dir, entry)
    if any(f.name == name for f in existing_flavors):
        raise ValueError(f"A flavor named {name!r} already exists for {entry_name!r}")

    existing_ids = {f.id for f in existing_flavors}
    base_id = _slugify_flavor_id(name)
    flavor_id = base_id
    suffix = 2
    while flavor_id in existing_ids:
        flavor_id = f"{base_id}-{suffix}"
        suffix += 1

    flavor = LlamaFlavor(
        id=flavor_id,
        name=name,
        description=description,
        llama_args=_strip_reasoning_cap_args(dict(llama_args or {})),
        min_ram=min_ram,
        min_vram=min_vram,
        platform=platform,
    )
    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    all_flavors.setdefault(entry_name, []).append(flavor)
    _write_custom_flavors(data, all_flavors)
    _save_raw(kodo_dir, data)
    return flavor


def update_flavor(
    kodo_dir: Path,
    entry_name: str,
    flavor_id: str,
    name: str,
    *,
    description: str = "",
    llama_args: dict[str, str] | None = None,
    min_ram: int = 0,
    min_vram: int = 0,
    platform: LlamaFlavorPlatform = LlamaFlavorPlatform.BOTH,
) -> LlamaFlavor:
    """Overwrite an existing *custom* flavor's definition in place, keeping its ``id``.

    Predefined flavors are strictly read-only: this rejects *flavor_id*
    outright if it names one of *entry_name*'s predefined flavors (checked
    against ``entry.flavors``, the hardcoded tuple — same check
    :func:`remove_flavor` uses), even if a stale custom override from before
    this restriction existed happens to sit under that id. Anyone who wants
    a predefined flavor's config with different values should use
    :func:`add_flavor` to create a new custom flavor (copying the
    predefined one's ``llama_args`` as a starting point) rather than
    mutating the original.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry the flavor belongs to.
        flavor_id: The existing *custom* flavor's id (from
            :func:`get_flavors`) — unlike :func:`add_flavor`, this is never
            re-derived from *name*.
        name: New display name.
        description: New description.
        llama_args: New CLI flags, same shape as ``LlamaFlavor.llama_args``.
            Any :data:`~kodo.llms.local_registry.RESERVED_REASONING_CAP_ARGS`
            key is silently dropped — those are managed automatically per
            session, not by flavors.
        min_ram: See ``LlamaFlavor.min_ram``. Defaults to ``0``, same as
            :func:`add_flavor` — unlike the pre-read-only behavior, this no
            longer carries the original flavor's value forward automatically;
            the caller must resend it to keep it unchanged.
        min_vram: See ``LlamaFlavor.min_vram``. Same default as *min_ram*.
        platform: See ``LlamaFlavor.platform``. Defaults to ``BOTH``, same
            not-carried-forward caveat as *min_ram*/*min_vram*.

    Returns:
        LlamaFlavor: The updated flavor.

    Raises:
        ValueError: If *entry_name* is unknown, is a ``custom_server_url``,
            *flavor_id* names a predefined flavor, *name* is blank,
            *flavor_id* isn't one of *entry_name*'s current flavors, or
            another flavor of *entry_name* already has *name*.
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if entry.kind == "custom_server_url":
        raise ValueError("custom_server_url entries do not support flavors")
    if any(f.id == flavor_id for f in entry.flavors):
        raise ValueError(f"{flavor_id!r} is a predefined flavor and cannot be edited")
    name = name.strip()
    if not name:
        raise ValueError("Flavor name is required")

    existing_flavors = get_flavors(kodo_dir, entry)
    original = next((f for f in existing_flavors if f.id == flavor_id), None)
    if original is None:
        raise ValueError(f"Unknown flavor {flavor_id!r} for {entry_name!r}")
    if any(f.name == name and f.id != flavor_id for f in existing_flavors):
        raise ValueError(f"A flavor named {name!r} already exists for {entry_name!r}")

    flavor = LlamaFlavor(
        id=flavor_id,
        name=name,
        description=description,
        llama_args=_strip_reasoning_cap_args(dict(llama_args or {})),
        min_ram=min_ram,
        min_vram=min_vram,
        platform=platform,
    )
    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    existing = all_flavors.get(entry_name, [])
    replaced = False
    new_list: list[LlamaFlavor] = []
    for f in existing:
        if f.id == flavor_id:
            new_list.append(flavor)
            replaced = True
        else:
            new_list.append(f)
    if not replaced:
        new_list.append(flavor)
    all_flavors[entry_name] = new_list
    _write_custom_flavors(data, all_flavors)
    _save_raw(kodo_dir, data)
    return flavor


def remove_flavor(kodo_dir: Path, entry_name: str, flavor_id: str) -> None:
    """Remove a custom flavor. Predefined flavors cannot be removed.

    A predefined flavor is rejected even if it currently has a custom
    *override* (see :func:`update_flavor`) — removing the override would
    silently revert it to the hardcoded definition, which is not "removing a
    flavor" from the user's perspective (the "Remove" button stays disabled
    for these ids in the UI for the same reason).

    If *flavor_id* was the active flavor for *entry_name*, the active
    selection resets to "" (Default — the entry's own launch config); the
    caller is responsible for restarting llama-server if *entry_name* is the
    currently running model (mirrors :func:`set_active_flavor`).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry the flavor belongs to.
        flavor_id: The flavor to remove.

    Raises:
        ValueError: If *flavor_id* is not a custom flavor of *entry_name*
            (includes the case where it's predefined, overridden or not).
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is not None and any(f.id == flavor_id for f in entry.flavors):
        raise ValueError(f"{flavor_id!r} is a predefined flavor and cannot be removed")

    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    current = all_flavors.get(entry_name, [])
    remaining = [f for f in current if f.id != flavor_id]
    if len(remaining) == len(current):
        raise ValueError(f"No custom flavor {flavor_id!r} for {entry_name!r}")
    if remaining:
        all_flavors[entry_name] = remaining
    else:
        all_flavors.pop(entry_name, None)
    _write_custom_flavors(data, all_flavors)

    active = _all_active_flavors(data)
    if active.get(entry_name) == flavor_id:
        active.pop(entry_name, None)
        data["active_flavors"] = active
    _save_raw(kodo_dir, data)


def get_active_flavor(kodo_dir: Path, entry_name: str) -> str:
    """The active flavor id for *entry_name*, or ``""`` for Default (the entry's own config)."""
    return _all_active_flavors(_load_raw(kodo_dir)).get(entry_name, "")


def set_active_flavor(kodo_dir: Path, entry_name: str, flavor_id: str) -> None:
    """Set (or clear) the active flavor for *entry_name*.

    Purely a persistence op — it does not touch a running llama-server.
    Callers that just changed the *currently active local model*'s flavor
    are responsible for restarting it (see ``local_llm.set_active_flavor``'s
    handler in ``kodo/server/_app.py``, doc/WS_PROTOCOL.md §7.6).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry to set the active flavor for.
        flavor_id: A flavor id from :func:`get_flavors`, or ``""`` for
            Default.

    Raises:
        ValueError: If *entry_name* is unknown, or *flavor_id* is non-empty
            and not one of *entry_name*'s flavors.
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if flavor_id and not any(f.id == flavor_id for f in get_flavors(kodo_dir, entry)):
        raise ValueError(f"Unknown flavor {flavor_id!r} for {entry_name!r}")

    data = _load_raw(kodo_dir)
    active = _all_active_flavors(data)
    if flavor_id:
        active[entry_name] = flavor_id
    else:
        active.pop(entry_name, None)
    data["active_flavors"] = active
    _save_raw(kodo_dir, data)


def resolve_context_window(entry: LocalLLMEntry, flavor: LlamaFlavor | None) -> int:
    """The effective context window (tokens) for *entry* launched with *flavor*.

    Deduced from :meth:`flavor.get_context_size() <LlamaFlavor.get_context_size>`
    when it's positive; otherwise (absent, ``0``, or unparseable — e.g.
    ``--ctx-size 0``'s "read the GGUF's own trained context length"
    sentinel) falls back to *entry*'s own ``context_window``. There is no
    separate ``context_window`` field on :class:`LlamaFlavor` any more —
    this function is the single place that turns launch args into a
    token-budgeting number (see :func:`kodo.llms.get_context_window`, which
    uses it via :func:`resolve_effective_llama_config`).

    Args:
        entry: The registry entry supplying the fallback value.
        flavor: The flavor about to be launched, or ``None`` (falls straight
            back to *entry*'s own ``context_window``).

    Returns:
        int: The effective context window in tokens.
    """
    if flavor is not None:
        value = flavor.get_context_size()
        if value > 0:
            return value
    return entry.context_window


def has_compatible_flavor(kodo_dir: Path, entry: LocalLLMEntry) -> bool:
    """Whether *entry* has at least one flavor launchable on this host.

    ``True`` when *entry* has no flavors at all (nothing to be incompatible
    about — a flavor-less ``custom_*`` entry launches with its own bare
    config, see :func:`resolve_effective_llama_config`) or when at least one
    of :func:`get_flavors`' flavors is compatible with
    :func:`~kodo.llms.local_registry.current_host_platform`
    (:func:`~kodo.llms.local_registry._types._flavor_compatible_with_host`).
    ``False`` only when *entry* has flavors and every single one targets the
    other platform — the case :func:`kodo.llms.llamacpp.ensure_llama_running`
    refuses to launch (doc/LLM_REGISTRY.md §4.6b).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to check.

    Returns:
        bool: Whether *entry* can run on this host at all.
    """
    flavors = get_flavors(kodo_dir, entry)
    if not flavors:
        return True
    return any(_flavor_compatible_with_host(f) for f in flavors)


def get_effective_flavor_id(kodo_dir: Path, entry: LocalLLMEntry) -> str:
    """The flavor id that would actually be launched for *entry* right now.

    - The active flavor (:func:`get_active_flavor`), if set, still present
      among :func:`get_flavors`, and compatible with
      :func:`~kodo.llms.local_registry.current_host_platform` — returned
      as-is.
    - An *explicit* active flavor that names a flavor incompatible with
      :func:`~kodo.llms.local_registry.current_host_platform` is no longer
      honored as-is: if a compatible flavor exists among *entry*'s flavors,
      this both returns and **persists** (:func:`set_active_flavor`) the
      first compatible one — the same correction applied below for an
      unset/stale selection, just also written back so kodo-vsix's sidebar
      picker and every other reader of :func:`get_active_flavor` agree with
      what's actually launched from here on. (If the platform is switched
      back later, the original choice has to be re-picked by hand — it is
      not remembered anywhere once overwritten.) If *no* flavor is
      compatible, the explicit choice is returned unchanged — there's
      nothing better to fall back to.
    - Unset, or a stale id whose definition was removed since it was
      selected ("Default" in the UI): the first available flavor that is
      compatible with :func:`~kodo.llms.local_registry.current_host_platform`
      (see :func:`~kodo.llms.local_registry._types._flavor_compatible_with_host`)
      — e.g. on Apple Silicon, a ``LlamaFlavorPlatform.GPU``-only flavor is
      skipped in favor of the next compatible one. If *no* flavor is
      compatible (every one of *entry*'s flavors targets the other
      platform), falls back to the first available flavor regardless, same
      as before this check existed — some (possibly broken) launch is
      preferable to none for *this* function; the actual launch path
      (:func:`kodo.llms.llamacpp.ensure_llama_running`) independently
      refuses to launch at all in that case (see
      :func:`has_compatible_flavor`), so this permissive fallback only
      still matters for other callers (context-window lookup, crash
      messaging, "is this flavor still the effective one" comparisons).
    - ``""`` if *entry* has no flavors at all.

    Callers that need to decide whether editing/removing a specific flavor
    id would change what's currently launched (e.g. whether to restart
    llama-server) compare against this, not the raw
    :func:`get_active_flavor` value — an *unset* active flavor still
    resolves to a real one (the first compatible, or first overall), so a
    change to that one is effectively an active-flavor change too.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to resolve.

    Returns:
        str: A flavor id from :func:`get_flavors`, or ``""``.
    """
    flavors = get_flavors(kodo_dir, entry)
    if not flavors:
        return ""
    compatible = [f for f in flavors if _flavor_compatible_with_host(f)]
    flavor_id = get_active_flavor(kodo_dir, entry.name)
    if flavor_id and any(f.id == flavor_id for f in flavors):
        if not compatible or any(f.id == flavor_id for f in compatible):
            return flavor_id
        _log.info(
            "Active flavor %r of %r is not compatible with the current platform "
            "(%s); switching to %r",
            flavor_id,
            entry.name,
            _types.current_host_platform().value,
            compatible[0].id,
        )
        set_active_flavor(kodo_dir, entry.name, compatible[0].id)
        return compatible[0].id
    if compatible:
        return compatible[0].id
    _log.warning(
        "No flavor of %r is compatible with the current platform (%s); falling back to %r anyway",
        entry.name,
        _types.current_host_platform().value,
        flavors[0].id,
    )
    return flavors[0].id


def resolve_effective_llama_config(
    kodo_dir: Path, entry: LocalLLMEntry
) -> tuple[dict[str, str], int]:
    """The ``(llama_args, context_window)`` actually launched for *entry*.

    Flavors are the only source of ``llama_args`` — *entry* itself carries
    none — so this always resolves to some flavor's args, selected via
    :func:`get_effective_flavor_id`. If *entry* has no flavors at all (only
    possible for a flavor-less ``custom_*`` entry whose sole flavor was
    since removed, or a ``custom_server_url`` entry, which is never actually
    launched this way), returns ``({}, entry.context_window)`` — no CLI args
    beyond the server-management ones in
    :class:`kodo.llms.llamacpp.LlamaServerConfig`.

    ``context_window`` is resolved via :func:`resolve_context_window` from
    the chosen flavor's own launch args, falling back to ``entry``'s.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry about to be launched.

    Returns:
        tuple[dict[str, str], int]: ``(llama_args, context_window)`` to use
        for this launch.
    """
    flavors = get_flavors(kodo_dir, entry)
    flavor_id = get_effective_flavor_id(kodo_dir, entry)
    flavor = next((f for f in flavors if f.id == flavor_id), None) if flavor_id else None
    if flavor is None:
        return {}, entry.context_window
    return dict(flavor.llama_args), resolve_context_window(entry, flavor)
