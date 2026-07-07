"""Local LLM registry: hardcoded GGUFs plus a user-managed external collection.

Every entry here runs on llama.cpp — there is no ``residence`` field any more
(the old flat registry's cloud/local split lives in
:mod:`kodo.llms._cloud_registry` now). Entries are discriminated by ``kind``:

- ``hardcoded_hf`` — compiled-in HuggingFace GGUF, shipped with kodo.
- ``custom_hf`` — user-added HuggingFace GGUF (same shape as ``hardcoded_hf``,
  added via the "Add local LLM from huggingface.com" flow). Has an
  installed/not-installed state, resolved the same way as ``hardcoded_hf``
  (presence in ``~/.kodo/etc/local-llm-index.json``, see
  :mod:`kodo.llms.llamacpp._downloader`).
- ``custom_file`` — user-added local GGUF file that kodo does not own or copy.
  "Installed" means the file exists on disk; per design this is checked once,
  by the kodo-vsix extension, at its own startup — not re-verified here.
- ``custom_server_url`` — user-added link to an already-running llama.cpp (or
  OpenAI-compatible) server kodo does not manage. Always considered
  installed; selecting it as active stops kodo's own managed llama-server
  (see :mod:`kodo.llms.llamacpp._llama`).

The external collection (``custom_*`` entries) plus the global llama-server
binary override path are persisted in ``~/.kodo/etc/local-llm-registry.json``,
owned (read + written) entirely by this module — the kodo-vsix extension only
ever reads it indirectly, via the WS protocol (see doc/LLM_REGISTRY.md).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

__all__ = [
    "LocalLLMEntry",
    "add_local_entry",
    "clear_llama_server_override_path",
    "get_llama_server_override_path",
    "get_local_registry",
    "parse_llama_args",
    "remove_local_entry",
    "set_llama_server_override_path",
]

_log = logging.getLogger(__name__)

_REGISTRY_RELATIVE_PATH = ("etc", "local-llm-registry.json")

_CUSTOM_KINDS = frozenset({"custom_hf", "custom_file", "custom_server_url"})


@dataclass(frozen=True)
class LocalLLMEntry:
    """A single local (llama.cpp) model, hardcoded or user-added.

    Attributes:
        name: Registry key / display name (e.g. ``'llamacpp-qwen36-27b-q4-k-xl'``
            for hardcoded entries, or whatever the user typed when adding a
            custom one). Must be unique across the merged registry.
        kind: ``'hardcoded_hf'``, ``'custom_hf'``, ``'custom_file'``, or
            ``'custom_server_url'``.
        description: Human-readable description.
        repo_id: HuggingFace repository ID (``hardcoded_hf``/``custom_hf`` only).
        filename: GGUF filename inside the HF repository
            (``hardcoded_hf``/``custom_hf`` only).
        llama_args: Extra CLI flags passed verbatim to ``llama-server``
            (any kind that runs through llama-server: ``hardcoded_hf``/
            ``custom_hf``/``custom_file`` — never ``custom_server_url``,
            which isn't a process kodo launches).
        context_window: Maximum input-context size in tokens. Falls back to
            the default when unset/non-positive (see
            :func:`kodo.llms.get_context_window`). Same kind restriction as
            ``llama_args``.
        path: Absolute path to the GGUF file on disk (``custom_file`` only).
        url: Base URL of the externally-managed server (``custom_server_url``
            only), e.g. ``'http://192.168.1.50:8042'``.
    """

    name: str
    kind: str
    description: str = ""
    repo_id: str = ""
    filename: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)
    context_window: int = 0
    path: str = ""
    url: str = ""


# Compiled-in GGUFs — ported from the old flat registry, dropping `residence`.
_HARDCODED_LOCAL_MODELS: tuple[LocalLLMEntry, ...] = (
    LocalLLMEntry(
        name="llamacpp-qwen36-27b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q8_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q8_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-27b-q6-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q6_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q6_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-27b-q5-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q5_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q5_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-27b-q4-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q4_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-35b-a3b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q8_K_XL by Unsloth — local inference via llama-server",
        repo_id="Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-35b-a3b-q6-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q6_K_XL by Unsloth — local inference via llama-server",
        repo_id="Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-35b-a3b-q5-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q5_K_XL by Unsloth — local inference via llama-server",
        repo_id="Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen36-35b-a3b-q4-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q4_K_XL by Unsloth — local inference via llama-server",
        repo_id="Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen3-coder-next-80b-q4-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q4_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen3-coder-next-80b-q3-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q3_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-Q3_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen3-coder-next-80b-mxfp4-moe",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B MXFP4-MOE by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-MXFP4_MOE.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-qwen35-9b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.5 9B UD-Q8_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/Qwen3.5-9B-MTP-GGUF",
        filename="Qwen3.5-9B-UD-Q8_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-gemma4-26b-q4-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B UD-Q4_K_XL by Unsloth — local inference via llama-server",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=131_072,
    ),
    LocalLLMEntry(
        name="llamacpp-ornith10-35b-q8-0",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q8_0 by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q8_0.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-ornith10-35b-q6-k",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q6_K by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q6_K.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-ornith10-35b-q5-k-m",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q5_K by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q5_K_m.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
    LocalLLMEntry(
        name="llamacpp-ornith10-35b-q4-k-m",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q4_K by DeepReinforce — local inference via llama-server",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q4_K_m.gguf",
        llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        context_window=262_144,
    ),
)


# ---------------------------------------------------------------------------
# External file I/O
# ---------------------------------------------------------------------------


def _registry_file(kodo_dir: Path) -> Path:
    return kodo_dir.joinpath(*_REGISTRY_RELATIVE_PATH)


def parse_llama_args(raw: object) -> dict[str, str]:
    """Coerce a WS-payload/JSON value into the ``llama_args`` shape.

    Anything that isn't a ``dict`` (missing field, wrong type from a
    malformed request) is treated as "no extra args" rather than raising —
    callers are parsing untrusted request payloads.
    """
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _entry_from_json(raw: dict[str, object]) -> LocalLLMEntry | None:
    name = str(raw.get("name", "")).strip()
    kind = str(raw.get("kind", "")).strip()
    if not name or kind not in _CUSTOM_KINDS:
        _log.warning("Skipping invalid local-llm-registry.json entry: %r", raw)
        return None
    llama_args = parse_llama_args(raw.get("llama_args", {}))
    return LocalLLMEntry(
        name=name,
        kind=kind,
        description=str(raw.get("description", "")),
        repo_id=str(raw.get("repo_id", "")),
        filename=str(raw.get("filename", "")),
        llama_args=llama_args,
        context_window=int(cast(int, raw.get("context_window", 0)) or 0),
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
        "llama_args": entry.llama_args,
        "context_window": entry.context_window,
        "path": entry.path,
        "url": entry.url,
    }


def _load_external(kodo_dir: Path) -> tuple[list[LocalLLMEntry], str | None]:
    path = _registry_file(kodo_dir)
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("Could not load %s: %s", path, exc)
        return [], None
    if not isinstance(data, dict):
        return [], None
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
    path = _registry_file(kodo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "entries": [_entry_to_json(e) for e in entries],
        "llama_server_override_path": override_path,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public registry API
# ---------------------------------------------------------------------------


def get_local_registry(kodo_dir: Path) -> dict[str, LocalLLMEntry]:
    """Return the merged local registry: hardcoded entries + the user's custom ones.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        dict[str, LocalLLMEntry]: Map of entry name to :class:`LocalLLMEntry`.
    """
    merged: dict[str, LocalLLMEntry] = {e.name: e for e in _HARDCODED_LOCAL_MODELS}
    external, _ = _load_external(kodo_dir)
    for entry in external:
        if entry.name in merged:
            _log.warning("Custom local LLM %r shadows a hardcoded entry — skipping", entry.name)
            continue
        merged[entry.name] = entry
    return merged


def add_local_entry(kodo_dir: Path, entry: LocalLLMEntry) -> None:
    """Add a custom entry to the external collection.

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
    external, override = _load_external(kodo_dir)
    external.append(entry)
    _save_external(kodo_dir, external, override)


def remove_local_entry(kodo_dir: Path, name: str) -> None:
    """Remove a custom entry from the external collection.

    Does not touch any downloaded GGUF file on disk — callers that want to
    free disk space should uninstall (see
    :func:`kodo.llms.llamacpp.delete_model`) before removing.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        name: Entry name to remove.

    Raises:
        ValueError: If *name* is a hardcoded entry or does not exist.
    """
    if any(e.name == name for e in _HARDCODED_LOCAL_MODELS):
        raise ValueError(f"{name!r} is a built-in local LLM and cannot be removed")
    external, override = _load_external(kodo_dir)
    remaining = [e for e in external if e.name != name]
    if len(remaining) == len(external):
        raise ValueError(f"No custom local LLM named {name!r}")
    _save_external(kodo_dir, remaining, override)


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
