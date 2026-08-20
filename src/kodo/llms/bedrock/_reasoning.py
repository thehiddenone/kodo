"""Thinking-tier translation for Bedrock's heterogeneous model catalog.

Bedrock's ``bedrock_effort`` family (:mod:`kodo.llms._cloud_thinking`) has the
same five tiers as Anthropic's direct API, because on Bedrock the models that
expose a graded reasoning control at all are the Claude ones, and their scale
*is* that scale (<https://docs.aws.amazon.com/bedrock/latest/userguide/
claude-messages-adaptive-thinking.html>).

**Why this dispatches per model instead of sending one unified knob.**
OpenRouter — the other aggregator here, and the template this one follows
(doc/LLM_REGISTRY.md §3b) — has a single provider-level ``reasoning.effort``
that OpenRouter itself translates per upstream model and that models without
reasoning silently ignore. Bedrock is *nearly* comparable but not quite, in a
way worth stating precisely:

* Converse **does** have a top-level ``outputConfig.effort`` (verified in
  botocore 1.43.73's own service model, which is what the SDK actually sends;
  note it is absent from the published API reference page for ``OutputConfig``,
  which still lists only ``textFormat``). It is a server-validated free-form
  string accepting exactly ``low``/``medium``/``high``/``xhigh``/``max`` — the
  same five tiers as this family.
* But it is **not sufficient on its own**: adaptive thinking is enabled by
  ``thinking: {"type": "adaptive"}``, which has *no* top-level equivalent and
  must still go through ``additionalModelRequestFields``. And that field is
  **passthrough** — Bedrock validates it against the target model and returns
  a ``ValidationException`` for anything that model doesn't define, so
  Anthropic's ``thinking``/``output_config`` sent to Nova or Llama is a hard
  400, not a no-op.
* Whether ``outputConfig.effort`` is *ignored* or *rejected* on a non-Claude
  model is not documented, and its own doc note ("when extended thinking is
  disabled, the effort level is capped at ``high``") is Claude-flavoured. That
  is the one unknown blocking a switch to it, and it cannot be settled without
  a real AWS account to test against.

So this module sends AWS's own documented Converse recipe for adaptive
thinking, and **emits nothing at all for families kodo has not verified**.
Migrating to top-level ``outputConfig.effort`` for the non-Claude majority is
the obvious follow-up once someone can confirm its behaviour there.

That makes the control a documented no-op on non-Claude Bedrock models, which
the client says out loud in the tier tooltips
rather than hiding (kodo-vsix ``ModeControls.tsx``'s Bedrock caveat) — the
same honesty trade OpenRouter's "models that don't support reasoning ignore
this" caveat makes.

Matching is by **substring**, not equality: a Bedrock model id may carry a
cross-region inference-profile prefix (``us.``/``eu.``/``apac.``, §3b) and a
version suffix (``-v1:0``) around the same family name, so
``us.anthropic.claude-opus-5-v1:0`` and ``anthropic.claude-opus-5`` must both
match one marker.
"""

from __future__ import annotations

__all__ = ["DEFAULT_TIER", "TIERS", "max_tokens_for", "reasoning_fields_for"]

#: The ``bedrock_effort`` tier slugs, lowest first — kept in lockstep with
#: :data:`kodo.llms._cloud_thinking.CLOUD_THINKING_FAMILIES`'s ``bedrock``
#: entry (``test_cloud_thinking.py`` asserts the two agree).
TIERS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Claude's own documented default effort on Bedrock ("high (default)"), so a
#: session that never touches the control sends what it would have sent
#: anyway.
DEFAULT_TIER = "high"

# Model families that accept `thinking: {"type": "adaptive"}` plus
# `output_config.effort` through additionalModelRequestFields. Straight from
# AWS's adaptive-thinking model table; anything not listed gets no reasoning
# fields at all.
_ADAPTIVE_EFFORT_MARKERS: tuple[str, ...] = (
    "anthropic.claude-fable-5",
    "anthropic.claude-mythos-5",
    "anthropic.claude-mythos-preview",
    "anthropic.claude-opus-4-6",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-5",
    "anthropic.claude-sonnet-4-6",
)

# `xhigh` and `max` are documented as Claude Opus 5 / Opus 4.6 only — every
# other adaptive model 400s on them. Clamping to `high` keeps a session-wide
# tier usable across a vendor whose models differ (the tier is vendor-scoped
# by design, see kodo/llms/_cloud_thinking.py's module docstring) instead of
# failing the request outright.
_DEEP_EFFORT_MARKERS: tuple[str, ...] = (
    "anthropic.claude-opus-4-6",
    "anthropic.claude-opus-5",
)

# Mirrors kodo/llms/anthropic/_claude.py's own ceilings, for the same reason:
# effort caps thinking *plus* answer, and a truncated turn trips the watchdog's
# "max_tokens" stop-reason check (runtime/_engine/_watchdog.py). Tiers up to
# and including the `high` default keep Bedrock's per-model default by sending
# no maxTokens at all, which is the safer choice across a catalog whose models
# have wildly different output ceilings.
_DEEP_EFFORT_MAX_TOKENS: dict[str, int] = {
    "xhigh": 16384,
    "max": 32768,
}


def _matches(model: str, markers: tuple[str, ...]) -> bool:
    return any(marker in model for marker in markers)


def _resolve_tier(thinking_level: str | None) -> str:
    """The tier to use — *thinking_level* when valid, the family default otherwise.

    Args:
        thinking_level (str | None): Caller-supplied tier slug, or ``None``.

    Returns:
        str: One of :data:`TIERS`.
    """
    return thinking_level if thinking_level in TIERS else DEFAULT_TIER


def reasoning_fields_for(model: str, thinking_level: str | None) -> dict[str, object]:
    """``additionalModelRequestFields`` for *model* at the session's tier.

    Args:
        model (str): The Bedrock model id or inference-profile id being called.
        thinking_level (str | None): The session's ``bedrock_effort`` tier.

    Returns:
        dict[str, object]: ``{"thinking": ..., "output_config": ...}`` for a
        model documented to accept them, or ``{}`` — which is then omitted
        from the request entirely — for every other family.
    """
    if not _matches(model, _ADAPTIVE_EFFORT_MARKERS):
        return {}
    tier = _resolve_tier(thinking_level)
    if tier in ("xhigh", "max") and not _matches(model, _DEEP_EFFORT_MARKERS):
        tier = "high"
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": tier},
    }


def max_tokens_for(model: str, thinking_level: str | None) -> int | None:
    """Output ceiling for this request, or ``None`` to use the model's own default.

    Args:
        model (str): The Bedrock model id or inference-profile id being called.
        thinking_level (str | None): The session's ``bedrock_effort`` tier.

    Returns:
        int | None: A raised ceiling for the two deep tiers on a model that
        actually honors them; ``None`` otherwise — see
        :data:`_DEEP_EFFORT_MAX_TOKENS`.
    """
    if not _matches(model, _ADAPTIVE_EFFORT_MARKERS) or not _matches(model, _DEEP_EFFORT_MARKERS):
        return None
    return _DEEP_EFFORT_MAX_TOKENS.get(_resolve_tier(thinking_level))
