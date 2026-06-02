"""HuggingFace Hub model downloader.

Downloads a specific GGUF file from a HuggingFace repository into the local
model cache directory, then records the installed path in a JSON index so
other components can locate the file without re-scanning the filesystem.

The cache directory defaults to ``~/.kodo/llama.cpp/models`` and can be
overridden by setting ``llm_models_dir`` in ``~/.kodo/settings.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import huggingface_hub

from kodo.llms import LLMEntry

__all__ = ["download_model", "get_model_path"]

_log = logging.getLogger(__name__)

_INDEX_FILE = "local-llm-index.json"


def _models_dir(kodo_dir: Path) -> Path:
    """Return the directory where GGUF model files are stored.

    Reads ``llm_models_dir`` from ``kodo_dir/settings.json``; falls back to
    ``kodo_dir/llama.cpp/models``.

    Args:
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        Path: Directory for model files.
    """
    settings_file = kodo_dir / "settings.json"
    if settings_file.is_file():
        try:
            parsed = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and "llm_models_dir" in parsed:
                return Path(str(parsed["llm_models_dir"]))
        except Exception:
            pass
    return kodo_dir / "llama.cpp" / "models"


def _read_index(kodo_dir: Path) -> dict[str, str]:
    index_file = kodo_dir / _INDEX_FILE
    if index_file.is_file():
        parsed = json.loads(index_file.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM index at {index_file} is not a JSON object")
        return cast(dict[str, str], parsed)
    return {}


def _write_index(kodo_dir: Path, index: dict[str, str]) -> None:
    index_file = kodo_dir / _INDEX_FILE
    index_file.write_text(json.dumps(index, indent=2), encoding="utf-8")


def download_model(model: LLMEntry, kodo_dir: Path) -> Path:
    """Download a GGUF model file from HuggingFace Hub.

    Delegates to :func:`huggingface_hub.hf_hub_download`, which handles
    resumable downloads, integrity verification, and caching.  Subsequent
    calls with the same *model* skip the network round-trip and return the
    cached path.  The resolved path is recorded in ``~/.kodo/llm_index.json``
    keyed by the model's registry name.

    Args:
        model (LLMEntry): Registry entry for the model to download.
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        Path: Local path to the downloaded ``.gguf`` file.

    Raises:
        ValueError: If *model* has no ``repo_id`` or ``filename`` (i.e. not local).
    """
    if not model.repo_id or not model.filename:
        raise ValueError(f"Model {model.name!r} has no repo_id/filename — not a local model")

    dest_dir = _models_dir(kodo_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    _log.info("Downloading model %r from %s", model.name, model.repo_id)
    local_path = huggingface_hub.hf_hub_download(
        repo_id=model.repo_id,
        filename=model.filename,
        cache_dir=str(dest_dir),
    )
    result = Path(local_path)
    _log.info("Model %r available at %s", model.name, result)

    index = _read_index(kodo_dir)
    index[model.name] = str(result.absolute())
    _write_index(kodo_dir, index)

    return result


def get_model_path(name: str, kodo_dir: Path) -> Path | None:
    """Return the local path for an installed model, or ``None`` if not installed.

    Args:
        name (str): Registry key (e.g. ``'llamacpp-qwen36-27b'``).
        kodo_dir (Path): User-level ``~/.kodo`` directory.

    Returns:
        Path | None: Path to the ``.gguf`` file, or ``None``.
    """
    index = _read_index(kodo_dir)
    raw = index.get(name)
    if raw is None:
        return None
    p = Path(raw)
    return p if p.is_file() else None
