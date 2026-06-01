from __future__ import annotations

__all__ = [
    "get_llm_registry",
]

_LLM_REGISTRY: dict[str, dict[str, str]] = {
    "claude-opus-4-8": {
        "residence": "cloud",
        "capability": "high",
        "module": "kodo.llms.anthropic",
        "model_id": "claude-opus-4-8",
        "description": "Anthropic Claude Opus 4.8",
    },
    "claude-opus-4-7": {
        "residence": "cloud",
        "capability": "high",
        "module": "kodo.llms.anthropic",
        "model_id": "claude-opus-4-7",
        "description": "Anthropic Claude Opus 4.7",
    },
    "claude-opus-4-6": {
        "residence": "cloud",
        "capability": "high",
        "module": "kodo.llms.anthropic",
        "model_id": "claude-opus-4-6",
        "description": "Anthropic Claude Opus 4.6",
    },
    "claude-sonnet-4-6": {
        "residence": "cloud",
        "capability": "medium",
        "module": "kodo.llms.anthropic",
        "model_id": "claude-sonnet-4-6",
        "description": "Anthropic Claude Sonnet 4.6",
    },
    "claude-haiku-4-5": {
        "residence": "cloud",
        "capability": "low",
        "module": "kodo.llms.anthropic",
        "model_id": "claude-haiku-4-5-20251001",
        "description": "Anthropic Claude Haiku 4.5",
    },
    "llamacpp-qwen36-27b": {
        "residence": "local",
        "module": "kodo.llms.llamacpp",
        "description": "Qwen 3.6 27B UD-Q4_K_XL — local inference via llama-server",
        "repo_id": "unsloth/Qwen3.6-27B-MTP-GGUF",
        "filename": "Qwen3.6-27B-UD-Q4_K_XL.gguf",
    },
}


def get_llm_registry() -> dict[str, dict[str, str]]:
    return _LLM_REGISTRY.copy()
