"""Local inference utilities for kodo.

Provides four capabilities:

* **Registry** — a built-in catalogue of known GGUF models
  (:data:`REGISTRY`, :func:`get_model`).
* **Installer** — platform-aware download and extraction of the latest
  llama.cpp release into ``~/.kodo/llama.cpp/b{N}/``
  (:func:`install`, :func:`find_installed`, :func:`server_executable`).
* **Downloader** — thin :mod:`huggingface_hub` wrapper that fetches a
  specific GGUF from a multi-model HF repository into
  ``~/.kodo/llmcache/`` (:func:`download_model`).
* **Server** — async process manager for ``llama-server``
  (:class:`LlamaServer`, :class:`LlamaServerConfig`).
"""

from ._downloader import download_model, get_llm_cache_index
from ._installer import find_installed, install, server_executable
from ._llama_server import LlamaServer, LlamaServerConfig
from ._registry import LLM_REGISTRY, ModelEntry, get_model

__all__ = [
    "LLM_REGISTRY",
    "LlamaServer",
    "LlamaServerConfig",
    "ModelEntry",
    "download_model",
    "get_llm_cache_index",
    "find_installed",
    "get_model",
    "install",
    "server_executable",
]
