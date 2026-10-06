"""Thinking-tier families: base_llm -> which reasoning-tiering mechanism (if
any) that model's GGUF supports. See doc/LLM_REGISTRY.md and
doc/LOCAL_INFERENCE.md for the llama.cpp mechanism each one rides on.
"""

from __future__ import annotations

__all__ = [
    "GPT_OSS_REASONING_EFFORT_FAMILY",
    "MUSE_GLIMMER_REASONING_STRENGTH_FAMILY",
    "QWEN4EXP_REASONING_EFFORT_FAMILY",
    "QWEN_REASONING_BUDGET_FAMILY",
    "QWEN_TIER_TOKEN_BUDGETS",
    "REASONING_BUDGET_MESSAGE",
    "UNLIMITED_THINKING_TIER",
    "RESERVED_REASONING_CAP_ARGS",
    "local_thinking_default_tier",
    "local_thinking_family",
    "local_thinking_tiers",
]

#: base_llm values launched with an explicit ``--reasoning-budget -1`` CLI
#: flag (see :func:`kodo.llms.llamacpp.ensure_llama_running`), which makes the
#: per-request ``thinking_budget_tokens`` override effective. All support a
#: shared 6-tier scale (Minimal..Unlimited); Qwen35-9B additionally needs
#: ``chat_template_kwargs.enable_thinking=true`` per request since its chat
#: template has thinking off by default (the other members think by default).
QWEN_REASONING_BUDGET_FAMILY: frozenset[str] = frozenset(
    {
        "Qwen38-27B",
        "Qwen36-27B",
        "Qwen36-35B-A3B",
        "Qwen35-9B",
        "Gemma4-26B-A4B",
        "Gemma4-31B",
        "Ornith15-35B-A3B",
        "Ornith15-9B",
        "Ornith10-35B-A3B",
        "Ornith10-9B",
        "Laguna-S-2.1",
        "Laguna-XS-2.1",
        "Nanbeige4.2-3B",
        "Nemotron35-30B-A3B",
    }
)

#: base_llm values that take a per-request nested
#: ``chat_template_kwargs.reasoning_effort`` ("low"|"medium"|"high"); no
#: launch-time CLI flags needed — the model's own default is "medium".
GPT_OSS_REASONING_EFFORT_FAMILY: frozenset[str] = frozenset({"GPT-OSS-120B", "GPT-OSS-20B"})

#: base_llm values that take a per-request nested
#: ``chat_template_kwargs.reasoning_effort`` of ``"low"|"medium"|"xhigh"`` —
#: the same *mechanism* as :data:`GPT_OSS_REASONING_EFFORT_FAMILY` but a
#: different *vocabulary*, which is why it is a family of its own rather than
#: a second member of that one. Qwen3.8-Flash-Next's chat template raises a
#: Jinja exception on any value outside those three (there is no ``"high"``),
#: so the two tier lists can never be merged; its own default is ``xhigh``.
#: No launch-time CLI flags: like GPT-OSS, the tiering is purely a per-request
#: template argument, so ``--reasoning-budget`` plays no part.
QWEN4EXP_REASONING_EFFORT_FAMILY: frozenset[str] = frozenset({"Qwen38-Flash-Next"})

#: base_llm values that take a per-request nested
#: ``chat_template_kwargs.reasoning_strength`` of
#: ``"low"|"medium"|"high"|"xhigh"``. The same per-request template-argument
#: mechanism as the two reasoning-*effort* families, but under a different
#: field *name*: Muse Glimmer's embedded chat template reads
#: ``reasoning_strength`` (and renders it as the ``Reasoning strength: <value>.``
#: system-prompt line its model card documents), and llama.cpp does not map the
#: OpenAI-style ``reasoning_effort`` onto it. The four-tier vocabulary also
#: matches neither effort family. No launch-time CLI flags; the template's own
#: default is ``high``.
MUSE_GLIMMER_REASONING_STRENGTH_FAMILY: frozenset[str] = frozenset({"MuseGlimmer-30B"})

#: The Qwen family's top tier, and the only one with no budget at all: it sends
#: ``thinking_budget_tokens: -1`` and no ``max_tokens``, so llama-server
#: generates until the model ends its thinking itself or the slot's context is
#: full (doc/LOCAL_INFERENCE.md §2a). Every other tier is a finite cap.
UNLIMITED_THINKING_TIER = "unlimited"

_QWEN_TIERS: tuple[str, ...] = (
    "minimal",
    "low",
    "medium",
    "high",
    "huge",
    UNLIMITED_THINKING_TIER,
)
_GPT_OSS_TIERS: tuple[str, ...] = ("low", "medium", "high")
_QWEN4EXP_TIERS: tuple[str, ...] = ("low", "medium", "xhigh")
_MUSE_GLIMMER_TIERS: tuple[str, ...] = ("low", "medium", "high", "xhigh")

