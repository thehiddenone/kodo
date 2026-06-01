"""Token-usage and dollar-cost accounting for the Anthropic plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("claude-opus-4", (15.0, 75.0, 18.75, 1.50)),
    ("claude-sonnet-4", (3.0, 15.0, 3.75, 0.30)),
    ("claude-haiku-4", (0.80, 4.0, 1.0, 0.08)),
    # Fallback for any other claude-* variant — use Sonnet-tier pricing
    ("claude", (3.0, 15.0, 3.75, 0.30)),
]

# Non-Claude models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the Anthropic plugin.

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
