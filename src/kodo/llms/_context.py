"""Cross-registry context-window lookup.

A *model key* means different things depending on residence: for cloud it's
the ``model_id`` (also the ``CloudLLMEntry`` registry key); for local it's the
``LocalLLMEntry.name``. This module accepts either without the caller having
to know which — the compaction/context-limit code only ever has the key that
was already resolved via ``_resolve_model_key`` (kodo/runtime/_engine/_llm.py).
"""

from __future__ import annotations

from pathlib import Path

from ._bedrock_catalog import get_bedrock_model
from ._cloud_registry import get_cloud_registry
from ._openrouter_catalog import get_openrouter_model
from .local_registry import get_local_registry, resolve_effective_llama_config

__all__ = ["get_context_window"]

# Fallback context window for an unknown key or one whose ``context_window``
# is unset/non-positive — keeps auto-compaction working with a sane budget.
_DEFAULT_CONTEXT_WINDOW = 262_144


def get_context_window(model_key: str, kodo_dir: Path) -> int:
    """Return the maximum context window (in tokens) for *model_key*.

    Checks the cloud registry (by ``model_id``) first, then the two
    aggregator vendors' own fetched/cached catalogs
    (:mod:`kodo.llms._openrouter_catalog`, :mod:`kodo.llms._bedrock_catalog`
    — neither is part of the cloud registry above, since both are dynamic,
    not compiled in), then the local registry (by name) — for a local entry, its *active
    configuration*'s ``context_window`` takes precedence over the entry's own
    (see :func:`kodo.llms.resolve_effective_llama_config`), since that is the
    context size actually launched. Falls back to
    :data:`_DEFAULT_CONTEXT_WINDOW` for an unknown key or one whose
    ``context_window``/``context_length`` is unset/non-positive — including
    ``"openrouter/auto"``, whose real context limit depends on whichever
    model it routes a request to.

    Args:
        model_key: A cloud ``model_id``, an OpenRouter/Bedrock catalog id, or
            a local registry name.
        kodo_dir: User-level ``~/.kodo`` directory (needed to load the local
            registry's external/custom entries and both aggregator catalog
            caches).

    Returns:
        int: The model's context window in tokens (always > 0).
    """
    for vendor_models in get_cloud_registry().values():
        for entry in vendor_models:
            if entry.model_id == model_key:
                return entry.context_window if entry.context_window > 0 else _DEFAULT_CONTEXT_WINDOW

    openrouter_model = get_openrouter_model(kodo_dir, model_key)
    if openrouter_model is not None and openrouter_model.context_length > 0:
        return openrouter_model.context_length

    # Bedrock's own APIs report no context window at all, so this is the
    # best-effort per-family figure kodo.llms._bedrock_catalog attaches, and
    # 0 (unknown) for an unrecognised family — which lands on the default
    # below, same as any other unknown key.
    bedrock_model = get_bedrock_model(kodo_dir, model_key)
    if bedrock_model is not None and bedrock_model.context_length > 0:
        return bedrock_model.context_length

    local_entry = get_local_registry(kodo_dir).get(model_key)
    if local_entry is not None:
        _, context_window = resolve_effective_llama_config(kodo_dir, local_entry)
        if context_window > 0:
            return context_window

    return _DEFAULT_CONTEXT_WINDOW
