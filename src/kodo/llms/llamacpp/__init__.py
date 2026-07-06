"""llama.cpp LLM provider plugin plus local-inference utilities.

This package hosts both the :class:`LlamaPlugin` (the :class:`~kodo.llms.LLMPlugin`
implementation that streams from ``llama-server``) and the local-inference
lifecycle utilities formerly in the top-level ``kodo.llm_utils`` package:

* **Installer** — platform-aware download/extraction of llama.cpp into
  ``~/.kodo/llama.cpp/b{N}/`` (:func:`install_llamacpp`, :func:`uninstall_llamacpp`,
  :func:`update_llamacpp`, :func:`check_llamacpp_update`, :func:`find_installed`,
  :func:`server_executable`).
* **Downloader** — :mod:`huggingface_hub` wrapper fetching/removing a specific
  GGUF (:func:`download_model`, :func:`delete_model`, :func:`get_model_path`).
* **Server** — async ``llama-server`` process manager (:class:`LlamaServer`,
  :class:`LlamaServerConfig`, :class:`RunningServer`, :func:`find_running_server`,
  :func:`ensure_llama_running`).

These utilities were merged here to break the former ``llms ⇄ llm_utils`` import
cycle: they are only ever used by llama.cpp inference, so they belong under the
``llamacpp`` subpackage.
"""

from ._downloader import delete_model, download_model, get_model_path
from ._installer import (
    LlamaInstall,
    check_llamacpp_update,
    find_installed,
    install_llamacpp,
    server_executable,
    uninstall_llamacpp,
    update_llamacpp,
)
from ._llama import LlamaPlugin, ThinkingStreamParser
from ._llama_server import (
    LlamaServer,
    LlamaServerConfig,
    RunningServer,
    find_running_server,
)
from ._manager import ensure_llama_running

__all__ = [
    "LlamaInstall",
    "LlamaPlugin",
    "LlamaServer",
    "LlamaServerConfig",
    "RunningServer",
    "ThinkingStreamParser",
    "check_llamacpp_update",
    "delete_model",
    "download_model",
    "ensure_llama_running",
    "find_installed",
    "find_running_server",
    "get_model_path",
    "install_llamacpp",
    "server_executable",
    "uninstall_llamacpp",
    "update_llamacpp",
]
