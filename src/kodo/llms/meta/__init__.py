"""Meta LLM plugin — Muse Spark 1.2 via Meta's OpenAI-Responses-API-compatible Model API."""

from ._muse import MusePlugin
from ._retry import RetryExhaustedError, UnrecoverableError
from ._usage import compute_cost

__all__ = [
    "MusePlugin",
    "UnrecoverableError",
    "RetryExhaustedError",
    "compute_cost",
]
