"""Local LLM registry: hardcoded GGUFs plus a user-managed external collection.

Every entry here runs on llama.cpp — there is no ``residence`` field any more
(the old flat registry's cloud/local split lives in
:mod:`kodo.llms._cloud_registry` now). Entries are discriminated by ``kind``:

- ``hardcoded_hf`` — compiled-in HuggingFace GGUF, shipped with kodo. The
  catalog is assembled in :mod:`._catalog` from one ``_hardcoded_<family>.py``
  module per model family (e.g. :mod:`._hardcoded_qwen36_27b`), each
  exporting a ``*_entries() -> list[LocalLLMEntry]`` function — add a new
  hardcoded model by adding it to (or creating) the relevant family module.
- ``custom_hf`` — user-added HuggingFace GGUF (same shape as ``hardcoded_hf``,
  added via the "Add local LLM from huggingface.com" flow). Has an
  installed/not-installed state, resolved the same way as ``hardcoded_hf``
  (presence in :class:`kodo.llms.local.LocalModelManager`'s state, see
  :func:`kodo.llms.llamacpp.get_local_model_manager`).
- ``custom_file`` — user-added local GGUF file that kodo does not own or copy.
  "Installed" means the file exists on disk; per design this is checked once,
  by the kodo-vsix extension, at its own startup — not re-verified here.
- ``custom_server_url`` — user-added link to an already-running llama.cpp (or
  OpenAI-compatible) server kodo does not manage. Always considered
  installed; selecting it as active stops kodo's own managed llama-server
  (see :mod:`kodo.llms.llamacpp._llama`).

The external collection (``custom_*`` entries) plus the global llama-server
binary override path are persisted in ``~/.kodo/etc/local-llm-registry.json``,
owned (read + written) entirely by this package (see :mod:`._io`) — the
kodo-vsix extension only ever reads it indirectly, via the WS protocol (see
doc/LLM_REGISTRY.md).

Submodules:
    ``_types`` — :class:`LocalLLMEntry`, :class:`LlamaFlavor`,
        :class:`LlamaFlavorPlatform`, :func:`current_host_platform`. No
        in-package dependencies; everything else depends on this.
    ``_thinking`` — the Qwen-reasoning-budget / GPT-OSS-reasoning-effort
        thinking-tier families.
    ``_hardcoded_<family>`` — one module per hardcoded model family, each a
        pure list of :class:`LocalLLMEntry` literals.
    ``_catalog`` — assembles ``_HARDCODED_LOCAL_MODELS`` from every
        ``_hardcoded_<family>`` module.
    ``_io`` — ``local-llm-registry.json`` file I/O and JSON (de)serialization.
    ``_flavors`` — flavor CRUD (custom flavors, active-flavor selection) and
        launch-config resolution. Depends on ``_entries``.
    ``_entries`` — the merged registry map, custom-entry CRUD, override path.
"""

from __future__ import annotations

from ._entries import (
    add_local_entry,
    clear_llama_server_override_path,
    get_llama_server_override_path,
    get_local_registry,
    remove_local_entry,
    set_llama_server_override_path,
)
from ._flavors import (
    add_flavor,
    get_active_flavor,
    get_effective_flavor_id,
    get_flavors,
    has_compatible_flavor,
    remove_flavor,
    resolve_context_window,
    resolve_effective_llama_config,
    set_active_flavor,
    update_flavor,
)
from ._io import parse_llama_args, parse_llama_args_text
from ._thinking import (
    GPT_OSS_REASONING_EFFORT_FAMILY,
    QWEN_REASONING_BUDGET_FAMILY,
    QWEN_TIER_TOKEN_BUDGETS,
    REASONING_BUDGET_MESSAGE,
    RESERVED_REASONING_CAP_ARGS,
    local_thinking_default_tier,
    local_thinking_family,
    local_thinking_tiers,
)
from ._types import LlamaFlavor, LlamaFlavorPlatform, LocalLLMEntry, current_host_platform

__all__ = [
    "GPT_OSS_REASONING_EFFORT_FAMILY",
    "QWEN_REASONING_BUDGET_FAMILY",
    "QWEN_TIER_TOKEN_BUDGETS",
    "REASONING_BUDGET_MESSAGE",
    "RESERVED_REASONING_CAP_ARGS",
    "LlamaFlavor",
    "LlamaFlavorPlatform",
    "LocalLLMEntry",
    "add_flavor",
    "add_local_entry",
    "clear_llama_server_override_path",
    "current_host_platform",
    "get_active_flavor",
    "get_effective_flavor_id",
    "get_flavors",
    "get_llama_server_override_path",
    "get_local_registry",
    "has_compatible_flavor",
    "local_thinking_default_tier",
    "local_thinking_family",
    "local_thinking_tiers",
    "parse_llama_args",
    "parse_llama_args_text",
    "remove_flavor",
    "remove_local_entry",
    "resolve_context_window",
    "resolve_effective_llama_config",
    "set_active_flavor",
    "set_llama_server_override_path",
    "update_flavor",
]
