"""Token-usage and dollar-cost accounting for the DeepSeek plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins.
#
# Source: web research (morphllm.com, cloudzero.com, flowith.io, benchlm.ai,
# openrouter.ai, as of 2026-08-16) -- DeepSeek's own pricing page numbers were
# cross-checked against several aggregators that disagreed on the exact
# cache-hit/cache-miss ratio, so these figures carry the same "hand-picked
# from external sources" epistemic status as alibaba/_usage.py's,
# openai/_usage.py's, meta/_usage.py's and google/_usage.py's tables.
# cache_write is always 0.0: DeepSeek's automatic disk-backed context caching
# (https://api-docs.deepseek.com/guides/kv_cache) has no separate cache-write
# charge, only a discounted cache *read*. The two models' cache-read figures
# below are the concrete numbers found per-model rather than a single shared
# ratio -- one source described a rough "~1/10 of standard input" rule of
# thumb for Pro that these two don't quite follow (V4 Flash's cache_read is
# ~1/50 of its input price, V4 Pro's is ~1/120), which reads as ordinary
# aggregator noise rather than a real per-model policy difference; flagged
# here rather than silently reconciled.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("deepseek-v4-pro", (0.435, 0.87, 0.0, 0.003625)),
    ("deepseek-v4-flash", (0.14, 0.28, 0.0, 0.0028)),
]

# Non-DeepSeek models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the DeepSeek plugin.

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
