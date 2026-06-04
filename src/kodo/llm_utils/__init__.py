"""Local inference utilities for kodo.

Provides three capabilities:

* **Installer** — platform-aware download and extraction of the latest
  llama.cpp release into ``~/.kodo/llama.cpp/b{N}/``
  (:func:`install_llamacpp`, :func:`uninstall_llamacpp`,
  :func:`update_llamacpp`, :func:`check_llamacpp_update`,
  :func:`find_installed`, :func:`server_executable`).
* **Downloader** — thin :mod:`huggingface_hub` wrapper that fetches a
  specific GGUF from a HuggingFace repository into the configured models
  directory (:func:`download_model`, :func:`get_model_path`).
* **Server** — async process manager for ``llama-server``
  (:class:`LlamaServer`, :class:`LlamaServerConfig`).
"""

from ._downloader import download_model, get_model_path
from ._installer import (
    LlamaInstall,
    check_llamacpp_update,
    find_installed,
    install_llamacpp,
    server_executable,
    uninstall_llamacpp,
    update_llamacpp,
)
from ._llama_server import LlamaServer, LlamaServerConfig, RunningServer, find_running_server

__all__ = [
    "LlamaInstall",
    "LlamaServer",
    "LlamaServerConfig",
    "RunningServer",
    "find_running_server",
    "check_llamacpp_update",
    "download_model",
    "find_installed",
    "get_model_path",
    "install_llamacpp",
    "server_executable",
    "uninstall_llamacpp",
    "update_llamacpp",
]
