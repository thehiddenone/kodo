"""Kimi (Moonshot AI) LLM plugin — K3/K2.7 Code via Kimi's OpenAI-compatible endpoint."""

from ._kimi import KimiPlugin
from ._retry import RetryExhaustedError, UnrecoverableError
from ._usage import compute_cost

__all__ = [
    "KimiPlugin",
    "UnrecoverableError",
    "RetryExhaustedError",
    "compute_cost",
]
