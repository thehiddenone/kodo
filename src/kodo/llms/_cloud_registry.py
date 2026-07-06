"""Cloud LLM registry: a hardcoded, two-tier vendor → model tree.

Tier one is the vendor (``"anthropic"``, ...); tier two is that vendor's
models. Unlike the local registry (:mod:`kodo.llms._local_registry`), this
catalogue is 100% compiled-in — there is no user-editable/external part, since
adding a cloud vendor or model always requires a matching plugin/pricing table
update anyway.

The registry key for a cloud model is its own ``model_id`` (the string sent to
the provider's API) — there is no separate synthetic key like the local
registry's arbitrary names.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CloudLLMEntry",
    "get_cloud_entry",
    "get_cloud_registry",
    "get_cloud_vendor_display_name",
    "get_cloud_vendor_module",
]


@dataclass(frozen=True)
class CloudLLMEntry:
    """A single hardcoded cloud model.

    Attributes:
        name: Human-readable display name (e.g. ``'Claude Opus 4.8'``).
        model_id: The API model identifier sent to the provider — also this
            entry's registry key.
        description: Human-readable description shown in the Cloud AI
            Settings webview.
        context_window: Maximum input-context size in tokens. Falls back to
            :data:`_DEFAULT_CONTEXT_WINDOW` when unset/non-positive (see
            :func:`kodo.llms.get_context_window`).
    """

    name: str
    model_id: str
    description: str
    context_window: int = 0


# One tuple of hardcoded entries per vendor. Add a new vendor by adding a new
# tuple here plus an entry in _CLOUD_REGISTRY/_CLOUD_VENDOR_DISPLAY/
# _CLOUD_VENDOR_MODULE below — no other file needs to change to add models to
# an existing vendor.
_ANTHROPIC_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="Claude Opus 4.8",
        model_id="claude-opus-4-8",
        description="Anthropic Claude Opus 4.8",
        context_window=1_000_000,
    ),
    CloudLLMEntry(
        name="Claude Opus 4.7",
        model_id="claude-opus-4-7",
        description="Anthropic Claude Opus 4.7",
        context_window=1_000_000,
    ),
    CloudLLMEntry(
        name="Claude Opus 4.6",
        model_id="claude-opus-4-6",
        description="Anthropic Claude Opus 4.6",
        context_window=1_000_000,
    ),
    CloudLLMEntry(
        name="Claude Sonnet 5",
        model_id="claude-sonnet-5",
        description="Anthropic Claude Sonnet 5",
        context_window=1_000_000,
    ),
    CloudLLMEntry(
        name="Claude Sonnet 4.6",
        model_id="claude-sonnet-4-6",
        description="Anthropic Claude Sonnet 4.6",
        context_window=1_000_000,
    ),
    CloudLLMEntry(
        name="Claude Haiku 4.5",
        model_id="claude-haiku-4-5-20251001",
        description="Anthropic Claude Haiku 4.5",
        context_window=200_000,
    ),
    CloudLLMEntry(
        name="Claude Fable 5",
        model_id="claude-fable-5",
        description="Anthropic Claude Fable 5",
        context_window=1_000_000,
    ),
)

# Vendor key -> hardcoded models. Vendor keys are lowercase slugs used in
# etc/settings.json (``active_cloud_vendor``, ``models.cloud.<vendor>``) and on
# the wire; display names are separate so the UI can show "Anthropic" etc.
_CLOUD_REGISTRY: dict[str, tuple[CloudLLMEntry, ...]] = {
    "anthropic": _ANTHROPIC_MODELS,
}

_CLOUD_VENDOR_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic",
}

# Vendor key -> dotted plugin module, mirroring the old LLMEntry.module field
# (now per-vendor instead of per-model, since every model from one vendor uses
# the same plugin implementation).
_CLOUD_VENDOR_MODULE: dict[str, str] = {
    "anthropic": "kodo.llms.anthropic",
}


def get_cloud_registry() -> dict[str, tuple[CloudLLMEntry, ...]]:
    """Return the full cloud registry: vendor key -> its hardcoded models."""
    return dict(_CLOUD_REGISTRY)


def get_cloud_entry(vendor: str, model_id: str) -> CloudLLMEntry | None:
    """Look up one model by vendor + model_id, or ``None`` if either is unknown."""
    for entry in _CLOUD_REGISTRY.get(vendor, ()):
        if entry.model_id == model_id:
            return entry
    return None


def get_cloud_vendor_display_name(vendor: str) -> str:
    """Human-readable vendor name, falling back to the raw key if unknown."""
    return _CLOUD_VENDOR_DISPLAY.get(vendor, vendor)


def get_cloud_vendor_module(vendor: str) -> str | None:
    """Dotted plugin module for *vendor*, or ``None`` if unknown."""
    return _CLOUD_VENDOR_MODULE.get(vendor)
