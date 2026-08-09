"""Thinking-tier families: base_llm -> which reasoning-tiering mechanism (if
any) that model's GGUF supports. See doc/LLM_REGISTRY.md and
doc/LOCAL_INFERENCE.md for the llama.cpp mechanism each one rides on.
"""

from __future__ import annotations

import logging

__all__ = [
    "GPT_OSS_REASONING_EFFORT_FAMILY",
    "QWEN_REASONING_BUDGET_FAMILY",
    "QWEN_TIER_TOKEN_BUDGETS",
    "REASONING_BUDGET_MESSAGE",
    "RESERVED_REASONING_CAP_ARGS",
    "local_thinking_default_tier",
    "local_thinking_family",
    "local_thinking_tiers",
]

_log = logging.getLogger(__name__)

#: base_llm values launched with an explicit ``--reasoning-budget -1`` CLI
#: flag (see :func:`kodo.llms.llamacpp.ensure_llama_running`), which makes the
#: per-request ``thinking_budget_tokens`` override effective. All support a
#: shared 6-tier scale (Minimal..Unlimited); Qwen35-9B additionally needs
#: ``chat_template_kwargs.enable_thinking=true`` per request since its chat
#: template has thinking off by default (the other members think by default).
QWEN_REASONING_BUDGET_FAMILY: frozenset[str] = frozenset(
    {
        "Qwen36-27B",
        "Qwen36-35B-A3B",
        "Qwen35-9B",
        "Gemma4-26B-A4B",
        "Gemma4-31B",
        "Ornith10-35B-A3B",
        "Ornith10-9B",
        "Laguna-S-2.1",
        "Laguna-XS-2.1",
    }
)

#: base_llm values that take a per-request nested
#: ``chat_template_kwargs.reasoning_effort`` ("low"|"medium"|"high"); no
#: launch-time CLI flags needed — the model's own default is "medium".
GPT_OSS_REASONING_EFFORT_FAMILY: frozenset[str] = frozenset({"GPT-OSS-120B", "GPT-OSS-20B"})

_QWEN_TIERS: tuple[str, ...] = ("minimal", "low", "medium", "high", "huge", "unlimited")
_GPT_OSS_TIERS: tuple[str, ...] = ("low", "medium", "high")

#: Per-base_llm token budget for each Qwen-family tier, including
#: "unlimited" — despite the name this is now a real finite cap (1.5x the
#: "huge" tier), not the ``-1``/no-limit sentinel it used to be. A true
#: uncapped budget left ``max_tokens`` sizing (see ``_llama.py``) with no
#: number to size against, and every "huge"/"unlimited" turn could exhaust the
#: *entire* per-request ``max_tokens`` on reasoning alone with zero room left
#: for ``--reasoning-budget-message`` to actually print (see doc/
#: LOCAL_INFERENCE.md §2a). Best-effort starting point, not sourced from an
#: official per-model spec — see doc/LLM_REGISTRY.md for the rationale behind
#: each family's scale (e.g. Ornith10-35B-A3B's RL-trained thinking efficiency vs.
#: Qwen35-9B's smaller/weaker-model verbosity). Expect these to be retuned
#: after real usage.
QWEN_TIER_TOKEN_BUDGETS: dict[str, dict[str, int]] = {
    "Qwen36-27B": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
        "unlimited": 24576,
    },
    "Qwen36-35B-A3B": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
        "unlimited": 24576,
    },
    "Qwen35-9B": {
        "minimal": 2048,
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "huge": 32768,
        "unlimited": 49152,
    },
    "Gemma4-26B-A4B": {
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
        "unlimited": 24576,
    },
    "Gemma4-31B": {
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
        "unlimited": 24576,
    },
    "Ornith10-35B-A3B": {
        "minimal": 256,
        "low": 768,
        "medium": 1536,
        "high": 3072,
        "huge": 6144,
        "unlimited": 9216,
    },
    "Ornith10-9B": {
        "minimal": 2048,
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "huge": 32768,
        "unlimited": 49152,
    },
    "Laguna-S-2.1": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
        "unlimited": 24576,
    },
    "Laguna-XS-2.1": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
        "unlimited": 24576,
    },
}

