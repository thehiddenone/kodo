"""Local LLM registry: a JSON catalog of GGUFs plus a user-managed external collection.

Every entry here runs on llama.cpp — there is no ``residence`` field any more
(the old flat registry's cloud/local split lives in
:mod:`kodo.llms._cloud_registry` now). Entries are discriminated by ``kind``:

- ``hardcoded_hf`` — a **catalog** HuggingFace GGUF: one JSON file per entry,
  ``<root>/<base_llm>/<name>.json`` (format: :mod:`._catalog_files`). Two
  roots share that layout (:mod:`._catalog`): :data:`BUILTIN_CATALOG_DIR`,
  shipped inside this package, and :func:`user_catalog_dir`
  (``~/.kodo/local_llms/``), whose files add models or **replace** a shipped
  entry of the same name. Add a shipped model by adding its file. The kind
  keeps its historical name whichever root an entry came from, so the wire
  shape does not depend on it.
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

Launch configuration: knobs and profiles
----------------------------------------

An entry is launched under one of two things (doc/LLM_REGISTRY.md §4.6):

- Its **Default profile** — computed, never stored: the entry's
  ``base_llama_args`` plus whatever its **knobs** currently resolve to. A knob
  is a hardcoded checkbox/dropdown/number control that owns a fixed set of CLI
  flags (:mod:`._knobs`); shared knobs live in :mod:`._knobs_shared` and
  per-family ones in their own ``_knobs_<family>`` modules, and a catalog file
  names the ones its entry offers by id. This is what replaced the old
  "one predefined flavor per combination" model.
- A **user-defined profile** (:class:`LlmProfile`) — a raw arg set the user
  built in the "Manage profiles" editor, which fully replaces the Default
  profile's args rather than layering onto them. These have no knobs.

The external collection (``custom_*`` entries), the global llama-server binary
override path, every user-defined profile, the active-profile selection and
the knob selections are all persisted in
``~/.kodo/etc/local-llm-registry.json``, owned (read + written) entirely by
this package (see :mod:`._io`) — the kodo-vsix extension only ever reads it
indirectly, via the WS protocol (see doc/LLM_REGISTRY.md).

Submodules:
    ``_knobs`` — :class:`LlamaKnob`, :class:`KnobOption`, :class:`KnobKind`,
        knob validation and selection resolution. No in-package dependencies.
    ``_types`` — :class:`LocalLLMEntry`, :class:`LlmProfile`.
    ``_thinking`` — the Qwen-reasoning-budget / GPT-OSS-reasoning-effort
        thinking-tier families.
    ``_reserved`` — the launch args kodo owns and a profile may never set.
    ``_knobs_shared`` — the knobs every llama-server entry offers, plus the
        base args every Default profile starts from.
    ``_knobs_context`` — factory for the private per-model YaRN long-context
        knob (needs the model's architecture key and native context length).
    ``_knobs_qwen`` / ``_knobs_laguna`` — the private per-family knobs.
    ``_knobs_mtp`` — the built-in MTP checkbox, and the per-family MTP head
        picker built from a family's ``mtp_sidecars.json``.
    ``_mtp_sidecars`` — the ``mtp_sidecars.json`` format: a family's
        standalone MTP draft heads, shared by all of its quants, and the
        download-record ids they are stored under.
    ``_knobs_table`` — ``KNOBS_BY_ID``, every knob a catalog file may name.
    ``_catalog_files`` — the catalog-entry JSON format: parse, serialize,
        scan a ``<base_llm>/<name>.json`` tree (and its ``mtp_sidecars.json``
        files), per-entry validation, attaching the MTP head picker, and the
        display-order sort key.
    ``_catalog`` — loads ``_HARDCODED_LOCAL_MODELS`` from the shipped
        ``catalog/`` directory (validated at import time) and the user
        catalog from ``~/.kodo/local_llms/`` (validated per call, invalid
        files skipped).
    ``_io`` — ``local-llm-registry.json`` file I/O and JSON (de)serialization.
    ``_profiles`` — profile CRUD, knob state, and launch-config resolution.
        Depends on ``_entries``.
    ``_entries`` — the merged registry map, custom-entry CRUD, override path,
        and the startup purge of state left behind by models this kodo
        release no longer has (``prune_unknown_model_state``).
"""

