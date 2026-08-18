"""Cloud LLM registry: a hardcoded, two-tier vendor → model tree.

Tier one is the vendor (``"anthropic"``, ...); tier two is that vendor's
models. Unlike the local registry (:mod:`kodo.llms.local_registry`), this
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
    "get_cloud_vendor_for_model_prefix",
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
        recommendation: One-line "when to pick this" blurb shown next to the
            model in the Cloud AI Settings webview (e.g. ``"For the most
            demanding work"``). Purely cosmetic — never parsed.
    """

    name: str
    model_id: str
    description: str
    context_window: int = 0
    recommendation: str = ""


# One tuple of hardcoded entries per vendor. Add a new vendor by adding a new
# tuple here plus an entry in _CLOUD_REGISTRY/_CLOUD_VENDOR_DISPLAY/
# _CLOUD_VENDOR_MODULE below — no other file needs to change to add models to
# an existing vendor.
#
# Fable is listed first (ahead of the Opus/Sonnet/Haiku lines) so it's the
# first option the Cloud AI Settings webview renders in every effort panel —
# it's the flagship max-effort recommendation, not because of API vintage.
_ANTHROPIC_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="Claude Fable 5",
        model_id="claude-fable-5",
        description="Anthropic Claude Fable 5",
        context_window=1_000_000,
        recommendation="For the most demanding work — deep reasoning, gnarly debugging, "
        "big architectural calls.",
    ),
    CloudLLMEntry(
        name="Claude Opus 5",
        model_id="claude-opus-5",
        description="Anthropic Claude Opus 5",
        context_window=1_000_000,
        recommendation="Your best all-around heavyweight — thorough and careful, rarely wrong.",
    ),
    CloudLLMEntry(
        name="Claude Opus 4.8",
        model_id="claude-opus-4-8",
        description="Anthropic Claude Opus 4.8",
        context_window=1_000_000,
        recommendation="A proven heavyweight from the previous Opus generation — "
        "still excellent for complex work.",
    ),
    CloudLLMEntry(
        name="Claude Opus 4.7",
        model_id="claude-opus-4-7",
        description="Anthropic Claude Opus 4.7",
        context_window=1_000_000,
        recommendation="An earlier Opus release — still solid for complex work.",
    ),
    CloudLLMEntry(
        name="Claude Opus 4.6",
        model_id="claude-opus-4-6",
        description="Anthropic Claude Opus 4.6",
        context_window=1_000_000,
        recommendation="An earlier Opus release — keep it around for reproducing older results.",
    ),
    CloudLLMEntry(
        name="Claude Sonnet 5",
        model_id="claude-sonnet-5",
        description="Anthropic Claude Sonnet 5",
        context_window=1_000_000,
        recommendation="The daily driver — fast and sharp for most everyday coding tasks.",
    ),
    CloudLLMEntry(
        name="Claude Sonnet 4.6",
        model_id="claude-sonnet-4-6",
        description="Anthropic Claude Sonnet 4.6",
        context_window=1_000_000,
        recommendation="A dependable middle-tier option from the previous Sonnet generation.",
    ),
    CloudLLMEntry(
        name="Claude Haiku 4.5",
        model_id="claude-haiku-4-5-20251001",
        description="Anthropic Claude Haiku 4.5",
        context_window=200_000,
        recommendation="Quick and cheap — ideal for simple, high-volume subagent tasks.",
    ),
)

# GPT-5.6 lineup — https://developers.openai.com/api/docs/models (2026-08-12).
# Only three SKUs this generation (Kōdo has four effort tiers) — Terra is the
# server-side default for both "medium" and "high" (see kodo/server/_config.py
# models.cloud.openai), Sol reserved for "max". Listed flagship-first, same
# convention as Fable leading _ANTHROPIC_MODELS.
_OPENAI_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="GPT-5.6 Sol",
        model_id="gpt-5.6-sol",
        description="OpenAI GPT-5.6 Sol",
        context_window=1_000_000,
        recommendation="For the most demanding work — deep reasoning, gnarly debugging, "
        "big architectural calls.",
    ),
    CloudLLMEntry(
        name="GPT-5.6 Terra",
        model_id="gpt-5.6-terra",
        description="OpenAI GPT-5.6 Terra",
        context_window=1_000_000,
        recommendation="The daily driver — balances capability and cost for most everyday "
        "coding tasks.",
    ),
    CloudLLMEntry(
        name="GPT-5.6 Luna",
        model_id="gpt-5.6-luna",
        description="OpenAI GPT-5.6 Luna",
        context_window=1_000_000,
        recommendation="Quick and cheap — ideal for simple, high-volume subagent tasks.",
    ),
)

