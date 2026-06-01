"""HuggingFace Hub model downloader.

Provides a thin wrapper around :mod:`huggingface_hub` that downloads a
specific GGUF file from a multi-model repository into the local model cache
at ``~/.kodo/llmcache``.  If the file is already cached, the download is
skipped automatically by :mod:`huggingface_hub`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import huggingface_hub

from ._registry import ModelEntry

__all__ = ["get_llm_cache_index", "download_model"]


def download_model(model: ModelEntry, kodo_dir: Path) -> Path:
    """Download a GGUF model file from HuggingFace Hub.

    Delegates to :func:`huggingface_hub.hf_hub_download`, which handles
    resumable downloads, integrity verification, and caching.  Subsequent
    calls with the same *entry* return the cached path without a network
    round-trip.

    Args:
        entry (ModelEntry): Registry entry describing the model to fetch.
        kodo_dir (Path): The ``~/.kodo`` base directory.  The model file
            is placed inside ``kodo_dir/llmcache/``.

    Returns:
        Path: Local path to the downloaded ``.gguf`` file.
    """
    cache_dir = kodo_dir / "llmcache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_path = huggingface_hub.hf_hub_download(
        repo_id=model.repo_id,
        filename=model.filename,
        cache_dir=str(cache_dir),
    )
    result = Path(local_path)
    update_llm_cache_index(kodo_dir, model, result)
    return result


def get_llm_cache_index(kodo_dir: Path) -> dict[str, str]:
    index_file = kodo_dir / "llmcacheindex.json"
    index = dict[str, str]()
    if index_file.is_file():
        parsed = json.loads(index_file.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Unexpected: content of LLM index in {index_file} is not a valid JSON object."
            )
        index = cast(dict[str, str], parsed)
    return index


def update_llm_cache_index(kodo_dir: Path, model: ModelEntry, path: Path) -> None:
    index = get_llm_cache_index(kodo_dir)
    index[model.repo_id] = str(path.absolute())
    index_file = kodo_dir / "llmcacheindex.json"
    index_file.write_text(json.dumps(index), "utf-8")
