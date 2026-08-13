"""Token-usage and dollar-cost accounting for the Google plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins.
#
# Source: web research (aipricecompare.org, apidog.com, eesel.ai, benchlm.ai, as
# of 2026-08-12) -- Google does not publish a single authoritative pricing page
# for this generation at the time of writing, so these figures carry the same
# "hand-picked from external sources" epistemic status as openai/_usage.py's
# and meta/_usage.py's tables. cache_write is always 0.0: Gemini's OpenAI-
# compatible endpoint exposes no separate cache-write charge, only a
# discounted cache *read*.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("gemini-3.6-flash", (1.50, 7.50, 0.0, 0.15)),
    ("gemini-3.5-flash-lite", (0.30, 2.50, 0.0, 0.03)),
]

# Non-Google models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the Google plugin.

    Returns:
        float: Estimated dollar cost (non-negative).
    """
    inp, out, cw, cr = _get_pricing(usage.model)
    return (
        usage.input_tokens * inp / 1_000_000
        + usage.output_tokens * out / 1_000_000
        + usage.cache_write_tokens * cw / 1_000_000
        + usage.cache_read_tokens * cr / 1_000_000
    )
