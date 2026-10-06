"""Writing the user catalog: one entry file, or one family's ``mtp_sidecars.json``.

Everything the loader reads (:mod:`._catalog_files`, :mod:`._mtp_sidecars`)
can also be written, so a tool can add a model without a hand-edited file.
Both writers validate the body with the loader's own parser *before* touching
the disk, write it in the shipped files' canonical form, and land it with an
atomic rename from a hidden temporary file — the scan skips names starting
with ``.``, so a concurrent ``get_local_registry`` never sees half a file.

Only the user root (:func:`~._catalog.user_catalog_dir`) is ever written; the
shipped catalog is code. Whether a write is *wise* — shadowing a shipped entry,
duplicating a quant another entry already serves — is the caller's policy, not
this module's: here a name is refused only when a user file already uses it,
since two user files with one stem make the scan load neither.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ._catalog import user_catalog_dir
from ._catalog_files import entry_to_catalog_json, parse_catalog_entry
from ._mtp_sidecars import (
    MTP_SIDECARS_FILENAME,
    MtpSidecar,
    mtp_sidecars_to_json,
    parse_mtp_sidecars,
)
from ._types import LocalLLMEntry

__all__ = [
    "BASE_LLM_PATTERN",
    "ENTRY_NAME_PATTERN",
    "user_catalog_entry_path",
    "write_user_catalog_entry",
    "write_user_mtp_sidecars",
]

#: A family directory name: ``Qwen38-27B``, ``Gemma4-26B-A4B``, ``Nanbeige4.2-3B``.
BASE_LLM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: An entry name (file stem): ``unsloth-qwen38-27b-ud-q4-k-xl``.
ENTRY_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def user_catalog_entry_path(kodo_dir: Path, name: str) -> Path | None:
    """Find the user catalog file whose stem is *name*, in any family directory.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        name: An entry name.

    Returns:
        Path | None: The first ``<root>/<family>/<name>.json`` found, or
        ``None``. Broken files count — a file that fails to load still blocks
        its name.
    """
    root = user_catalog_dir(kodo_dir)
    if not root.is_dir():
        return None
    for family in sorted(root.iterdir()):
        candidate = family / f"{name}.json"
        if family.is_dir() and candidate.is_file():
            return candidate
    return None


def _write_json_atomically(path: Path, body: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_user_catalog_entry(
    kodo_dir: Path, raw: dict[str, object], *, base_llm: str, name: str
) -> tuple[LocalLLMEntry, Path]:
    """Validate *raw* as a catalog file body and write it to ``<user root>/<base_llm>/<name>.json``.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        raw: The file body — the format of :mod:`._catalog_files`, without
            ``name``/``base_llm``.
        base_llm: The family directory; must match :data:`BASE_LLM_PATTERN`.
        name: The entry name; must match :data:`ENTRY_NAME_PATTERN`.

    Returns:
        tuple[LocalLLMEntry, Path]: The entry as the loader will read it back,
        and the file written.

    Raises:
        ValueError: A malformed *base_llm* or *name*, a name a user file
            already uses, or a body the loader would reject. Nothing is
            written in any of these cases.
    """
    if not BASE_LLM_PATTERN.match(base_llm):
        raise ValueError(
            f"base_llm {base_llm!r} must start with a letter or digit and contain only "
            "letters, digits, '.', '_' and '-'"
        )
    if not ENTRY_NAME_PATTERN.match(name):
        raise ValueError(
            f"name {name!r} must start with a lowercase letter or digit and contain only "
            "lowercase letters, digits, '.' and '-'"
        )
    existing = user_catalog_entry_path(kodo_dir, name)
    if existing is not None:
        raise ValueError(f"the user catalog already has an entry named {name!r} ({existing})")
    entry = parse_catalog_entry(raw, base_llm=base_llm, name=name)
    path = user_catalog_dir(kodo_dir) / base_llm / f"{name}.json"
    _write_json_atomically(path, entry_to_catalog_json(entry))
    return entry, path


def write_user_mtp_sidecars(
    kodo_dir: Path, base_llm: str, sidecars: tuple[MtpSidecar, ...]
) -> Path:
    """Write *sidecars* as the family's ``mtp_sidecars.json`` in the user catalog.

    The file replaces the shipped heads of the family outright (see
    :mod:`._mtp_sidecars`), so pass the complete list the family should offer.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        base_llm: The family directory; must match :data:`BASE_LLM_PATTERN`.
        sidecars: Every head the family offers, in file order.

    Returns:
        Path: The file written.

    Raises:
        ValueError: A malformed *base_llm*, two heads sharing an id, or a head
            the loader would reject. Nothing is written in any of these cases.
    """
    if not BASE_LLM_PATTERN.match(base_llm):
        raise ValueError(f"base_llm {base_llm!r} is not a valid family directory name")
    ids = [sidecar.id for sidecar in sidecars]
    duplicates = sorted({head_id for head_id in ids if ids.count(head_id) > 1})
    if duplicates:
        raise ValueError(f"head id(s) used more than once: {', '.join(duplicates)}")
    body = mtp_sidecars_to_json(sidecars)
    parse_mtp_sidecars(body)
    path = user_catalog_dir(kodo_dir) / base_llm / MTP_SIDECARS_FILENAME
    _write_json_atomically(path, body)
    return path
