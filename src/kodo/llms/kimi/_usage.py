"""Token-usage and dollar-cost accounting for the Kimi plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins.
#
# Source: web research (morphllm.com, benchlm.ai, requesty.ai, costgoat.com,
# openrouter.ai, as of 2026-08-16) -- Moonshot's own platform pricing page was
# cross-checked against several aggregators, giving these figures the same
# "hand-picked from external sources" epistemic status as
# alibaba/_usage.py's/deepseek/_usage.py's/openai/_usage.py's/meta/_usage.py's/
# google/_usage.py's tables. cache_write is always 0.0: Kimi's automatic
# prefix-based context caching (https://platform.kimi.ai/docs/guide/use-context-caching-feature-of-kimi-api)
# has no separate cache-write charge, only a discounted cache *read* -- same
# posture as DeepSeek/Alibaba/Google.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("kimi-k3", (3.00, 15.00, 0.0, 0.30)),
    ("kimi-k2.7-code", (0.95, 4.00, 0.0, 0.19)),
]

# Non-Kimi models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the Kimi plugin.

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
