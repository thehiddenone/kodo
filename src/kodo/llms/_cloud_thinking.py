"""Cloud thinking-tier families: vendor -> which reasoning-tiering mechanism
that vendor's API exposes, the tier slugs it accepts, and its default tier.

The cloud counterpart of :mod:`kodo.llms.local_registry._thinking` (which is
keyed by a local model's ``base_llm``). Every registered cloud vendor has an
entry here, so the session's ``thinking_level`` (doc/SESSIONS.md) is a real,
user-adjustable control on **every** vendor rather than a fixed per-model
constant baked into each plugin.

Two consumers read this table and nothing else:

* :mod:`kodo.runtime._engine._llm` — validates ``thinking_level.set``,
  re-derives the session tier when the active vendor changes, and decides
  whether to splice ``thinking_level`` into a ``stream_query`` call.
* :func:`kodo.server._app._thinking_families_payload` — the client-facing
  ``thinking_families`` catalog (doc/WS_PROTOCOL.md §5.12a), merged with the
  local-registry entries.

Keeping both on one table is deliberate: OpenRouter (the first vendor to get
this control) had its tier list written out twice — once in the engine, once
in the server payload — with a comment on each begging the next editor to
keep them in sync. Eight vendors of that would be a guaranteed drift bug.

**The tier slugs here are the vendor's own API vocabulary, not a normalised
kōdo scale.** They differ per vendor on purpose (Google has no ``max``,
DeepSeek/Kimi have no ``medium``, Alibaba jumps ``medium`` -> ``xhigh``), so
the tier the user sees is always exactly the value the plugin sends. Each
plugin owns the translation from tier to its own request shape, including
per-model clamping where one of a vendor's models accepts a narrower set than
the vendor as a whole (see e.g. :mod:`kodo.llms.anthropic._claude`'s
effort-vs-``budget_tokens`` split).

A tier set is deliberately **vendor-scoped, never model-scoped**: one session
talks to several of a vendor's models in a single turn (each agent capability
tier resolves its own ``model_id`` — see
``LLMPlumbingMixin._resolve_model_key``), so a per-model tier list could not
describe "this session's thinking level" at all.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CLOUD_THINKING_FAMILIES",
    "CloudThinkingFamily",
    "cloud_thinking_default_tier",
    "cloud_thinking_family",
    "cloud_thinking_tiers",
]


@dataclass(frozen=True)
class CloudThinkingFamily:
    """One cloud vendor's reasoning-tier mechanism.

    Attributes:
        family: Family slug sent to clients (``thinking_families[vendor].
            family``). One per vendor rather than one shared cloud family:
            the same slug carries a different tier set and a different
            underlying request parameter per vendor, and the client picks its
            per-tier help text off it (kodo-vsix's ``ModeControls.tsx``).
        tiers: Ordered tier slugs, lowest intensity first. Exactly the values
            the vendor's API accepts for that parameter.
        default: Tier used when the session has no explicit choice — the
            vendor's own documented API default wherever one exists.
    """

    family: str
    tiers: tuple[str, ...]
    default: str


#: vendor key (``kodo.llms._cloud_registry``'s ``CLOUD_REGISTRY`` keys) ->
#: that vendor's thinking-tier family. Every registered vendor appears here;
#: a vendor missing from this map has no thinking control at all (the client
#: renders "Thinking: N/A" and ``thinking_level`` stays ``""``).
CLOUD_THINKING_FAMILIES: dict[str, CloudThinkingFamily] = {
    # Anthropic's `output_config.effort` scale
    # (https://platform.claude.com/docs/en/build-with-claude/effort), which
    # the adaptive-thinking models take alongside `thinking: {"type":
    # "adaptive"}`. Default `high` is the API's own documented default
    # ("setting effort to high produces exactly the same behavior as omitting
    # the parameter entirely"), so an untouched session keeps today's
    # behavior. `xhigh` is not accepted by every model in the registry and
    # the extended-thinking-only models take no effort at all — both are
    # handled inside the plugin, not by narrowing this list.
    "anthropic": CloudThinkingFamily(
        family="anthropic_effort",
        tiers=("low", "medium", "high", "xhigh", "max"),
        default="high",
    ),
    # OpenAI's Responses API `reasoning.effort`
    # (https://developers.openai.com/api/docs/guides/reasoning). The full
    # documented set also includes "none"/"minimal"; kōdo never sends either
    # (no non-reasoning tier — see doc/LLM_REGISTRY.md §4.5a), and the
    # GPT-5.6 generation does not list "minimal" among its supported values
    # anyway. Default `medium` is GPT-5.6's own documented default.
    "openai": CloudThinkingFamily(
        family="openai_reasoning_effort",
        tiers=("low", "medium", "high", "xhigh", "max"),
        default="medium",
    ),
    # Meta's Model API `reasoning.effort` (https://dev.meta.ai/docs/reasoning)
    # — Responses-API-shaped like OpenAI's, but a genuinely different set:
    # Muse Spark supports "minimal" and has no "max", and rejects "none" with
    # a 400 (reasoning cannot be disabled). Meta documents no default value
    # ("the model still reasons at a model-determined level" when omitted);
    # `medium` is what the plugin sent unconditionally before this control
    # existed, kept as the default so an untouched session is unchanged.
    "meta": CloudThinkingFamily(
        family="meta_reasoning_effort",
        tiers=("minimal", "low", "medium", "high", "xhigh"),
        default="medium",
    ),
    # Gemini's thinking levels
    # (https://ai.google.dev/gemini-api/docs/thinking), reached through the
    # OpenAI-compatible endpoint's `reasoning_effort` field, which Google
    # maps 1:1 onto them (https://ai.google.dev/gemini-api/docs/openai).
    # Both registered models (gemini-3.6-flash, gemini-3.5-flash-lite)
    # accept all four; there is no `max`, and reasoning cannot be turned off
    # on a Gemini 3 model at all. Default `medium` is gemini-3.6-flash's own
    # (flash-lite's own default is `minimal`, but the tier is session-wide
    # across a vendor's models — see this module's docstring).
    "google": CloudThinkingFamily(
        family="google_thinking_level",
        tiers=("minimal", "low", "medium", "high"),
        default="medium",
    ),
    # DashScope's `reasoning_effort`, passed in `extra_body` alongside the
    # existing `enable_thinking` switch (https://www.alibabacloud.com/help/en/
    # model-studio/deep-thinking). Only three levels, with a hole where
    # `high` would be: Qwen3.8 documents `low`/`medium`/`xhigh`, default
    # `xhigh`. Note DashScope rejects `reasoning_effort` and `thinking_budget`
    # sent together — kōdo only ever sends the former.
    "alibaba": CloudThinkingFamily(
        family="alibaba_reasoning_effort",
        tiers=("low", "medium", "xhigh"),
        default="xhigh",
    ),
    # DeepSeek's top-level `reasoning_effort`
    # (https://api-docs.deepseek.com/guides/thinking_mode/). The API accepts
    # `medium`/`xhigh` but folds both into `high` server-side, so offering
    # them as separate tiers would be a UI lie: only the three values that
    # map to distinct behavior are listed. Default `high` is DeepSeek's own
    # ("thinking mode is enabled by default, with the default effort being
    # high").
    "deepseek": CloudThinkingFamily(
        family="deepseek_reasoning_effort",
        tiers=("low", "high", "max"),
        default="high",
    ),
    # Moonshot's top-level `reasoning_effort`
    # (https://platform.kimi.ai/docs/guide/kimi-k3-quickstart) — same
    # three-value scale as DeepSeek's, and the same missing `medium`.
    # Default `max` is the OpenPlatform API's own default for K3.
    # `kimi-k2.7-code` supports no effort parameter at all (thinking is
    # permanently on at a fixed level); the plugin drops the tier for that
    # model, and the client's tier tooltips carry the caveat.
    "kimi": CloudThinkingFamily(
        family="kimi_reasoning_effort",
        tiers=("low", "high", "max"),
        default="max",
    ),
    # OpenRouter's unified `reasoning.effort`
    # (https://openrouter.ai/docs/use-cases/reasoning-tokens) — the first
    # vendor to get this control (doc/LLM_REGISTRY.md §3a). Also accepts
    # "minimal"/"xhigh"/"none", which kōdo never sends. Unchanged by the
    # generalisation to every vendor; it just reads its table from here now
    # instead of from two hardcoded copies.
    "openrouter": CloudThinkingFamily(
        family="openrouter_reasoning_effort",
        tiers=("low", "medium", "high", "max"),
        default="medium",
    ),
}


def cloud_thinking_family(vendor: str) -> str | None:
    """Which reasoning-tiering mechanism *vendor* uses, if any.

    Args:
        vendor (str): Cloud vendor key (``"anthropic"``, ``"openai"``, ...).

    Returns:
        str | None: The family slug, or ``None`` for an unknown vendor (or one
        with no thinking mechanism — none today).
    """
    entry = CLOUD_THINKING_FAMILIES.get(vendor)
    return entry.family if entry is not None else None


def cloud_thinking_tiers(vendor: str) -> tuple[str, ...]:
    """The ordered tier slugs *vendor* supports, or ``()`` if none.

    Args:
        vendor (str): Cloud vendor key.

    Returns:
        tuple[str, ...]: Ordered tier slugs, lowest intensity first.
    """
    entry = CLOUD_THINKING_FAMILIES.get(vendor)
    return entry.tiers if entry is not None else ()


def cloud_thinking_default_tier(vendor: str) -> str:
    """The default tier slug for *vendor*'s thinking family.

    Args:
        vendor (str): Cloud vendor key.

    Returns:
        str: The vendor's default tier, or ``""`` if it has no family.
    """
    entry = CLOUD_THINKING_FAMILIES.get(vendor)
    return entry.default if entry is not None else ""
