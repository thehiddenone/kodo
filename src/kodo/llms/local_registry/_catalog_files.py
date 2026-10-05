"""The catalog-entry JSON file format: one ``hardcoded_hf`` entry per file.

Both catalog locations share one layout and one format:

- **shipped** — ``catalog/`` inside this package (see :mod:`._catalog`);
- **user** — ``~/.kodo/local_llms/`` (see
  :func:`~kodo.llms.local_registry.user_catalog_dir`).

Layout: ``<root>/<base_llm>/<name>.json``. The **path is the identity**: the
directory name *is* the entry's ``base_llm`` and the file stem *is* its
``name``, so neither is stored in the file (a file that carries either key is
rejected — two sources of truth for one field would eventually disagree).
Renaming a file therefore renames the entry, with everything that implies for
state keyed by name (doc/LLM_REGISTRY.md §4.1b).

Every file describes a ``hardcoded_hf`` entry — ``kind`` is implied, never
stored. The user-added ``custom_*`` kinds keep living in
``~/.kodo/etc/local-llm-registry.json`` (:mod:`._io`), which this format has
nothing to do with.

Body: a JSON object whose keys are the remaining :class:`~._types.LocalLLMEntry`
fields. ``repo_id``, ``filename`` and ``knobs`` are required, every other key
is optional and defaults to the dataclass's own default. Two keys are not
plain copies of the dataclass field:

- ``knobs`` — a list of knob **ids**, resolved through
  :data:`~._knobs_table.KNOBS_BY_ID`. Knobs are code; a file only chooses
  among them.
- ``base_llama_args`` — layered **over**
  :data:`~._knobs_shared.BASE_LLAMA_ARGS` rather than replacing it, the same
  rule ``custom_*`` entries follow, so an entry cannot lose ``--jinja`` by
  forgetting to repeat it. Omitted when an entry adds nothing.

Unknown keys are an error rather than ignored, so a typo (``"descripton"``)
fails loudly instead of silently leaving the field at its default.

One file name is reserved: ``<root>/<base_llm>/mtp_sidecars.json`` is not an
entry but the family's list of standalone MTP heads (format:
:mod:`._mtp_sidecars`), read by :func:`scan_mtp_sidecars`. A family with heads
gets the head-picker knob on every entry (:func:`attach_mtp_sidecars`)
without any entry file naming it.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from ._knobs import LlamaKnob, validate_knobs
from ._knobs_mtp import (
    MTP_BUILTIN_OPTION_ID,
    MTP_SPEC_DECODE_KNOB,
    is_mtp_head_knob,
    make_mtp_head_knob,
)
from ._knobs_shared import BASE_LLAMA_ARGS
from ._knobs_table import KNOBS_BY_ID
from ._mtp_sidecars import (
    MTP_SIDECAR_MODEL_ID_PREFIX,
    MTP_SIDECARS_FILENAME,
    MtpSidecar,
    load_mtp_sidecars_file,
)
from ._types import LocalLLMEntry

__all__ = [
    "CATALOG_ENTRY_KIND",
    "attach_mtp_sidecars",
    "catalog_sort_key",
    "entry_to_catalog_json",
    "load_catalog_file",
    "offers_builtin_mtp",
    "scan_catalog_dir",
    "scan_mtp_sidecars",
    "validate_catalog_entry",
]

#: The ``kind`` of every entry loaded from a catalog file, shipped or user.
#: Kept as ``hardcoded_hf`` (rather than a new kind) so the wire shape and
#: kodo-vsix's install/download handling are unchanged by where an entry came
#: from.
CATALOG_ENTRY_KIND = "hardcoded_hf"

_STR_FIELDS: tuple[str, ...] = (
    "description",
    "repo_id",
    "filename",
    "quant_author",
    "quant_type",
    "size_hint",
    "llm_author",
    "license_name",
    "license_url",
    "gpu_tip",
    "mac_tip",
)
_INT_FIELDS: tuple[str, ...] = ("context_window", "llamacpp_version", "min_memory", "memory")
_BOOL_FIELDS: tuple[str, ...] = ("mtp_supported",)
_STR_MAP_FIELDS: tuple[str, ...] = ("knob_defaults", "base_llama_args")
_REQUIRED: tuple[str, ...] = ("repo_id", "filename", "knobs")
_KNOWN_KEYS = frozenset(_STR_FIELDS + _INT_FIELDS + _BOOL_FIELDS + _STR_MAP_FIELDS + ("knobs",))

#: Fields the path supplies — see the module docstring.
_PATH_DERIVED_KEYS = frozenset({"name", "base_llm"})

_SIZE_HINT = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(KB|MB|GB|TB)\s*$", re.IGNORECASE)
_SIZE_UNIT_GB = {"KB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1e3}


def _str_map(raw: object, key: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ValueError(f"{key!r} must be an object mapping strings to strings")
    return {str(k): str(v) for k, v in raw.items()}


def _knob_ids(raw: object) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("'knobs' must be an array of knob id strings")
    unknown = [str(item) for item in raw if item not in KNOBS_BY_ID]
    if unknown:
        raise ValueError(
            f"'knobs' names unknown knob id(s) {', '.join(map(repr, unknown))}; "
            f"known ids: {', '.join(sorted(KNOBS_BY_ID))}"
        )
    return [str(item) for item in raw]


def _entry_from_catalog_json(raw: object, *, base_llm: str, name: str) -> LocalLLMEntry:
    """Build the entry a catalog file's parsed JSON body describes.

    Raises:
        ValueError: On any shape problem — see the module docstring.
    """
    if not isinstance(raw, dict):
        raise ValueError("the file must contain a JSON object")
    if name.startswith(MTP_SIDECAR_MODEL_ID_PREFIX):
        raise ValueError(
            f"an entry name may not start with {MTP_SIDECAR_MODEL_ID_PREFIX!r} — "
            "that prefix is reserved for MTP head downloads"
        )
    derived = sorted(_PATH_DERIVED_KEYS & raw.keys())
    if derived:
        raise ValueError(
            f"{', '.join(map(repr, derived))} must not appear in the file — the entry's "
            "base_llm is its directory name and its name is the file name"
        )
    unknown = sorted(str(k) for k in raw.keys() - _KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(map(repr, unknown))}")
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ValueError(f"missing required key(s) {', '.join(map(repr, missing))}")

    strs: dict[str, str] = {}
    for key in _STR_FIELDS:
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{key!r} must be a string")
        strs[key] = value
    for key in ("repo_id", "filename"):
        if not strs[key].strip():
            raise ValueError(f"{key!r} must not be empty")
    ints: dict[str, int] = {}
    for key in _INT_FIELDS:
        value = raw.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key!r} must be a non-negative integer")
        ints[key] = value
    mtp_supported = raw.get("mtp_supported", False)
    if not isinstance(mtp_supported, bool):
        raise ValueError("'mtp_supported' must be true or false")

    return LocalLLMEntry(
        name=name,
        kind=CATALOG_ENTRY_KIND,
        base_llm=base_llm,
        knobs=tuple(KNOBS_BY_ID[knob_id] for knob_id in _knob_ids(raw["knobs"])),
        knob_defaults=_str_map(raw.get("knob_defaults", {}), "knob_defaults"),
        base_llama_args={
            **BASE_LLAMA_ARGS,
            **_str_map(raw.get("base_llama_args", {}), "base_llama_args"),
        },
        mtp_supported=mtp_supported,
        description=strs["description"],
        repo_id=strs["repo_id"],
        filename=strs["filename"],
        quant_author=strs["quant_author"],
        quant_type=strs["quant_type"],
        size_hint=strs["size_hint"],
        llm_author=strs["llm_author"],
        license_name=strs["license_name"],
        license_url=strs["license_url"],
        gpu_tip=strs["gpu_tip"],
        mac_tip=strs["mac_tip"],
        context_window=ints["context_window"],
        llamacpp_version=ints["llamacpp_version"],
        min_memory=ints["min_memory"],
        memory=ints["memory"],
    )


def entry_to_catalog_json(entry: LocalLLMEntry) -> dict[str, object]:
    """The catalog-file body that :func:`load_catalog_file` reads back as *entry*.

    ``name`` and ``base_llm`` are left out — they are the file's path.

    Raises:
        ValueError: If *entry* cannot be expressed as a catalog file: it is not
            a ``hardcoded_hf`` entry, it offers a knob that is not in
            :data:`~._knobs_table.KNOBS_BY_ID`, or its ``base_llama_args`` drop
            one of the shared base args (a file can only add to them).
    """
    if entry.kind != CATALOG_ENTRY_KIND:
        raise ValueError(f"{entry.name}: only {CATALOG_ENTRY_KIND} entries live in catalog files")
    for knob in entry.knobs:
        if KNOBS_BY_ID.get(knob.id) != knob:
            raise ValueError(f"{entry.name}: knob {knob.id!r} is not in the knob table")
    dropped = sorted(BASE_LLAMA_ARGS.keys() - entry.base_llama_args.keys())
    if dropped:
        raise ValueError(f"{entry.name}: base_llama_args drops shared base arg(s) {dropped}")
    body: dict[str, object] = {key: getattr(entry, key) for key in _STR_FIELDS}
    body.update({key: getattr(entry, key) for key in _INT_FIELDS})
    body["mtp_supported"] = entry.mtp_supported
    body["knobs"] = [knob.id for knob in entry.knobs]
    body["knob_defaults"] = dict(entry.knob_defaults)
    extra_args = {
        flag: value
        for flag, value in entry.base_llama_args.items()
        if BASE_LLAMA_ARGS.get(flag) != value
    }
    if extra_args:
        body["base_llama_args"] = extra_args
    return body


def offers_builtin_mtp(knobs: tuple[LlamaKnob, ...]) -> bool:
    """Whether *knobs* let the user draft with the GGUF's own MTP layers.

    Either the built-in checkbox (:data:`~._knobs_mtp.MTP_SPEC_DECODE_KNOB`)
    or a head picker with a *Built-in* option
    (:func:`~._knobs_mtp.make_mtp_head_knob`).
    """
    return any(
        knob.id == MTP_SPEC_DECODE_KNOB.id
        or (is_mtp_head_knob(knob) and knob.option(MTP_BUILTIN_OPTION_ID) is not None)
        for knob in knobs
    )


def validate_catalog_entry(entry: LocalLLMEntry) -> None:
    """Per-entry knob checks every catalog entry must pass, shipped or user.

    1. Its knob set is legal (:func:`~._knobs.validate_knobs`): no two knobs
       own the same llama-server flag, and each knob is coherent.
    2. ``mtp_supported`` agrees with whether its knobs offer the built-in MTP
       layers (:func:`offers_builtin_mtp`) — without this the boolean could
       drift from what is actually wired. Holds both for an entry as its file
       declares it and after :func:`attach_mtp_sidecars`.
    3. Every ``knob_defaults`` key names a knob the entry offers, and every
       value is one of that knob's options.

    Raises:
        ValueError: On the first failed check.
    """
    validate_knobs(entry.knobs, context=entry.name)
    has_mtp_knob = offers_builtin_mtp(entry.knobs)
    if entry.mtp_supported != has_mtp_knob:
        raise ValueError(
            f"{entry.name}: mtp_supported={entry.mtp_supported!r} but "
            f"{'lists' if has_mtp_knob else 'does not list'} the "
            f"{MTP_SPEC_DECODE_KNOB.id!r} knob — the two must agree"
        )
    by_id = {knob.id: knob for knob in entry.knobs}
    for knob_id, selection in entry.knob_defaults.items():
        knob = by_id.get(knob_id)
        if knob is None:
            raise ValueError(
                f"{entry.name}: knob_defaults names {knob_id!r}, which this entry does not offer"
            )
        if knob.options and knob.option(selection) is None:
            raise ValueError(
                f"{entry.name}: knob_defaults sets {knob_id!r} to {selection!r}, "
                "which is not one of its options"
            )


def attach_mtp_sidecars(entry: LocalLLMEntry, sidecars: tuple[MtpSidecar, ...]) -> LocalLLMEntry:
    """*entry* with its family's head picker in place of the built-in MTP checkbox.

    The picker (:func:`~._knobs_mtp.make_mtp_head_knob`) takes the checkbox's
    position in ``knobs`` when the entry lists it, and is appended otherwise —
    an entry whose own GGUF has no MTP layers still drafts with a head. A
    ``knob_defaults`` value for the checkbox carries over (``"on"`` becomes
    the *Built-in* option).

    Args:
        entry: A catalog entry as its file declares it.
        sidecars: Its family's heads, in display order. Empty leaves *entry*
            unchanged.

    Returns:
        LocalLLMEntry: The entry to serve.
    """
    if not sidecars:
        return entry
    picker = make_mtp_head_knob(entry.base_llm, sidecars, builtin=entry.mtp_supported)
    knobs = list(entry.knobs)
    position = next((i for i, knob in enumerate(knobs) if knob.id == MTP_SPEC_DECODE_KNOB.id), None)
    if position is None:
        knobs.append(picker)
    else:
        knobs[position] = picker
    defaults = dict(entry.knob_defaults)
    checkbox_default = defaults.pop(MTP_SPEC_DECODE_KNOB.id, None)
    if checkbox_default == "on" and entry.mtp_supported:
        defaults[picker.id] = MTP_BUILTIN_OPTION_ID
    return replace(entry, knobs=tuple(knobs), knob_defaults=defaults)


def load_catalog_file(path: Path) -> LocalLLMEntry:
    """Read and validate one ``<root>/<base_llm>/<name>.json`` catalog file.

    Args:
        path: The file. Its parent directory's name becomes the entry's
            ``base_llm`` and its stem the entry's ``name``.

    Returns:
        LocalLLMEntry: The validated ``hardcoded_hf`` entry.

    Raises:
        ValueError: If the file cannot be read, is not valid JSON, does not
            match the format (see the module docstring), or fails
            :func:`validate_catalog_entry`.
    """
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read: {exc}") from exc
    entry = _entry_from_catalog_json(raw, base_llm=path.parent.name, name=path.stem)
    validate_catalog_entry(entry)
    return entry


def scan_catalog_dir(root: Path) -> tuple[list[LocalLLMEntry], list[str]]:
    """Load every ``<root>/<base_llm>/<name>.json`` file under *root*.

    Hidden files and directories (a leading ``.``) and non-``.json`` files are
    skipped, so a family's ``README.md`` can sit beside its entries — and so
    is ``mtp_sidecars.json``, which :func:`scan_mtp_sidecars` reads. A
    ``.json`` directly under *root*, outside any ``<base_llm>/`` directory,
    is reported as an error rather than skipped silently — it was almost
    certainly meant to be an entry.

    Two files with the same stem in different directories would define one
    entry twice; neither is loaded and both are reported.

    Args:
        root: The catalog root. A missing directory is an empty catalog, not an
            error.

    Returns:
        tuple[list[LocalLLMEntry], list[str]]: The entries that loaded (in no
        particular order — sort with :func:`catalog_sort_key`), and one
        human-readable message per file that did not.
    """
    if not root.is_dir():
        return [], []
    errors: list[str] = []
    found: dict[str, list[tuple[Path, LocalLLMEntry]]] = {}
    try:
        model_dirs = sorted(root.iterdir())
    except OSError as exc:
        return [], [f"{root}: cannot list: {exc}"]
    for model_dir in model_dirs:
        if model_dir.name.startswith("."):
            continue
        if model_dir.is_file():
            if model_dir.suffix == ".json":
                errors.append(
                    f"{model_dir}: catalog files go in a model directory, "
                    f"<root>/<base_llm>/{model_dir.name}"
                )
            continue
        try:
            files = sorted(model_dir.iterdir())
        except OSError as exc:
            errors.append(f"{model_dir}: cannot list: {exc}")
            continue
        for path in files:
            if (
                path.name.startswith(".")
                or path.suffix != ".json"
                or path.name == MTP_SIDECARS_FILENAME
                or not path.is_file()
            ):
                continue
            try:
                entry = load_catalog_file(path)
            except ValueError as exc:
                errors.append(f"{path}: {exc}")
                continue
            found.setdefault(entry.name, []).append((path, entry))
    entries: list[LocalLLMEntry] = []
    for name, sources in found.items():
        if len(sources) > 1:
            paths = ", ".join(str(path) for path, _ in sources)
            errors.append(f"{name!r} is defined by more than one file ({paths}); none is loaded")
            continue
        entries.append(sources[0][1])
    return entries, errors


def scan_mtp_sidecars(root: Path) -> tuple[dict[str, tuple[MtpSidecar, ...]], list[str]]:
    """Load every ``<root>/<base_llm>/mtp_sidecars.json`` under *root*.

    Args:
        root: The catalog root. A missing directory has no heads.

    Returns:
        tuple[dict[str, tuple[MtpSidecar, ...]], list[str]]: ``{base_llm:
        heads}`` for every file that loaded — including an empty tuple for an
        empty file, which is how a user file switches a shipped family's heads
        off — and one human-readable message per file that did not.
    """
    if not root.is_dir():
        return {}, []
    try:
        model_dirs = sorted(root.iterdir())
    except OSError as exc:
        return {}, [f"{root}: cannot list: {exc}"]
    found: dict[str, tuple[MtpSidecar, ...]] = {}
    errors: list[str] = []
    for model_dir in model_dirs:
        path = model_dir / MTP_SIDECARS_FILENAME
        if model_dir.name.startswith(".") or not path.is_file():
            continue
        try:
            found[model_dir.name] = load_mtp_sidecars_file(path)
        except ValueError as exc:
            errors.append(f"{path}: {exc}")
    return found, errors


def _size_gb(size_hint: str) -> float:
    """``"54.7 GB"`` -> ``54.7``; ``-1.0`` when *size_hint* does not parse."""
    match = _SIZE_HINT.match(size_hint)
    if match is None:
        return -1.0
    return float(match.group(1)) * _SIZE_UNIT_GB[match.group(2).upper()]


def catalog_sort_key(entry: LocalLLMEntry) -> tuple[str, str, float, str]:
    """Display order for catalog entries: families A-Z, then biggest quant first.

    Families sort case-insensitively by ``base_llm``; within one, entries go by
    ``size_hint`` descending (an unparseable or empty hint sorts last), with
    the entry name as the final tie-break so the order never depends on
    directory-listing order. kodo-vsix renders entries — and groups them by
    ``base_llm`` — in exactly the order the server sends them.
    """
    return (entry.base_llm.casefold(), entry.base_llm, -_size_gb(entry.size_hint), entry.name)
