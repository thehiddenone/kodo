"""OpenRouter LLM plugin — 400+ upstream models via OpenRouter's OpenAI-compatible endpoint."""

from ._openrouter import OpenRouterPlugin
from ._retry import RetryExhaustedError, UnrecoverableError

__all__ = [
    "OpenRouterPlugin",
    "UnrecoverableError",
    "RetryExhaustedError",
]
