"""Token-usage and dollar-cost accounting for the Alibaba plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins.
#
# Source: web research (eesel.ai, technode.global, datacamp.com, apidog.com,
# secondtalent.com, as of 2026-08-16) -- Alibaba Cloud Model Studio's own
# pricing page was not directly reachable at the time of writing, so these
# figures carry the same "hand-picked from external sources" epistemic status
# as openai/_usage.py's, meta/_usage.py's and google/_usage.py's tables.
# cache_write is always 0.0: Model Studio's automatic "context cache" feature
# (https://www.alibabacloud.com/help/en/model-studio/context-cache) has no
# separate cache-write charge, only a discounted cache *read* -- generally
# ~20% of the standard input price; qwen3.8-max's cache_read below uses the
# one concrete figure found ($0.25/M, an ~8x discount off its $2.00/M input
# price), the other two rows apply that same ~20% ratio for lack of a
# published per-model figure.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("qwen3.8-max", (2.00, 6.00, 0.0, 0.25)),
    ("qwen3.8-plus", (0.40, 1.20, 0.0, 0.08)),
    ("qwen3.8-flash", (0.05, 0.40, 0.0, 0.01)),
]

# Non-Alibaba models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the Alibaba plugin.

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