#: Per-base_llm token budget for each finite Qwen-family tier — every tier but
#: :data:`UNLIMITED_THINKING_TIER`, which has no budget and is deliberately
#: absent here. ``_llama.py`` sizes each request's ``max_tokens`` as the
#: budget plus headroom, so a capped tier always leaves room for
#: ``--reasoning-budget-message`` and the answer after it (see doc/
#: LOCAL_INFERENCE.md §2a). Best-effort starting point, not sourced from an
#: official per-model spec — see doc/LLM_REGISTRY.md for the rationale behind
#: each family's scale (e.g. the Ornith 35B-A3B builds' RL-trained thinking efficiency vs.
#: Qwen35-9B's smaller/weaker-model verbosity). Expect these to be retuned
#: after real usage.
QWEN_TIER_TOKEN_BUDGETS: dict[str, dict[str, int]] = {
    "Qwen38-27B": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Qwen36-27B": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Qwen36-35B-A3B": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Qwen35-9B": {
        "minimal": 2048,
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "huge": 32768,
    },
    "Gemma4-26B-A4B": {
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Gemma4-31B": {
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Ornith15-35B-A3B": {
        "minimal": 256,
        "low": 768,
        "medium": 1536,
        "high": 3072,
        "huge": 6144,
    },
    "Ornith15-9B": {
        "minimal": 2048,
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "huge": 32768,
    },
    "Ornith10-35B-A3B": {
        "minimal": 256,
        "low": 768,
        "medium": 1536,
        "high": 3072,
        "huge": 6144,
    },
    "Ornith10-9B": {
        "minimal": 2048,
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "huge": 32768,
    },
    "Laguna-S-2.1": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Laguna-XS-2.1": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
    "Nanbeige4.2-3B": {
        "minimal": 2048,
        "low": 4096,
        "medium": 8192,
        "high": 16384,
        "huge": 32768,
    },
    "Nemotron35-30B-A3B": {
        "minimal": 512,
        "low": 1536,
        "medium": 4096,
        "high": 8192,
        "huge": 16384,
    },
}

_QWEN_DEFAULT_TIER = "high"
_GPT_OSS_DEFAULT_TIER = "medium"
#: Qwen3.8-Flash-Next's chat template defaults ``reasoning_effort`` to
#: ``xhigh`` when the field is absent, so kodo starts where the model does.
_QWEN4EXP_DEFAULT_TIER = "xhigh"
#: Muse Glimmer's chat template defaults ``reasoning_strength`` to ``high``
#: when the field is absent, so kodo starts where the model does.
_MUSE_GLIMMER_DEFAULT_TIER = "high"

#: Injected before the end-of-thinking tag whenever a finite Qwen-family
#: budget is exhausted (``--reasoning-budget-message``).
REASONING_BUDGET_MESSAGE = (
    "I've reached the limit of my thinking budget, so I'll stop reasoning here "
    "and give the best answer I can based on what I've worked out so far."
)

#: CLI flags kodo manages automatically per session for the Qwen
#: reasoning-budget family (``ensure_llama_running``,
#: ``kodo/llms/llamacpp/_manager.py``) — no profile may set these itself.
#: They are part of :data:`~kodo.llms.local_registry.RESERVED_LLAMA_ARGS`, so
#: any occurrence in profile-supplied ``llama_args`` is dropped before the
#: profile is persisted (:func:`~kodo.llms.local_registry.strip_reserved_llama_args`),
#: and the correct values are force-assigned again at launch time regardless,
#: in case a profile saved before this existed still carries them.
RESERVED_REASONING_CAP_ARGS: tuple[str, ...] = (
    "--reasoning-budget",
    "--reasoning-budget-message",
)


def local_thinking_family(base_llm: str) -> str | None:
    """Which reasoning-tiering mechanism *base_llm* uses, if any.

    Args:
        base_llm (str): The ``LocalLLMEntry.base_llm`` slug to look up.

    Returns:
        str | None: ``"qwen_reasoning_budget"``,
        ``"gpt_oss_reasoning_effort"``, ``"qwen4exp_reasoning_effort"``,
        ``"muse_glimmer_reasoning_strength"``, or ``None`` (includes every
        ``custom_*`` entry, whose ``base_llm`` is always ``""``).
    """
    if base_llm in QWEN_REASONING_BUDGET_FAMILY:
        return "qwen_reasoning_budget"
    if base_llm in GPT_OSS_REASONING_EFFORT_FAMILY:
        return "gpt_oss_reasoning_effort"
    if base_llm in QWEN4EXP_REASONING_EFFORT_FAMILY:
        return "qwen4exp_reasoning_effort"
    if base_llm in MUSE_GLIMMER_REASONING_STRENGTH_FAMILY:
        return "muse_glimmer_reasoning_strength"
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
    if family == "qwen4exp_reasoning_effort":
        return _QWEN4EXP_TIERS
    if family == "muse_glimmer_reasoning_strength":
        return _MUSE_GLIMMER_TIERS
    return ()


def local_thinking_default_tier(base_llm: str) -> str:
    """The default tier slug for *base_llm*'s thinking family.

    Args:
        base_llm (str): The ``LocalLLMEntry.base_llm`` slug to look up.

    Returns:
        str: ``"high"`` for the Qwen reasoning-budget family, ``"medium"``
        for GPT-OSS, ``"xhigh"`` for Qwen3.8-Flash-Next, ``"high"`` for Muse
        Glimmer, or ``""`` if *base_llm* has no thinking family.
    """
    family = local_thinking_family(base_llm)
    if family == "gpt_oss_reasoning_effort":
        return _GPT_OSS_DEFAULT_TIER
    if family == "qwen4exp_reasoning_effort":
        return _QWEN4EXP_DEFAULT_TIER
    if family == "muse_glimmer_reasoning_strength":
        return _MUSE_GLIMMER_DEFAULT_TIER
    if family == "qwen_reasoning_budget":
        return _QWEN_DEFAULT_TIER
    return ""
