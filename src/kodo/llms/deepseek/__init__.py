"""DeepSeek LLM plugin — V4 Flash/Pro via DeepSeek's OpenAI-compatible endpoint."""

from ._deepseek import DeepSeekPlugin
from ._retry import RetryExhaustedError, UnrecoverableError
from ._usage import compute_cost

__all__ = [
    "DeepSeekPlugin",
    "UnrecoverableError",
    "RetryExhaustedError",
    "compute_cost",
]
