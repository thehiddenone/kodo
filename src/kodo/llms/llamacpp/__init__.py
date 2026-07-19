"""llama.cpp LLM provider plugin plus local-inference utilities.

This package hosts both the :class:`LlamaPlugin` (the :class:`~kodo.llms.LLMPlugin`
implementation that streams from ``llama-server``) and the local-inference
lifecycle utilities formerly in the top-level ``kodo.llm_utils`` package:

* **Installer** — platform-aware download/extraction of llama.cpp into
  ``~/.kodo/llama.cpp/b{N}/`` (:func:`install_llamacpp`, :func:`uninstall_llamacpp`,
  :func:`update_llamacpp`, :func:`check_llamacpp_update`, :func:`build_exists`,
  :func:`fetch_latest_build_number`, :func:`find_installed`, :func:`server_executable`).
  ``install_llamacpp``/``update_llamacpp`` accept an optional ``version`` (a
  build number) to pin an explicit release instead of installing latest;
  :func:`build_exists` checks whether a given build number was actually
  published before anything pinned to it uninstalls the current build.
* **Local model manager access** — :func:`get_local_model_manager` resolves
  the models directory (``llm_models_dir`` in ``settings.json``, falling back
  to ``~/.kodo/llama.cpp/models``) and returns the process-wide
  :class:`kodo.llms.local.LocalModelManager` for it, cached per directory so
  every caller within one server process shares the same instance. See
  ``kodo/doc/LOCAL_MODEL_MANAGER.md`` for the manager itself — download,
  pause/resume, multi-file (split GGUF) downloads, mmproj companions, and
  HF tokens.
* **Server** — async ``llama-server`` process manager (:class:`LlamaServer`,
  :class:`LlamaServerConfig`, :class:`RunningServer`, :func:`find_running_server`,
  :func:`ensure_llama_running`).

These utilities were merged here to break the former ``llms ⇄ llm_utils`` import
cycle: they are only ever used by llama.cpp inference, so they belong under the
``llamacpp`` subpackage.
"""

from ._installer import (
    LlamaInstall,
    build_exists,
    check_llamacpp_update,
    fetch_latest_build_number,
    find_installed,
    install_llamacpp,
    server_executable,
    uninstall_llamacpp,
    update_llamacpp,
)
from ._llama import LlamaPlugin, MalformedToolCallError, ThinkingStreamParser
from ._llama_server import (
    LlamaServer,
    LlamaServerConfig,
    RunningServer,
    find_running_server,
)
from ._manager import ensure_llama_running, get_local_model_manager

__all__ = [
    "LlamaInstall",
    "LlamaPlugin",
    "LlamaServer",
    "LlamaServerConfig",
    "MalformedToolCallError",
    "RunningServer",
    "ThinkingStreamParser",
    "build_exists",
    "check_llamacpp_update",
    "ensure_llama_running",
    "fetch_latest_build_number",
    "find_installed",
    "find_running_server",
    "get_local_model_manager",
    "install_llamacpp",
    "server_executable",
    "uninstall_llamacpp",
    "update_llamacpp",
]
