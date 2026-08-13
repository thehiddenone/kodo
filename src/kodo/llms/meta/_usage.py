"""Token-usage and dollar-cost accounting for the Meta plugin."""

from __future__ import annotations

from kodo.llms._interface import Usage

__all__ = ["compute_cost"]

# Pricing table: (input, output, cache_write, cache_read) USD per million tokens.
# Keyed by model name prefix; first prefix match wins -- the contributor
# variant's id ("muse-spark-1.2-contributor") also starts with the standard
# id's prefix ("muse-spark-1.2"), so it MUST be listed first or every
# contributor-tier call would be priced at standard rates.
#
# Source: https://dev.meta.ai/docs/pricing-rate-limits/ (as of 2026-08-12).
# The contributor tier trades a heavy discount for permission to train future
# Meta models on the traffic -- see kodo/llms/meta/_muse.py for where the
# model id gets the "-contributor" suffix that selects this row. cache_write
# is always 0.0, same reasoning as openai/_usage.py: Meta's Model API caching
# is automatic (Responses-API-shaped), so only a cache *read* is discounted.
_PRICING: list[tuple[str, tuple[float, float, float, float]]] = [
    ("muse-spark-1.2-contributor", (0.10, 0.20, 0.0, 0.002)),
    ("muse-spark-1.2", (1.25, 4.25, 0.0, 0.15)),
]

# Non-Meta models (e.g. local llama.cpp inference) have no API cost.
_FALLBACK: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _get_pricing(model: str) -> tuple[float, float, float, float]:
    for prefix, rates in _PRICING:
        if model.startswith(prefix):
            return rates
    return _FALLBACK


def compute_cost(usage: Usage) -> float:
    """Compute the USD cost for a single LLM call.

    Args:
        usage: Token usage record returned by the Meta plugin. ``usage.model``
            already carries the ``-contributor`` suffix when that tier was
            active for the call (see :mod:`kodo.llms.meta._muse`), so the
            correct discounted row is picked automatically.

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