_QWEN_DEFAULT_TIER = "unlimited"
_GPT_OSS_DEFAULT_TIER = "medium"

#: Injected before the end-of-thinking tag whenever a finite Qwen-family
#: budget is exhausted (``--reasoning-budget-message``).
REASONING_BUDGET_MESSAGE = (
    "I've reached the limit of my thinking budget, so I'll stop reasoning here "
    "and give the best answer I can based on what I've worked out so far."
)

#: CLI flags kodo manages automatically per session for the Qwen
#: reasoning-budget family (``ensure_llama_running``,
#: ``kodo/llms/llamacpp/_manager.py``) — no flavor, predefined or custom, may
#: set these itself. Any occurrence in flavor-supplied ``llama_args`` is
#: silently dropped before the flavor is even persisted (see
#: :func:`~kodo.llms.local_registry._flavors.add_flavor`/
#: :func:`~kodo.llms.local_registry._flavors.update_flavor` below), and the
#: correct values are force-assigned again at launch time regardless, in
#: case a flavor saved before this existed still carries them.
RESERVED_REASONING_CAP_ARGS: tuple[str, ...] = (
    "--reasoning-budget",
    "--reasoning-budget-message",
)


def _strip_reasoning_cap_args(llama_args: dict[str, str]) -> dict[str, str]:
    """Drop any :data:`RESERVED_REASONING_CAP_ARGS` key from *llama_args*.

    Used by
    :func:`~kodo.llms.local_registry._flavors.add_flavor`/
    :func:`~kodo.llms.local_registry._flavors.update_flavor` so a flavor's
    own CLI args can never carry (and therefore never later silently
    defeat) the per-session reasoning-budget mechanism — see
    :data:`RESERVED_REASONING_CAP_ARGS`.
    """
    stripped = {k: v for k, v in llama_args.items() if k not in RESERVED_REASONING_CAP_ARGS}
    if len(stripped) != len(llama_args):
        dropped = sorted(set(llama_args) - set(stripped))
        _log.warning(
            "Dropped reserved reasoning-cap arg(s) %s from flavor llama_args — these are "
            "managed automatically per session, not by flavors",
            dropped,
        )
    return stripped


def local_thinking_family(base_llm: str) -> str | None:
    """Which reasoning-tiering mechanism *base_llm* uses, if any.

    Args:
        base_llm (str): The ``LocalLLMEntry.base_llm`` slug to look up.

    Returns:
        str | None: ``"qwen_reasoning_budget"``, ``"gpt_oss_reasoning_effort"``,
        or ``None`` (includes every ``custom_*`` entry, whose ``base_llm`` is
        always ``""``).
    """
    if base_llm in QWEN_REASONING_BUDGET_FAMILY:
        return "qwen_reasoning_budget"
    if base_llm in GPT_OSS_REASONING_EFFORT_FAMILY:
        return "gpt_oss_reasoning_effort"
    return None


def local_thinking_tiers(base_llm: str) -> tuple[str, ...]:
    """The ordered tier slugs *base_llm* supports, or ``()`` if none.

    Args:
        base_llm (str): The ``LocalLLMEntry.base_llm`` slug to look up.

    Returns:
        tuple[str, ...]: Ordered tier slugs, lowest intensity first.
    """
    family = local_thinking_family(base_llm)
    if family == "qwen_reasoning_budget":
        return _QWEN_TIERS
    if family == "gpt_oss_reasoning_effort":
        return _GPT_OSS_TIERS
    return ()


def local_thinking_default_tier(base_llm: str) -> str:
    """The default tier slug for *base_llm*'s thinking family.

    Args:
        base_llm (str): The ``LocalLLMEntry.base_llm`` slug to look up.

    Returns:
        str: ``"unlimited"`` for the Qwen family, ``"medium"`` for GPT-OSS,
        or ``""`` if *base_llm* has no thinking family.
    """
    family = local_thinking_family(base_llm)
    if family == "gpt_oss_reasoning_effort":
        return _GPT_OSS_DEFAULT_TIER
    if family == "qwen_reasoning_budget":
        return _QWEN_DEFAULT_TIER
    return ""