# Meta's Model API (https://dev.meta.ai/docs/, as of 2026-08-12) has no
# effort-tiered lineup like Anthropic/OpenAI -- Muse Spark 1.2 is the one
# model on offer, so it is the sole entry here and gets assigned to all four
# kodo effort tiers (kodo/server/_config.py's models.cloud.meta defaults).
# The heavily-discounted "contributor" tier (trains future Meta models on
# your traffic in exchange for ~8x-cheaper tokens) is deliberately NOT a
# second registry entry/selectable model -- it is a per-account toggle
# (settings.json's meta_contributor_tier, kodo/llms/meta/_muse.py) that
# rewrites the outbound model id to "muse-spark-1.2-contributor" at request
# time, since real-world eligibility (see the Cloud AI Settings webview's
# Meta tab) is a country-restricted opt-in, not a model choice.
_META_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="Muse Spark 1.2",
        model_id="muse-spark-1.2",
        description="Meta Muse Spark 1.2",
        context_window=1_000_000,
        recommendation="Meta's flagship agentic model — the same model handles every effort "
        "tier, from quick edits to deep multi-file work.",
    ),
)

# Gemini's OpenAI-compatible endpoint (https://ai.google.dev/gemini-api/docs/openai,
# model ids/pricing researched 2026-08-12) has two SKUs against kodo's four
# effort tiers -- gemini-3.6-flash covers medium/high/max (kodo/server/
# _config.py's models.cloud.google), gemini-3.5-flash-lite is reserved for
# low. Unlike Anthropic/OpenAI/Meta (Responses-API-shaped), Gemini's plugin
# speaks Chat Completions -- see kodo/llms/google/_gemini.py's module
# docstring. Listed flagship-first, same convention as the other vendors'
# tuples.
_GOOGLE_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="Gemini 3.6 Flash",
        model_id="gemini-3.6-flash",
        description="Google Gemini 3.6 Flash",
        context_window=1_048_576,
        recommendation="The workhorse -- covers everyday coding through the most demanding "
        "work in this lineup.",
    ),
    CloudLLMEntry(
        name="Gemini 3.5 Flash-Lite",
        model_id="gemini-3.5-flash-lite",
        description="Google Gemini 3.5 Flash-Lite",
        context_window=1_048_576,
        recommendation="Quick and cheap -- ideal for simple, high-volume subagent tasks.",
    ),
)

# Qwen3.8 generation via Alibaba Cloud Model Studio's OpenAI-compatible
# endpoint (https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope,
# model ids/pricing researched 2026-08-16 -- see kodo/llms/alibaba/_usage.py).
# Three SKUs against kodo's four effort tiers, same "middle SKU covers two
# tiers" shape as _OPENAI_MODELS -- qwen3.8-plus covers medium/high
# (kodo/server/_config.py's models.cloud.alibaba defaults), qwen3.8-flash is
# reserved for low, qwen3.8-max for max. Like Google (and unlike
# Anthropic/OpenAI/Meta), Alibaba's plugin speaks Chat Completions -- see
# kodo/llms/alibaba/_qwen.py's module docstring. Listed flagship-first, same
# convention as the other vendors' tuples.
_ALIBABA_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="Qwen3.8 Max",
        model_id="qwen3.8-max",
        description="Alibaba Qwen3.8 Max",
        context_window=1_000_000,
        recommendation="For the most demanding work -- deep reasoning, gnarly debugging, "
        "big architectural calls.",
    ),
    CloudLLMEntry(
        name="Qwen3.8 Plus",
        model_id="qwen3.8-plus",
        description="Alibaba Qwen3.8 Plus",
        context_window=1_000_000,
        recommendation="The daily driver -- balances capability and cost for most everyday "
        "coding tasks.",
    ),
    CloudLLMEntry(
        name="Qwen3.8 Flash",
        model_id="qwen3.8-flash",
        description="Alibaba Qwen3.8 Flash",
        context_window=1_000_000,
        recommendation="Quick and cheap -- ideal for simple, high-volume subagent tasks.",
    ),
)

