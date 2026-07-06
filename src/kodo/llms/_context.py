"""Cross-registry context-window lookup.

A *model key* means different things depending on residence: for cloud it's
the ``model_id`` (also the ``CloudLLMEntry`` registry key); for local it's the
``LocalLLMEntry.name``. This module accepts either without the caller having
to know which — the compaction/context-limit code only ever has the key that
was already resolved via ``_resolve_model_key`` (kodo/runtime/_engine/_llm.py).
"""

from __future__ import annotations

from pathlib import Path

from ._cloud_registry import get_cloud_registry
from ._local_registry import get_local_registry

__all__ = ["get_context_window"]

# Fallback context window for an unknown key or one whose ``context_window``
# is unset/non-positive — keeps auto-compaction working with a sane budget.
_DEFAULT_CONTEXT_WINDOW = 262_144


def get_context_window(model_key: str, kodo_dir: Path) -> int:
    """Return the maximum context window (in tokens) for *model_key*.

    Checks the cloud registry (by ``model_id``) first, then the local
    registry (by name); falls back to :data:`_DEFAULT_CONTEXT_WINDOW` for an
    unknown key or one whose ``context_window`` is unset/non-positive.

    Args:
        model_key: A cloud ``model_id`` or local registry name.
        kodo_dir: User-level ``~/.kodo`` directory (needed to load the local
            registry's external/custom entries).

    Returns:
        int: The model's context window in tokens (always > 0).
    """
    for vendor_models in get_cloud_registry().values():
        for entry in vendor_models:
            if entry.model_id == model_key:
                return entry.context_window if entry.context_window > 0 else _DEFAULT_CONTEXT_WINDOW

    local_entry = get_local_registry(kodo_dir).get(model_key)
    if local_entry is not None and local_entry.context_window > 0:
        return local_entry.context_window

    return _DEFAULT_CONTEXT_WINDOW
