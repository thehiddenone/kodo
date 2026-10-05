"""Public registry API: the merged entry map, custom-entry CRUD, override path.

The merged registry has three sources, in precedence order (see
doc/LLM_REGISTRY.md §4):

1. the user catalog, ``~/.kodo/local_llms/<base_llm>/<name>.json``
   (:func:`~._catalog.load_user_catalog`) — replaces a shipped entry of the
   same name outright;
2. the shipped catalog (``_catalog._HARDCODED_LOCAL_MODELS``);
3. the ``custom_*`` entries in ``local-llm-registry.json`` (:mod:`._io`) —
   skipped when their name is already taken by either catalog.

Every catalog entry of a family that ships standalone MTP heads is served
with that family's head picker attached
(:func:`~._catalog_files.attach_mtp_sidecars`); the heads themselves are
:func:`get_mtp_sidecars`.

References the shipped catalog via ``_catalog._HARDCODED_LOCAL_MODELS``
(qualified module attribute access rather than ``from ._catalog import
_HARDCODED_LOCAL_MODELS``) specifically so tests can monkeypatch
``_catalog._HARDCODED_LOCAL_MODELS`` and have every function here observe
the patched value.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from . import _catalog
from ._catalog_files import attach_mtp_sidecars, catalog_sort_key
from ._io import (
    _CUSTOM_KINDS,
    _all_active_profiles,
    _all_knob_selections,
    _all_profiles,
    _load_external,
    _load_raw,
    _registry_file_unreadable,
    _save_external,
    _save_raw,
    _write_knob_selections,
    _write_profiles,
)
from ._knobs_shared import BASE_LLAMA_ARGS, SHARED_KNOBS
from ._mtp_sidecars import MTP_SIDECAR_MODEL_ID_PREFIX, MtpSidecar, parse_mtp_sidecar_model_id
from ._types import LocalLLMEntry

_log = logging.getLogger(__name__)

__all__ = [
    "add_local_entry",
    "clear_llama_server_override_path",
    "get_llama_server_override_path",
    "get_local_registry",
    "get_mtp_sidecars",
    "prune_unknown_model_state",
    "remove_local_entry",
    "set_llama_server_override_path",
    "stale_mtp_sidecar_model_ids",
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
    """Return the merged local registry: catalog entries + the user's custom ones.

    Catalog entries — shipped ones, each possibly replaced by a same-named
    file in the user catalog, plus any user-only ones — come first, in
    :func:`~._catalog_files.catalog_sort_key` order (families A-Z, biggest
    quant first). ``custom_*`` entries follow in the order they were added.
    kodo-vsix renders the list in exactly this order.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        dict[str, LocalLLMEntry]: Map of entry name to :class:`LocalLLMEntry`,
        in display order.
    """
    catalog: dict[str, LocalLLMEntry] = {e.name: e for e in _catalog._HARDCODED_LOCAL_MODELS}
    user_entries, _ = _catalog.load_user_catalog(kodo_dir)
    for entry in user_entries:
        if entry.name in catalog:
            _log.debug("User catalog file replaces the shipped local LLM %r", entry.name)
        catalog[entry.name] = entry
    sidecars = _catalog.mtp_sidecars_by_family(kodo_dir)
    merged = {
        e.name: attach_mtp_sidecars(e, sidecars.get(e.base_llm, ()))
        for e in sorted(catalog.values(), key=catalog_sort_key)
    }
    external, _ = _load_external(kodo_dir)
    for entry in external:
        if entry.name in merged:
            _log.warning("Custom local LLM %r shadows a catalog entry — skipping", entry.name)
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
        ValueError: If ``entry.kind`` is not a custom kind, ``entry.name``
            already exists (hardcoded or custom), or it starts with the prefix
            reserved for MTP head downloads.
    """
    if entry.kind not in _CUSTOM_KINDS:
        raise ValueError(f"Cannot add a local LLM entry of kind {entry.kind!r}")
    if entry.name.startswith(MTP_SIDECAR_MODEL_ID_PREFIX):
        raise ValueError(
            f"A local LLM name may not start with {MTP_SIDECAR_MODEL_ID_PREFIX!r} — "
            "it is reserved for MTP head downloads"
        )
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
        ValueError: If *name* is a catalog entry (shipped, or a user catalog
            file — delete that file instead) or does not exist.
    """
    if any(e.name == name for e in _catalog._HARDCODED_LOCAL_MODELS):
        raise ValueError(f"{name!r} is a built-in local LLM and cannot be removed")
    user_entries, _ = _catalog.load_user_catalog(kodo_dir)
    if any(e.name == name for e in user_entries):
        raise ValueError(
            f"{name!r} is defined by a file in {_catalog.user_catalog_dir(kodo_dir)} — "
            "delete that file to remove it"
        )
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


def get_mtp_sidecars(kodo_dir: Path, base_llm: str) -> tuple[MtpSidecar, ...]:
    """The standalone MTP heads *base_llm*'s family offers, most precise first.

    The user catalog's ``mtp_sidecars.json`` for the family replaces the
    shipped one (see :mod:`._mtp_sidecars`).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        base_llm: The family. ``""`` (every ``custom_*`` entry) has none.

    Returns:
        tuple[MtpSidecar, ...]: The heads; ``()`` when the family has none.
    """
    if not base_llm:
        return ()
    return _catalog.mtp_sidecars_by_family(kodo_dir).get(base_llm, ())


def _user_catalog_unreliable(kodo_dir: Path, what: str) -> bool:
    """True when stored state cannot be judged against the registry right now.

    Either the registry file does not parse (every ``custom_*`` entry would
    look unknown) or a user catalog file — an entry or an MTP head list —
    failed to load (it might be the only definition of something the user
    has downloaded). Logs which, naming *what* is being skipped.
    """
    if _registry_file_unreadable(kodo_dir):
        _log.warning("local-llm-registry.json does not parse — skipping %s", what)
        return True
    _, entry_errors = _catalog.load_user_catalog(kodo_dir)
    _, sidecar_errors = _catalog.load_user_mtp_sidecars(kodo_dir)
    failed = len(entry_errors) + len(sidecar_errors)
    if failed:
        _log.warning("%d user local-LLM catalog file(s) failed to load — skipping %s", failed, what)
        return True
    return False


def stale_mtp_sidecar_model_ids(kodo_dir: Path, model_ids: Iterable[str]) -> tuple[str, ...]:
    """The MTP head downloads among *model_ids* that nothing needs any more.

    A head (download-record id
    :func:`~._mtp_sidecars.mtp_sidecar_model_id`) is kept for as long as at
    least one quant of its family has a download record — finished or not —
    since every quant can draft with it. It is stale once no id in
    *model_ids* is a registry entry of its family, or once its family no
    longer lists it (a head dropped or renamed in ``mtp_sidecars.json``).

    Like :func:`prune_unknown_model_state`, judges nothing — returns ``()`` —
    while a user catalog file is broken or the registry file does not parse.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        model_ids: Every download-record id the caller has, quants and heads.

    Returns:
        tuple[str, ...]: The head ids to delete, sorted.
    """
    ids = set(model_ids)
    heads = {model_id: parse_mtp_sidecar_model_id(model_id) for model_id in ids}
    if not any(heads.values()) or _user_catalog_unreliable(kodo_dir, "the MTP head cleanup"):
        return ()
    registry = get_local_registry(kodo_dir)
    families_in_use = {
        registry[model_id].base_llm
        for model_id in ids
        if model_id in registry and registry[model_id].base_llm
    }
    offered = _catalog.mtp_sidecars_by_family(kodo_dir)
    stale: list[str] = []
    for model_id, parsed in heads.items():
        if parsed is None:
            continue
        base_llm, sidecar_id = parsed
        listed = any(sidecar.id == sidecar_id for sidecar in offered.get(base_llm, ()))
        if base_llm not in families_in_use or not listed:
            stale.append(model_id)
    return tuple(sorted(stale))


def prune_unknown_model_state(
    kodo_dir: Path, installed_model_ids: Iterable[str] = ()
) -> tuple[str, ...]:
    """Forget every stored per-model setting whose model the registry no longer knows.

    The counterpart to :func:`remove_local_entry`'s cleanup, for the models
    *it* cannot reach: a ``hardcoded_hf`` entry that a kodo release renamed or
    dropped. Its ``profiles``/``active_profiles``/``knob_selections`` keys are
    keyed by entry name, so the old name keeps its knob selections and
    user-defined profiles in ``local-llm-registry.json`` forever, and a later
    release that reuses the name would silently inherit them.

    MTP head downloads among *installed_model_ids* are not models and are
    never judged here — :func:`stale_mtp_sidecar_model_ids` decides about
    those. Names in *installed_model_ids* that the registry does not know are
    reported in the return value even when they carry no stored settings —
    that is how the caller that owns the downloaded files
    (:func:`kodo.llms.llamacpp.purge_unknown_local_models`) learns which GGUFs
    are now unreachable. This function itself never touches a file on disk
    outside ``local-llm-registry.json``.

    Does nothing at all — and reports nothing to uninstall — when the registry
    file exists but does not parse: every ``custom_*`` entry would look
    unknown, and the purge would take the user's own models with it. The same
    goes for a user catalog (``~/.kodo/local_llms/``) with any file that failed
    to load: that file might be the only definition of a model the user has
    downloaded, and a typo must not cost them the GGUF.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        installed_model_ids: Model ids the caller has files for, to be judged
            against the registry alongside the stored settings.

    Returns:
        tuple[str, ...]: Every unknown name seen, sorted — those whose stored
        settings were just dropped plus those from *installed_model_ids*.
    """
    if _user_catalog_unreliable(kodo_dir, "the unknown-model purge"):
        return ()
    installed_models = {
        model_id for model_id in installed_model_ids if parse_mtp_sidecar_model_id(model_id) is None
    }
    known = set(get_local_registry(kodo_dir))
    data = _load_raw(kodo_dir)
    all_profiles = _all_profiles(data)
    active = _all_active_profiles(data)
    selections = _all_knob_selections(data)
    unknown = sorted((set(all_profiles) | set(active) | set(selections) | installed_models) - known)
    changed = False
    for name in unknown:
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
        _log.info("Dropped stored settings for unknown local models: %s", ", ".join(unknown))
    return tuple(unknown)


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