# DeepSeek's own OpenAI-compatible endpoint (https://api-docs.deepseek.com/,
# model ids/pricing researched 2026-08-16 -- see kodo/llms/deepseek/_usage.py).
# Legacy `deepseek-chat`/`deepseek-reasoner` aliases were deprecated
# 2026-07-24 in favor of explicit model ids with a request-level thinking
# toggle, so those aliases are deliberately not registered here. Two SKUs
# against kodo's four effort tiers -- a plain 2-2 split (unlike the other
# two-SKU vendor, Google, which splits 3-1), since DeepSeek's naming makes
# the capability gap explicit: V4 Pro is the deep-reasoning flagship
# (high/max), V4 Flash the fast/cheap everyday model (low/medium) --
# `kodo/server/_config.py`'s `models.cloud.deepseek` defaults. Like Google/
# Alibaba (and unlike Anthropic/OpenAI/Meta), DeepSeek's plugin speaks Chat
# Completions -- see `kodo/llms/deepseek/_deepseek.py`'s module docstring.
# Listed flagship-first, same convention as the other vendors' tuples.
_DEEPSEEK_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="DeepSeek V4 Pro",
        model_id="deepseek-v4-pro",
        description="DeepSeek V4 Pro",
        context_window=1_048_576,
        recommendation="For the most demanding work -- deep reasoning, gnarly debugging, "
        "big architectural calls.",
    ),
    CloudLLMEntry(
        name="DeepSeek V4 Flash",
        model_id="deepseek-v4-flash",
        description="DeepSeek V4 Flash",
        context_window=1_048_576,
        recommendation="The fast, cheap daily driver -- covers everyday coding at a "
        "fraction of Pro's cost.",
    ),
)

# Moonshot AI's Kimi lineup via its own OpenAI-compatible endpoint
# (https://platform.moonshot.ai/docs/guide/migrating-from-openai-to-kimi,
# model list https://platform.kimi.ai/docs/models, pricing researched
# 2026-08-16 -- see kodo/llms/kimi/_usage.py). Moonshot's current lineup has
# three non-deprecated SKUs (kimi-k3, kimi-k2.6, kimi-k2.7-code); kimi-k2.5
# and the kimi-k2 series are deliberately not registered here -- k2.5 is no
# longer offered to new signups ahead of a 2026-08-31 platform sunset, and k2
# was discontinued outright on 2026-05-25, same "don't register a model
# already on its way out" posture as DeepSeek's dropped legacy aliases. Of
# the two current non-flagship SKUs, kimi-k2.7-code is registered here and
# kimi-k2.6 is not -- a judgment call (the two are identically priced and
# comparably capable, but Kōdo is a coding agent with no use for K2.6's
# vision input, and K2.7 Code's own docs claim higher success rates on coding
# tasks specifically), not a documented Moonshot recommendation; see
# `kodo/llms/kimi/_kimi.py`'s module-level comment for the full reasoning.
# Two SKUs against kodo's four effort tiers -- a plain 2-2 split, same shape
# as DeepSeek's (`kodo/server/_config.py`'s `models.cloud.kimi` defaults):
# kimi-k2.7-code covers low/medium, kimi-k3 covers high/max. Like
# Google/Alibaba/DeepSeek (and unlike Anthropic/OpenAI/Meta), Kimi's plugin
# speaks Chat Completions -- see `kodo/llms/kimi/_kimi.py`'s module
# docstring. Listed flagship-first, same convention as the other vendors'
# tuples.
_KIMI_MODELS: tuple[CloudLLMEntry, ...] = (
    CloudLLMEntry(
        name="Kimi K3",
        model_id="kimi-k3",
        description="Moonshot AI Kimi K3",
        context_window=1_048_576,
        recommendation="For the most demanding work -- deep reasoning, gnarly debugging, "
        "big architectural calls.",
    ),
    CloudLLMEntry(
        name="Kimi K2.7 Code",
        model_id="kimi-k2.7-code",
        description="Moonshot AI Kimi K2.7 Code",
        context_window=262_144,
        recommendation="The fast, cheap daily driver -- Kimi's coding-tuned model covers "
        "everyday work at a fraction of K3's cost.",
    ),
)

