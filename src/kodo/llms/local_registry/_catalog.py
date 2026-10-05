"""The local-LLM catalog: shipped JSON entry files plus the user's own.

Every ``hardcoded_hf`` entry is a JSON file in the format
:mod:`._catalog_files` defines, laid out as ``<root>/<base_llm>/<name>.json``
under one of two roots:

- :data:`BUILTIN_CATALOG_DIR` — ``catalog/`` inside this package, shipped
  with kodo. Loaded once, at import, into :data:`_HARDCODED_LOCAL_MODELS`;
  any invalid file is a hard startup failure, same as a malformed Python
  literal used to be. Add a model by adding its file (and, for a new family,
  a ``README.md`` beside it for whatever the JSON cannot say: which GGUF
  header keys were checked, why a knob is or is not offered, upstream PRs
  being waited on).
- :func:`user_catalog_dir` — ``~/.kodo/local_llms/``, user-editable. Read on
  every :func:`~kodo.llms.local_registry.get_local_registry` call (so an edit
  takes effect on the next registry push, no restart), and **a user file
  whose name matches a shipped entry replaces that entry outright** — copy a
  shipped file over and edit it. A file that fails to load is skipped with a
  warning (logged once per distinct problem) and the shipped entry of the
  same name, if any, stays in effect.

Each family's private knobs stay in code (:mod:`._knobs_table`); files name
them by id.

A family directory may also hold ``mtp_sidecars.json`` — its standalone MTP
heads (:mod:`._mtp_sidecars`). Shipped ones load at import into
:data:`_BUILTIN_MTP_SIDECARS`, with the same hard failure; a user file
replaces the shipped one for its family (:func:`mtp_sidecars_by_family`), and
a user directory may hold *only* that file, to give a shipped family heads.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ._catalog_files import (
    attach_mtp_sidecars,
    catalog_sort_key,
    scan_catalog_dir,
    scan_mtp_sidecars,
    validate_catalog_entry,
)
from ._mtp_sidecars import MTP_SIDECARS_FILENAME, MtpSidecar
from ._thinking import (
    GPT_OSS_REASONING_EFFORT_FAMILY,
    QWEN4EXP_REASONING_EFFORT_FAMILY,
    QWEN_REASONING_BUDGET_FAMILY,
    QWEN_TIER_TOKEN_BUDGETS,
)
from ._types import LocalLLMEntry

__all__ = [
    "BUILTIN_CATALOG_DIR",
    "load_user_catalog",
    "load_user_mtp_sidecars",
    "mtp_sidecars_by_family",
    "user_catalog_dir",
]

_log = logging.getLogger(__name__)

#: The shipped catalog root, ``<package>/catalog/<base_llm>/<name>.json``.
BUILTIN_CATALOG_DIR: Path = Path(__file__).parent / "catalog"


def user_catalog_dir(kodo_dir: Path) -> Path:
    """``~/.kodo/local_llms/`` — the user's catalog root, same layout as the shipped one.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        Path: ``<kodo_dir>/local_llms``. Need not exist; a missing directory is
        an empty user catalog.
    """
    return kodo_dir / "local_llms"


def _load_builtin_catalog() -> tuple[LocalLLMEntry, ...]:
    """Every shipped entry, in display order (:func:`~._catalog_files.catalog_sort_key`).

    Raises:
        ValueError: If any shipped file fails to load, or there are none at all
            (a wheel built without its JSON data).
    """
    entries, errors = scan_catalog_dir(BUILTIN_CATALOG_DIR)
    if errors:
        raise ValueError("The shipped local-LLM catalog is invalid:\n" + "\n".join(errors))
    if not entries:
        raise ValueError(f"No shipped local-LLM catalog entries under {BUILTIN_CATALOG_DIR}")
    return tuple(sorted(entries, key=catalog_sort_key))


_HARDCODED_LOCAL_MODELS: tuple[LocalLLMEntry, ...] = _load_builtin_catalog()


def _load_builtin_mtp_sidecars() -> dict[str, tuple[MtpSidecar, ...]]:
    """Every shipped family's heads, ``{base_llm: heads}``.

    Raises:
        ValueError: If any shipped ``mtp_sidecars.json`` fails to load.
    """
    found, errors = scan_mtp_sidecars(BUILTIN_CATALOG_DIR)
    if errors:
        raise ValueError("The shipped MTP sidecar lists are invalid:\n" + "\n".join(errors))
    return found


#: Shipped heads per family. Like :data:`_HARDCODED_LOCAL_MODELS`, a module
#: attribute tests may monkeypatch.
_BUILTIN_MTP_SIDECARS: dict[str, tuple[MtpSidecar, ...]] = _load_builtin_mtp_sidecars()

#: User-catalog problems already logged, so a broken file is reported once per
#: process rather than on every registry push.
_reported_user_catalog_errors: set[str] = set()


def load_user_catalog(kodo_dir: Path) -> tuple[list[LocalLLMEntry], list[str]]:
    """Every valid entry under :func:`user_catalog_dir`, plus what failed to load.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        tuple[list[LocalLLMEntry], list[str]]: The loaded entries (unsorted) and
        one message per file that was skipped. A non-empty error list is what
        makes :func:`~kodo.llms.local_registry.prune_unknown_model_state` stand
        down — see there.
    """
    entries, errors = scan_catalog_dir(user_catalog_dir(kodo_dir))
    _report_user_catalog_errors(errors)
    return entries, errors


def _report_user_catalog_errors(errors: list[str]) -> None:
    for message in errors:
        if message not in _reported_user_catalog_errors:
            _reported_user_catalog_errors.add(message)
            _log.warning("Skipping user local-LLM catalog file: %s", message)


def load_user_mtp_sidecars(kodo_dir: Path) -> tuple[dict[str, tuple[MtpSidecar, ...]], list[str]]:
    """Every valid ``mtp_sidecars.json`` under :func:`user_catalog_dir`, plus what failed.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        tuple[dict[str, tuple[MtpSidecar, ...]], list[str]]: ``{base_llm:
        heads}`` and one message per file that was skipped. A skipped file
        leaves the shipped heads of its family in effect, and — like a broken
        entry file — makes the startup purge stand down.
    """
    found, errors = scan_mtp_sidecars(user_catalog_dir(kodo_dir))
    _report_user_catalog_errors(errors)
    return found, errors


def mtp_sidecars_by_family(kodo_dir: Path) -> dict[str, tuple[MtpSidecar, ...]]:
    """The heads each family offers: the shipped lists, each replaced by a user file.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        dict[str, tuple[MtpSidecar, ...]]: ``{base_llm: heads}``, heads most
        precise first. A family a user file emptied maps to ``()``.
    """
    user, _ = load_user_mtp_sidecars(kodo_dir)
    return {**_BUILTIN_MTP_SIDECARS, **user}


def _validate_catalog() -> None:
    """Import-time checks across the whole shipped catalog.

    All hard failures at startup rather than mysteries at launch time. The
    user catalog (:func:`load_user_catalog`) gets checks 1 and 4 per file, as
    a skip rather than a failure, and is never held to check 3.

    1. **Per entry** (:func:`~._catalog_files.validate_catalog_entry`) — no
       two of its knobs own the same llama-server flag, every knob is
       structurally coherent, and every ``knob_defaults`` key names a knob the
       entry actually offers whose value is a real option.
    2. **Across entries** — two entries listing a knob under the same id must
       list the identical knob. Knob definitions are deduplicated by id into
       one table on the wire, so a same-id/different-definition pair would
       make one entry's Configure modal silently render the other's options.
       Entries loaded from files satisfy this by construction (every id
       resolves through :data:`~._knobs_table.KNOBS_BY_ID`); the check still
       guards anything that builds :class:`~._types.LocalLLMEntry` objects
       directly.
    3. **Against the thinking tables** — every ``base_llm`` slug named in
       :mod:`._thinking` still belongs to some shipped entry. Those
       tables are keyed by ``base_llm`` strings that nothing else re-checks,
       so renaming or dropping a model family (its catalog directory) leaves a
       slug behind that matches nothing and silently strips that family's
       reasoning tiers — the model keeps working, just with thinking quietly
       unavailable.
    4. **``mtp_supported`` against the knob** — part of check 1: an entry's
       :attr:`~kodo.llms.local_registry.LocalLLMEntry.mtp_supported` must
       agree with whether its knobs offer the built-in MTP layers
       (:func:`~._catalog_files.offers_builtin_mtp`). Without this, the
       boolean could drift from what's actually wired — set without the knob
       (a claim the UI never backs up) or the knob added without the flag
       (silently missing from whatever, in the future, reads the flag instead
       of the knob list). Checks 1, 2 and 4 run on every entry both as its
       file declares it and with its family's MTP head picker attached
       (:func:`~._catalog_files.attach_mtp_sidecars`).
    5. **MTP sidecar lists have a family** — every shipped
       ``mtp_sidecars.json`` sits in a directory with at least one shipped
       entry, so a misspelled directory name cannot leave its heads silently
       unreachable.

    Note that this validates *code against code*; the mirror-image cleanup of
    a user's stored per-model state after a rename or removal is
    :func:`~kodo.llms.local_registry.prune_unknown_model_state`, which runs
    once per server start rather than at import.
    """
    known: dict[str, object] = {}
    attached = tuple(
        attach_mtp_sidecars(entry, _BUILTIN_MTP_SIDECARS.get(entry.base_llm, ()))
        for entry in _HARDCODED_LOCAL_MODELS
    )
    for entry in _HARDCODED_LOCAL_MODELS + attached:
        validate_catalog_entry(entry)
        for knob in entry.knobs:
            previous = known.setdefault(knob.id, knob)
            if previous != knob:
                raise ValueError(
                    f"{entry.name}: knob {knob.id!r} differs from the definition another "
                    "entry uses under the same id"
                )
    base_llms = {entry.base_llm for entry in _HARDCODED_LOCAL_MODELS}
    orphaned = sorted(set(_BUILTIN_MTP_SIDECARS) - base_llms)
    if orphaned:
        raise ValueError(
            f"{MTP_SIDECARS_FILENAME} in catalog directories with no entries: {', '.join(orphaned)}"
        )
    tiered = (
        QWEN_REASONING_BUDGET_FAMILY
        | GPT_OSS_REASONING_EFFORT_FAMILY
        | QWEN4EXP_REASONING_EFFORT_FAMILY
        | frozenset(QWEN_TIER_TOKEN_BUDGETS)
    )
    stale = sorted(tiered - base_llms)
    if stale:
        raise ValueError(
            "_thinking.py names base_llm slugs no entry in the catalog has: "
            f"{', '.join(stale)} — drop them there, or fix the spelling to match the "
            "renamed family"
        )


_validate_catalog()
