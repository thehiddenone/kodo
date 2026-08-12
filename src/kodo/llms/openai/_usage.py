"""Token-usage and dollar-cost accounting for the OpenAI plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins.
#
# Source: https://developers.openai.com/api/docs/models (as of 2026-08-12).
# That page does not publish cached-input pricing; the cache_read values below
# are a placeholder ESTIMATE mirroring OpenAI's long-standing real-world
# ~50%-off cached-input discount (cache_read == input / 2) -- same hand-picked
# spirit as anthropic/_usage.py's table. cache_write is always 0.0: creating a
# Responses API cache entry is free (automatic), only reading one is
# discounted -- there is no separate write charge to price.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("gpt-5.6-sol", (5.00, 30.00, 0.0, 2.50)),
    ("gpt-5.6-terra", (2.00, 12.00, 0.0, 1.00)),
    ("gpt-5.6-luna", (0.20, 1.20, 0.0, 0.10)),
    # Fallback for any other gpt-5.6-* variant not in the registry yet
    # (e.g. a since-deprecated or newly-added SKU still referenced by an old
    # session log) -- price at Terra's mid-tier rate.
    ("gpt-5.6", (2.00, 12.00, 0.0, 1.00)),
]

# Non-OpenAI models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the OpenAI plugin.

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
