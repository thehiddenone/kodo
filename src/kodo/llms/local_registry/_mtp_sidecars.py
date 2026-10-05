"""MTP sidecar heads: standalone draft-head GGUFs shared by every quant of one model.

Some quant repos ship a model's Multi-Token Prediction head as its own small
GGUF (unsloth puts them in an ``MTP/`` subfolder) instead of, or as well as,
baking the layers into each quant. llama.cpp loads one with ``--model-draft
<head> --spec-type draft-mtp``. A head is quantized independently of the model
it drafts for, so **any quant of a model works with any of its heads** —
llama.cpp rejects a head built for a different model outright rather than
producing bad output.

A family offers its heads through one optional file beside its entries,
``<root>/<base_llm>/mtp_sidecars.json``, whose body maps a stable head id to
the head's download coordinates::

    {
      "q8_0": {
        "repo_id": "unsloth/Qwen3.8-Flash-Next-GGUF",
        "filename": "MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf",
        "quant_type": "Q8_0",
        "size_hint": "4.14 GB",
        "llamacpp_version": 11330
      }
    }

``repo_id``, ``filename`` and ``quant_type`` are required; ``size_hint`` and
``llamacpp_version`` (the oldest llama.cpp build that loads this head — ``0``
for any) are optional. Unknown keys are an error, as in an entry file. The
head id is persisted twice — as a knob option id in the user's selection, and
inside the head's download record id (:func:`mtp_sidecar_model_id`) — so it is
an identifier, not a label: renaming it orphans both.

Only **self-contained** heads belong here: ones that carry their own token
embedding and output projection. unsloth's ``shared-*`` heads borrow those
tensors from the running model (``<arch>.nextn_shared_target_tensors``), which
needs cross-model tensor borrowing that mainline llama.cpp does not have.

The same file in the user catalog (``~/.kodo/local_llms/<base_llm>/``)
replaces the shipped one for that family outright, like an entry file does; an
empty object there switches a family's shipped heads off.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MTP_SIDECARS_FILENAME",
    "MTP_SIDECAR_MODEL_ID_PREFIX",
    "MtpSidecar",
    "load_mtp_sidecars_file",
    "mtp_sidecar_model_id",
    "parse_mtp_sidecar_model_id",
    "quant_precision_bits",
]

#: The reserved file name a family's heads live in. It never loads as an entry.
MTP_SIDECARS_FILENAME = "mtp_sidecars.json"

#: Download-record ids of heads start with this, so they can never collide with
#: an entry name (catalog files and custom entries are refused a name that
#: starts with it).
MTP_SIDECAR_MODEL_ID_PREFIX = "mtp-head:"

#: Option ids the MTP head knob uses for itself (:mod:`._knobs_mtp`).
_RESERVED_HEAD_IDS = frozenset({"off", "builtin"})

_HEAD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_REQUIRED: tuple[str, ...] = ("repo_id", "filename", "quant_type")
_KNOWN_KEYS = frozenset(_REQUIRED + ("size_hint", "llamacpp_version"))

#: ``BF16``/``F16``/``F32``/``FP8``/``MXFP4``, then ``Q8_0``/``IQ4_XS``/``UD-Q4_K_XL``.
_FLOAT_BITS = re.compile(r"(?:BF|FP|F)(\d+)")
_INT_BITS = re.compile(r"I?Q(\d)")


@dataclass(frozen=True)
class MtpSidecar:
    """One downloadable MTP draft head for a model family.

    Attributes:
        id: Stable slug, unique within the family — the key in
            ``mtp_sidecars.json``. Becomes a knob option id and part of the
            head's download-record id.
        repo_id: HuggingFace repository the head is downloaded from.
        filename: The head's path inside *repo_id* (may include a subfolder).
        quant_type: The head's own quantization, e.g. ``'Q8_0'`` —
            independent of whichever quant of the model is running. Decides
            the display order (:func:`quant_precision_bits`).
        size_hint: Human-readable file size as HF lists it, e.g. ``'4.14 GB'``.
        llamacpp_version: Oldest llama.cpp build that loads this head; ``0``
            means any. Checked at launch, not at selection: an older build
            launches without speculative decoding (see
            :func:`kodo.llms.llamacpp.resolve_llama_launch`).
    """

    id: str
    repo_id: str
    filename: str
    quant_type: str
    size_hint: str = ""
    llamacpp_version: int = 0


def quant_precision_bits(quant_type: str) -> int:
    """The bits per weight *quant_type* names — the "more precise first" rank.

    ``BF16``/``F16`` -> 16, ``F32`` -> 32, ``Q8_0`` -> 8, ``UD-Q4_K_XL`` ->
    4, ``IQ2_XXS`` -> 2, ``MXFP4`` -> 4.

    Raises:
        ValueError: If no bit width can be read off *quant_type*.
    """
    upper = quant_type.upper()
    match = _FLOAT_BITS.search(upper) or _INT_BITS.search(upper)
    if match is None:
        raise ValueError(f"cannot tell the precision of quant_type {quant_type!r}")
    return int(match.group(1))


def _sort_key(sidecar: MtpSidecar) -> tuple[int, str, str]:
    """Most precise first; ties by quant type, then id, so the order never depends on the file."""
    return (-quant_precision_bits(sidecar.quant_type), sidecar.quant_type.casefold(), sidecar.id)


def _sidecar_from_json(head_id: object, raw: object) -> MtpSidecar:
    if not isinstance(head_id, str) or not _HEAD_ID.match(head_id):
        raise ValueError(
            f"head id {head_id!r} must be a lowercase slug (letters, digits, '.', '_', '-')"
        )
    if head_id in _RESERVED_HEAD_IDS:
        raise ValueError(f"head id {head_id!r} is reserved by the MTP knob")
    where = f"head {head_id!r}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a JSON object")
    unknown = sorted(str(k) for k in raw.keys() - _KNOWN_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}")
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise ValueError(f"{where}: missing required key(s) {', '.join(map(repr, missing))}")
    strs: dict[str, str] = {}
    for key in _REQUIRED + ("size_hint",):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"{where}: {key!r} must be a string")
        if key in _REQUIRED and not value.strip():
            raise ValueError(f"{where}: {key!r} must not be empty")
        strs[key] = value
    version = raw.get("llamacpp_version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise ValueError(f"{where}: 'llamacpp_version' must be a non-negative integer")
    try:
        quant_precision_bits(strs["quant_type"])
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None
    return MtpSidecar(
        id=head_id,
        repo_id=strs["repo_id"],
        filename=strs["filename"],
        quant_type=strs["quant_type"],
        size_hint=strs["size_hint"],
        llamacpp_version=version,
    )


def load_mtp_sidecars_file(path: Path) -> tuple[MtpSidecar, ...]:
    """Read and validate one ``<root>/<base_llm>/mtp_sidecars.json``.

    Args:
        path: The file.

    Returns:
        tuple[MtpSidecar, ...]: The family's heads, most precise first
        (:func:`quant_precision_bits`). Empty for an empty object.

    Raises:
        ValueError: If the file cannot be read, is not valid JSON, or does not
            match the format in the module docstring.
    """
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the file must contain a JSON object mapping head ids to heads")
    sidecars = [_sidecar_from_json(head_id, body) for head_id, body in raw.items()]
    return tuple(sorted(sidecars, key=_sort_key))


def mtp_sidecar_model_id(base_llm: str, sidecar_id: str) -> str:
    """The download-record id a head of *base_llm* is stored under.

    Heads are downloaded through the same
    :class:`~kodo.llms.local.LocalModelManager` as model quants, as records of
    their own — one per head, shared by every quant of the family rather than
    copied into each quant's directory.
    """
    return f"{MTP_SIDECAR_MODEL_ID_PREFIX}{base_llm}:{sidecar_id}"


def parse_mtp_sidecar_model_id(model_id: str) -> tuple[str, str] | None:
    """``(base_llm, sidecar_id)`` for a head's download-record id, else ``None``.

    The inverse of :func:`mtp_sidecar_model_id`. A head id is a slug and never
    contains ``:``, so splitting at the last one is unambiguous.
    """
    if not model_id.startswith(MTP_SIDECAR_MODEL_ID_PREFIX):
        return None
    base_llm, sep, sidecar_id = model_id[len(MTP_SIDECAR_MODEL_ID_PREFIX) :].rpartition(":")
    if not sep or not base_llm or not sidecar_id:
        return None
    return base_llm, sidecar_id