from __future__ import annotations

from ._catalog import BUILTIN_CATALOG_DIR, user_catalog_dir
from ._entries import (
    add_local_entry,
    clear_llama_server_override_path,
    get_llama_server_override_path,
    get_local_registry,
    get_mtp_sidecars,
    prune_unknown_model_state,
    remove_local_entry,
    set_llama_server_override_path,
    stale_mtp_sidecar_model_ids,
)
from ._io import parse_llama_args, parse_llama_args_text
from ._knobs import (
    KnobKind,
    KnobOption,
    LlamaKnob,
    knob_owned_flags,
    knob_selection_args,
    resolve_knob_selections,
    validate_knobs,
)
from ._knobs_context import make_yarn_context_knob
from ._knobs_mtp import MTP_DRAFT_MODEL_FLAG
from ._knobs_shared import BASE_LLAMA_ARGS, SHARED_KNOBS
from ._mtp_sidecars import MTP_SIDECARS_FILENAME, MtpSidecar, mtp_sidecar_model_id
from ._profiles import (
    add_profile,
    get_active_profile,
    get_knob_selections,
    get_profiles,
    get_selected_mtp_sidecar,
    remove_profile,
    resolve_context_window,
    resolve_default_profile_args,
    resolve_effective_llama_config,
    set_active_profile,
    set_knobs,
    update_profile,
)
from ._reserved import RESERVED_LLAMA_ARGS, strip_reserved_llama_args
from ._thinking import (
    GPT_OSS_REASONING_EFFORT_FAMILY,
    QWEN4EXP_REASONING_EFFORT_FAMILY,
    QWEN_REASONING_BUDGET_FAMILY,
    QWEN_TIER_TOKEN_BUDGETS,
    REASONING_BUDGET_MESSAGE,
    RESERVED_REASONING_CAP_ARGS,
    UNLIMITED_THINKING_TIER,
    local_thinking_default_tier,
    local_thinking_family,
    local_thinking_tiers,
)
from ._types import LlmProfile, LocalLLMEntry

__all__ = [
    "BASE_LLAMA_ARGS",
    "BUILTIN_CATALOG_DIR",
    "GPT_OSS_REASONING_EFFORT_FAMILY",
    "MTP_DRAFT_MODEL_FLAG",
    "MTP_SIDECARS_FILENAME",
    "QWEN4EXP_REASONING_EFFORT_FAMILY",
    "QWEN_REASONING_BUDGET_FAMILY",
    "QWEN_TIER_TOKEN_BUDGETS",
    "REASONING_BUDGET_MESSAGE",
    "RESERVED_LLAMA_ARGS",
    "RESERVED_REASONING_CAP_ARGS",
    "SHARED_KNOBS",
    "UNLIMITED_THINKING_TIER",
    "KnobKind",
    "KnobOption",
    "LlamaKnob",
    "LlmProfile",
    "LocalLLMEntry",
    "MtpSidecar",
    "add_local_entry",
    "add_profile",
    "clear_llama_server_override_path",
    "get_active_profile",
    "get_knob_selections",
    "get_llama_server_override_path",
    "get_local_registry",
    "get_mtp_sidecars",
    "get_profiles",
    "get_selected_mtp_sidecar",
    "knob_owned_flags",
    "knob_selection_args",
    "local_thinking_default_tier",
    "local_thinking_family",
    "local_thinking_tiers",
    "make_yarn_context_knob",
    "mtp_sidecar_model_id",
    "parse_llama_args",
    "parse_llama_args_text",
    "prune_unknown_model_state",
    "remove_local_entry",
    "remove_profile",
    "resolve_context_window",
    "resolve_default_profile_args",
    "resolve_effective_llama_config",
    "resolve_knob_selections",
    "set_active_profile",
    "set_knobs",
    "set_llama_server_override_path",
    "stale_mtp_sidecar_model_ids",
    "strip_reserved_llama_args",
    "update_profile",
    "user_catalog_dir",
    "validate_knobs",
]