# Vendor key -> hardcoded models. Vendor keys are lowercase slugs used in
# etc/settings.json (``active_cloud_vendor``, ``models.cloud.<vendor>``) and on
# the wire; display names are separate so the UI can show "Anthropic" etc.
#
# OpenRouter is deliberately NOT a key here. Every vendor above ships a fixed,
# small, hand-picked model lineup that changes on kodo's own release schedule
# — a real fit for a compiled-in tuple. OpenRouter is an aggregator with 400+
# models and per-model pricing/context/reasoning-support metadata that
# changes on OpenRouter's own schedule, fetched and cached at runtime instead
# (see kodo.llms._openrouter_catalog). It still needs an entry in
# _CLOUD_VENDOR_DISPLAY/_CLOUD_VENDOR_MODULE below (display name + plugin
# dispatch), just not one here.
_CLOUD_REGISTRY: dict[str, tuple[CloudLLMEntry, ...]] = {
    "anthropic": _ANTHROPIC_MODELS,
    "openai": _OPENAI_MODELS,
    "meta": _META_MODELS,
    "google": _GOOGLE_MODELS,
    "alibaba": _ALIBABA_MODELS,
    "deepseek": _DEEPSEEK_MODELS,
    "kimi": _KIMI_MODELS,
}

_CLOUD_VENDOR_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "meta": "Meta",
    "google": "Google",
    "alibaba": "Alibaba",
    "deepseek": "DeepSeek",
    "kimi": "Kimi",
    "openrouter": "OpenRouter",
}

# Vendor key -> dotted plugin module, mirroring the old LLMEntry.module field
# (now per-vendor instead of per-model, since every model from one vendor uses
# the same plugin implementation).
_CLOUD_VENDOR_MODULE: dict[str, str] = {
    "anthropic": "kodo.llms.anthropic",
    "openai": "kodo.llms.openai",
    "meta": "kodo.llms.meta",
    "google": "kodo.llms.google",
    "alibaba": "kodo.llms.alibaba",
    "deepseek": "kodo.llms.deepseek",
    "kimi": "kodo.llms.kimi",
    "openrouter": "kodo.llms.openrouter",
}

# Vendor key -> the naming-convention prefix that vendor's model_ids use.
# Used only for best-effort vendor->pricing dispatch (kodo.llms._pricing) when
# a Usage.model string needs a vendor looked up from the model id alone (e.g.
# a since-deprecated model no longer in the registry above, still referenced
# by an old session log). NOT used for plugin resolution, which requires an
# exact registry match (kodo/runtime/_engine/_llm.py). "muse-spark" matches
# both "muse-spark-1.2" and the contributor-tier "muse-spark-1.2-contributor"
# rewrite -- kodo.llms.meta._usage.compute_cost does its own finer
# contributor-vs-standard prefix match once dispatched here.
#
# OpenRouter is deliberately absent: its model ids are "<upstream
# provider>/<model>" (e.g. "anthropic/claude-sonnet-4"), with no prefix
# shared across the whole catalog -- and it doesn't need one, since every
# OpenRouter Usage always carries provider_reported_cost
# (kodo.llms._interface.Usage), so it never falls through to this
# prefix-based dispatch at all.
_CLOUD_VENDOR_MODEL_PREFIX: dict[str, str] = {
    "anthropic": "claude",
    "openai": "gpt-",
    "meta": "muse-spark",
    "google": "gemini-",
    "alibaba": "qwen",
    "deepseek": "deepseek-",
    "kimi": "kimi-",
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


def get_cloud_vendor_for_model_prefix(model: str) -> str | None:
    """Best-effort vendor lookup from a bare model id, by naming prefix.

    For pricing dispatch (:mod:`kodo.llms._pricing`) only — not a substitute
    for an exact registry lookup. Returns ``None`` for a local model name or
    any id matching no known vendor's prefix.
    """
    for vendor, prefix in _CLOUD_VENDOR_MODEL_PREFIX.items():
        if model.startswith(prefix):
            return vendor
    return None
