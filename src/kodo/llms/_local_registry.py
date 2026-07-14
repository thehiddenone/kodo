"""Local LLM registry: hardcoded GGUFs plus a user-managed external collection.

Every entry here runs on llama.cpp — there is no ``residence`` field any more
(the old flat registry's cloud/local split lives in
:mod:`kodo.llms._cloud_registry` now). Entries are discriminated by ``kind``:

- ``hardcoded_hf`` — compiled-in HuggingFace GGUF, shipped with kodo.
- ``custom_hf`` — user-added HuggingFace GGUF (same shape as ``hardcoded_hf``,
  added via the "Add local LLM from huggingface.com" flow). Has an
  installed/not-installed state, resolved the same way as ``hardcoded_hf``
  (presence in :class:`kodo.llms.local.LocalModelManager`'s state, see
  :func:`kodo.llms.llamacpp.get_local_model_manager`).
- ``custom_file`` — user-added local GGUF file that kodo does not own or copy.
  "Installed" means the file exists on disk; per design this is checked once,
  by the kodo-vsix extension, at its own startup — not re-verified here.
- ``custom_server_url`` — user-added link to an already-running llama.cpp (or
  OpenAI-compatible) server kodo does not manage. Always considered
  installed; selecting it as active stops kodo's own managed llama-server
  (see :mod:`kodo.llms.llamacpp._llama`).

The external collection (``custom_*`` entries) plus the global llama-server
binary override path are persisted in ``~/.kodo/etc/local-llm-registry.json``,
owned (read + written) entirely by this module — the kodo-vsix extension only
ever reads it indirectly, via the WS protocol (see doc/LLM_REGISTRY.md).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

__all__ = [
    "GPT_OSS_REASONING_EFFORT_FAMILY",
    "QWEN_REASONING_BUDGET_FAMILY",
    "QWEN_TIER_TOKEN_BUDGETS",
    "REASONING_BUDGET_MESSAGE",
    "LlamaFlavor",
    "LocalLLMEntry",
    "add_flavor",
    "add_local_entry",
    "clear_llama_server_override_path",
    "get_active_flavor",
    "get_effective_flavor_id",
    "get_flavors",
    "get_llama_server_override_path",
    "get_local_registry",
    "local_thinking_default_tier",
    "local_thinking_family",
    "local_thinking_tiers",
    "parse_llama_args",
    "parse_llama_args_text",
    "remove_flavor",
    "remove_local_entry",
    "resolve_context_window",
    "resolve_effective_llama_config",
    "set_active_flavor",
    "set_llama_server_override_path",
    "update_flavor",
]

_log = logging.getLogger(__name__)

_REGISTRY_RELATIVE_PATH = ("etc", "local-llm-registry.json")

_CUSTOM_KINDS = frozenset({"custom_hf", "custom_file", "custom_server_url"})

# ---------------------------------------------------------------------------
# Thinking-tier families: base_llm -> which reasoning-tiering mechanism (if
# any) that model's GGUF supports. See doc/LLM_REGISTRY.md and
# doc/LOCAL_INFERENCE.md for the llama.cpp mechanism each one rides on.
# ---------------------------------------------------------------------------

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
        "Ornith10-35B",
    }
)

#: base_llm values that take a per-request nested
#: ``chat_template_kwargs.reasoning_effort`` ("low"|"medium"|"high"); no
#: launch-time CLI flags needed — the model's own default is "medium".
GPT_OSS_REASONING_EFFORT_FAMILY: frozenset[str] = frozenset({"GPT-OSS-120B", "GPT-OSS-20B"})

_QWEN_TIERS: tuple[str, ...] = ("minimal", "low", "medium", "high", "huge", "unlimited")
_GPT_OSS_TIERS: tuple[str, ...] = ("low", "medium", "high")

#: Per-base_llm token budget for each finite Qwen-family tier ("unlimited" is
#: always -1, not listed here). Best-effort starting point, not sourced from
#: an official per-model spec — see doc/LLM_REGISTRY.md for the rationale
#: behind each family's scale (e.g. Ornith10-35B's RL-trained thinking
#: efficiency vs. Qwen35-9B's smaller/weaker-model verbosity). Expect these to
#: be retuned after real usage.
QWEN_TIER_TOKEN_BUDGETS: dict[str, dict[str, int]] = {
    "Qwen36-27B": {"minimal": 512, "low": 1536, "medium": 4096, "high": 8192, "huge": 16384},
    "Qwen36-35B-A3B": {"minimal": 512, "low": 1536, "medium": 4096, "high": 8192, "huge": 16384},
    "Qwen35-9B": {"minimal": 2048, "low": 4096, "medium": 8192, "high": 16384, "huge": 32768},
    "Gemma4-26B-A4B": {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "huge": 16384},
    "Gemma4-31B": {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "huge": 16384},
    "Ornith10-35B": {"minimal": 256, "low": 768, "medium": 1536, "high": 3072, "huge": 6144},
}

_QWEN_DEFAULT_TIER = "unlimited"
_GPT_OSS_DEFAULT_TIER = "medium"

#: Injected before the end-of-thinking tag whenever a finite Qwen-family
#: budget is exhausted (``--reasoning-budget-message``).
REASONING_BUDGET_MESSAGE = (
    "I've reached the limit of my thinking budget, so I'll stop reasoning here "
    "and give the best answer I can based on what I've worked out so far."
)


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


@dataclass(frozen=True)
class LlamaFlavor:
    """A named, alternate launch configuration for a :class:`LocalLLMEntry`.

    Flavors are the **only** source of llama-server CLI args — a
    :class:`LocalLLMEntry` carries no ``llama_args`` of its own any more.
    Every entry that runs through llama-server gets at least one flavor: a
    ``hardcoded_hf`` entry ships a built-in ``"default"`` flavor (via
    :meth:`default_flavours_field`, unless it explicitly declares its own
    ``flavors=`` tuple — e.g. the F16 GGUFs use :meth:`make_default_kv_fp16`
    instead); a ``custom_hf``/``custom_file`` entry gets a ``"default"``
    flavor seeded from its "Add local LLM" form's ``llama_args`` field at
    creation time (see ``_handle_local_llm_add_huggingface``/``_add_file`` in
    ``kodo/server/_app.py``) — stored as a regular *custom* flavor, not a
    predefined one, since it isn't baked into Python source.

    A flavor lets one GGUF be launched with a completely different set of
    llama-server CLI args than another — e.g. a "1M context" flavor (YaRN
    rope-scaling flags plus a much larger ``context_window``) or a
    "VRAM-tight" flavor (``--n-cpu-moe``/``--override-tensor`` tuned for a
    smaller GPU). Switching the active flavor **fully replaces** the
    previously-active flavor's ``llama_args``/``context_window`` — it does
    not merge two flavors' args together (see
    :func:`resolve_effective_llama_config`); a flavor that wants another
    flavor's KV-cache-type flags (or anything else) must repeat them itself.

    Attributes:
        id: Stable slug, unique among the flavors available for one entry
            (predefined + custom together). Auto-generated from ``name`` for
            custom flavors (see :func:`add_flavor`); hardcoded ones set it
            explicitly as a literal.
        name: Human-readable display name shown in the flavor dropdown.
        description: Optional human-readable explanation.
        llama_args: CLI flags passed verbatim to ``llama-server`` while this
            flavor is active — the complete set, not "extras" layered on top
            of some other default (there is no other default; see
            :class:`kodo.llms.llamacpp.LlamaServerConfig`, which carries only
            server-management fields like host/port/log paths). A
            bare/valueless flag is represented with an empty string value.
            There is no separate ``context_window`` field on a flavor any
            more — the effective context size is *deduced* from this dict's
            own ``-c``/``--ctx-size`` value (falling back to the entry's own
            ``context_window`` if absent/``0``), see
            :func:`resolve_context_window`.
        min_ram: Minimum system RAM (GB) this flavor needs to run, or the
            minimum *unified memory* on Apple Silicon — kodo-vsix reads
            ``detected_vram_gb`` for the unified-memory figure there (see
            ``kodo/llms/_hardware.py``), so a Mac-oriented flavor should set
            ``min_ram`` and leave ``min_vram`` at ``0``. ``0`` means
            "unknown/no requirement — don't check". Editable via
            :func:`add_flavor`/:func:`update_flavor` for a *custom* flavor;
            a predefined flavor's value is fixed at its hardcoded literal,
            since :func:`update_flavor` rejects predefined ``flavor_id``\\s
            outright (see its docstring) — the only way to get a different
            threshold on a predefined flavor's config is to copy it into a
            new custom flavor.
        min_vram: Minimum discrete GPU VRAM (GB) this flavor needs, for a
            Windows/Linux GPU setup (``0`` on Apple Silicon — see
            ``min_ram``). ``0`` means "unknown/no requirement — don't
            check". If both ``min_ram`` and ``min_vram`` are ``0`` the
            hardware-fit check is inactive and the flavor is treated as
            runnable everywhere. Editable the same way as ``min_ram``.
    """

    id: str
    name: str
    description: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)
    min_ram: int = 0
    min_vram: int = 0

    @staticmethod
    def make_default_kv_q8() -> LlamaFlavor:
        return LlamaFlavor(
            id="default",
            name="default",
            description="Default flavor",
            llama_args={
                "--cache-type-k": "q8_0",
                "--cache-type-v": "q8_0",
                "--ctx-size": "0",
                "--n-gpu-layers": "-1",
                "--reasoning-format": "auto",
                "--jinja": "",
            },
        )

    @staticmethod
    def make_default_kv_fp16() -> LlamaFlavor:
        return LlamaFlavor(
            id="default",
            name="default",
            description="Default flavor",
            llama_args={
                "--cache-type-k": "fp16",
                "--cache-type-v": "fp16",
                "--ctx-size": "0",
                "--n-gpu-layers": "-1",
                "--reasoning-format": "auto",
                "--jinja": "",
            },
        )

    @staticmethod
    def default_flavours_field() -> tuple[LlamaFlavor, ...]:
        return (LlamaFlavor.make_default_kv_q8(),)


@dataclass(frozen=True)
class LocalLLMEntry:
    """A single local (llama.cpp) model, hardcoded or user-added.

    Attributes:
        name: Registry key / display name (e.g. ``'llamacpp-qwen36-27b-q4-k-xl'``
            for hardcoded entries, or whatever the user typed when adding a
            custom one). Must be unique across the merged registry.
        kind: ``'hardcoded_hf'``, ``'custom_hf'``, ``'custom_file'``, or
            ``'custom_server_url'``.
        description: Human-readable description.
        repo_id: HuggingFace repository ID (``hardcoded_hf``/``custom_hf`` only).
        filename: GGUF filename inside the HF repository
            (``hardcoded_hf``/``custom_hf`` only).
        context_window: Maximum input-context size in tokens. Falls back to
            the default when unset/non-positive (see
            :func:`kodo.llms.get_context_window`); the active flavor's own
            ``-c``/``--ctx-size`` launch arg (if positive) takes precedence
            over this one, see :func:`resolve_context_window`.
        flavors: Predefined alternate launch configurations shipped with this
            entry (see :class:`LlamaFlavor`) — ``hardcoded_hf`` only.
            Entries without an explicit ``flavors=`` literal get exactly one,
            via this dataclass field's default factory
            (:meth:`LlamaFlavor.default_flavours_field`); the few that need a
            different built-in default (e.g. the F16 GGUFs' KV cache) set
            ``flavors=`` explicitly instead. Always ``()`` for every
            ``custom_*`` kind — :func:`add_local_entry` forces this
            regardless of what's passed in (a caller-supplied non-empty value
            would otherwise silently shadow a same-id custom flavor added
            later), and ``_entry_from_json`` passes ``flavors=()`` explicitly
            too, since loading from disk doesn't go through
            ``add_local_entry``. A custom entry's launch args live entirely
            in the *custom* flavor store instead (see
            :func:`get_flavors`/:func:`add_flavor`) — a ``custom_hf``/
            ``custom_file`` entry gets its first custom flavor seeded from
            its "Add local LLM" form at creation time; ``custom_server_url``
            never gets one at all (not a process kodo launches).
        path: Absolute path to the GGUF file on disk (``custom_file`` only).
        url: Base URL of the externally-managed server (``custom_server_url``
            only), e.g. ``'http://192.168.1.50:8042'``.
        base_llm: Slug identifying the original (unquantized) model this
            quant was created from, e.g. ``'qwen36-27b'``. ``hardcoded_hf``
            only — always ``""`` for every other kind.
        quant_author: Team or person who produced the quantized GGUF, e.g.
            ``'Unsloth'``. ``hardcoded_hf`` only — always ``""`` for every
            other kind.
        quant_type: The quantization spec, e.g. ``'Q8_0'`` or ``'UD-Q4_K_XL'``.
            ``hardcoded_hf`` only — always ``""`` for every other kind.
        size_hint: Human-readable GGUF file size as shown on the HuggingFace
            repo's file listing, e.g. ``'28.6 GB'``. ``hardcoded_hf`` only —
            always ``""`` for every other kind.
        gpu_tip: User-facing hardware recommendation string, e.g.
            ``'~43GB total at 128K context — no need to hunt for a giant
            workstation card. llama.cpp splits dense models layer-by-layer
            between GPU and CPU, so an 8GB GPU (e.g. RTX 4060) carries a
            solid share of the layers at full speed, with ~48GB of ordinary
            DDR5 system RAM covering the rest.'``. Estimated from
            ``size_hint`` plus the model's approximate KV-cache footprint at
            128K context, then framed as a modest discrete GPU (8-16GB VRAM,
            what most people actually own) plus enough system RAM to make up
            the difference — never as "buy a bigger GPU" — since llama.cpp's
            per-layer (dense) and MoE-expert (sparse) offloading make the
            split practical without a workstation-class card. Notes when a
            model is sparse-MoE (offloads especially well, near-full-GPU
            speed even with most weights in RAM) vs dense (still works, but
            every offloaded layer costs proportionally more speed).
            ``hardcoded_hf`` only — always ``""`` for every other kind.
        mac_tip: User-facing Apple Silicon recommendation string, e.g.
            ``'Needs ~43GB — comfortable on a 64GB MacBook Pro (M4 Pro/Max
            or M5 Pro/Max); a 48GB config is tight.'``. Same VRAM estimate
            as ``gpu_tip``, mapped onto MacBook Pro unified-memory tiers
            with headroom for macOS overhead. Unlike ``gpu_tip``, this stays
            framed as a single pool since Apple Silicon has no separate
            VRAM/RAM split to offload across. ``hardcoded_hf`` only —
            always ``""`` for every other kind.
        min_memory: Absolute minimum **combined** VRAM + system RAM assuming
            0 context (i.e. roughly ``size_hint``) — not VRAM alone, since
            llama.cpp can run a model split across both. If the host
            computer's detected VRAM plus RAM together don't reach this,
            the user is warned that this LLM will likely not run. If set to
            0, this value should be ignored. ``hardcoded_hf`` only — always
            ``0`` for every other kind.
        memory: Recommended **combined** VRAM + system RAM for comfortable
            operation up to 128K context — again VRAM+RAM together, not
            VRAM alone. If the host's detected VRAM plus RAM together fall
            short, the user is warned that performance may degrade sharply
            at large contexts. If set to 0, this value should be ignored.
            ``hardcoded_hf`` only — always ``0`` for every other kind.
    """

    name: str
    kind: str
    description: str = ""
    repo_id: str = ""
    filename: str = ""
    context_window: int = 0
    flavors: tuple[LlamaFlavor, ...] = field(default_factory=LlamaFlavor.default_flavours_field)
    path: str = ""
    url: str = ""
    base_llm: str = ""
    quant_author: str = ""
    quant_type: str = ""
    size_hint: str = ""
    gpu_tip: str = ""
    mac_tip: str = ""
    min_memory: int = 0
    memory: int = 0


# Compiled-in GGUFs — ported from the old flat registry, dropping `residence`.
_HARDCODED_LOCAL_MODELS: tuple[LocalLLMEntry, ...] = (
    LocalLLMEntry(
        name="atomicchat-qwen36-27b-q8",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B Q8_0 by AtomicChat",
        repo_id="AlexAtomic/qwen36-27b-GGUF",
        filename="qwen36-27b-Q8_0.gguf",
        context_window=262_144,
        base_llm="Qwen36-27B",
        quant_author="AtomicChat",
        quant_type="Q8_0",
        size_hint="28.6 GB",
        gpu_tip="~43GB total at 128K context — no need to hunt for a giant workstation card. "
        "llama.cpp splits dense models layer-by-layer between GPU and CPU, so an 8GB GPU "
        "(e.g. RTX 4060) carries a solid share of the layers at full speed, with ~48GB of "
        "ordinary DDR5 system RAM covering the rest.",
        mac_tip="Needs ~43GB — comfortable on a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max); "
        "a 48GB config is tight.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-27b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q8_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-27B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="35.8 GB",
        gpu_tip="~50GB total at 128K context — the biggest of the Qwen 3.6 27B builds, but "
        "still no reason to chase a 64GB+ card. An 8GB GPU (e.g. RTX 3060 Ti) plus ~64GB of "
        "everyday DDR5 system RAM covers it via llama.cpp's layer offloading.",
        mac_tip="Needs ~50GB — a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) is tight; a 128GB "
        "M4 Max or M5 Max gives more headroom.",
        min_memory=48,
        memory=64,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-27b-q6-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q6_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q6_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-27B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K_XL",
        size_hint="26.0 GB",
        gpu_tip="~40GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus ~48GB of DDR5 "
        "system RAM covers the whole model — llama.cpp keeps as many layers on the GPU as fit "
        "and runs the rest from RAM.",
        mac_tip="Needs ~40GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
        "a 48GB config is tight.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-27b-q5-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q5_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q5_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-27B",
        quant_author="Unsloth",
        quant_type="UD-Q5_K_XL",
        size_hint="20.4 GB",
        gpu_tip="~35GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 32GB DDR5 kit is "
        "enough, with llama.cpp's layer offloading filling in the gap.",
        mac_tip="Needs ~35GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-27b-q4-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 27B UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-27B-MTP-GGUF",
        filename="Qwen3.6-27B-UD-Q4_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-27B",
        quant_author="Unsloth",
        quant_type="UD-Q4_K_XL",
        size_hint="17.9 GB",
        gpu_tip="~32GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 32GB DDR5 kit "
        "covers it comfortably — well within reach of a typical gaming rig once llama.cpp "
        "splits the layers.",
        mac_tip="Needs ~32GB — fits a 32GB MacBook Pro (M4 or M5) if you trim context a bit, "
        "or a 48GB config (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-35b-a3b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-35B-A3B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="39.1 GB",
        gpu_tip="~46GB total at 128K context, but it's a sparse MoE model — most of those "
        "weights sit idle on any given token. An 8GB GPU (e.g. RTX 3060 Ti) keeps the always-on "
        "attention/shared layers at full speed while llama.cpp offloads the inactive experts "
        "to ~48GB of DDR5 system RAM, staying close to full-GPU speed.",
        mac_tip="Needs ~46GB — a 48GB MacBook Pro is close to its limit; a 64GB config "
        "(M4 Pro/Max or M5 Pro/Max) is safer.",
        min_memory=48,
        memory=64,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-35b-a3b-q6-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q6_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-35B-A3B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K_XL",
        size_hint="32.6 GB",
        gpu_tip="~39GB total at 128K context. Same MoE-offload trick as the Q8_K_XL build: an 8GB "
        "GPU (e.g. RTX 5060) handles the shared layers at full speed, and ~48GB of DDR5 system "
        "RAM comfortably holds the offloaded experts.",
        mac_tip="Needs ~39GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=48,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-35b-a3b-q5-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q5_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-35B-A3B",
        quant_author="Unsloth",
        quant_type="UD-Q5_K_XL",
        size_hint="27.2 GB",
        gpu_tip="~34GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus ~32GB of DDR5 system "
        "RAM is enough — llama.cpp's MoE offloading keeps this close to full-GPU speed.",
        mac_tip="Needs ~34GB — fits a 48GB MacBook Pro comfortably; a 36GB M4 Max or M5 Max is "
        "tight.",
        min_memory=36,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen36-35b-a3b-q4-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.6 35B-A3B UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.6-35B-A3B-MTP-GGUF",
        filename="Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen36-35B-A3B",
        quant_author="Unsloth",
        quant_type="UD-Q4_K_XL",
        size_hint="22.9 GB",
        gpu_tip="~30GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 32GB DDR5 kit "
        "covers it — the sparse MoE architecture means llama.cpp's expert offloading barely "
        "costs you any speed.",
        mac_tip="Needs ~30GB — tight on a 32GB MacBook Pro (M4 or M5); a 36GB M4 Max/M5 Max is "
        "the safe choice.",
        min_memory=32,
        memory=36,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="UD-Q8_K_XL/Qwen3-Coder-Next-UD-Q8_K_XL-00001-of-00003.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="86.3 GB",
        gpu_tip="~90GB total at 128K context — the biggest of the Qwen 3 Coder Next 80B builds. "
        "A 16GB GPU (e.g. RTX 4080) handles the shared layers at full speed while llama.cpp's "
        "MoE expert offloading pushes the rest onto a 96GB DDR5 kit.",
        mac_tip="Needs ~90GB — comfortable on a 128GB MacBook Pro (M4 Max or M5 Max).",
        min_memory=128,
        memory=128,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q8-0",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q8_0 by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Q8_0/Qwen3-Coder-Next-UD-Q8_0-00001-of-00003.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q8_0",
        size_hint="84.8 GB",
        gpu_tip="~89GB total at 128K context. A 16GB GPU (e.g. RTX 5080) keeps the shared "
        "layers fast, with llama.cpp offloading the rest of the experts to a 96GB DDR5 kit.",
        mac_tip="Needs ~89GB — comfortable on a 128GB MacBook Pro (M4 Max or M5 Max).",
        min_memory=128,
        memory=128,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q6-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q6_K_XL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="UD-Q6_K_XL/Qwen3-Coder-Next-UD-Q6_K_XL-00001-of-00003.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K_XL",
        size_hint="73.1 GB",
        gpu_tip="~77GB total at 128K context. A 16GB GPU (e.g. RTX 4070 Ti Super) handles the "
        "shared layers, and llama.cpp's MoE offloading covers the rest with a 96GB DDR5 kit.",
        mac_tip="Needs ~77GB — exceeds a 64GB MacBook Pro; a 128GB M4 Max or M5 Max is required.",
        min_memory=128,
        memory=128,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q6-k",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q6_K by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="UD-Q6_K/Qwen3-Coder-Next-UD-Q6_K-00001-of-00003.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K",
        size_hint="65.8 GB",
        gpu_tip="~70GB total at 128K context. A 16GB GPU (e.g. RX 7900 GRE) handles the shared "
        "layers, and llama.cpp's MoE offloading covers the rest with a 96GB DDR5 kit.",
        mac_tip="Needs ~70GB — exceeds a 64GB MacBook Pro; a 128GB M4 Max or M5 Max is required.",
        min_memory=128,
        memory=128,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q5-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q5_K_XL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="UD-Q5_K_XL/Qwen3-Coder-Next-UD-Q5_K_XL-00001-of-00003.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q5_K_XL",
        size_hint="59.5 GB",
        gpu_tip="~64GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) handles the shared "
        "layers, and llama.cpp's MoE offloading covers the rest with a 64GB DDR5 kit.",
        mac_tip="Needs ~64GB — right at the edge of a 64GB MacBook Pro; a 128GB M4 Max or M5 "
        "Max gives more headroom.",
        min_memory=64,
        memory=128,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q5-k-s",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q5_K_S by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="UD-Q5_K_S/Qwen3-Coder-Next-UD-Q5_K_S-00001-of-00003.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q5_K_S",
        size_hint="55.8 GB",
        gpu_tip="~60GB total at 128K context. A 16GB GPU (e.g. RX 7800 XT) handles the shared "
        "layers, and llama.cpp's MoE offloading covers the rest with a 48GB DDR5 kit.",
        mac_tip="Needs ~60GB — tight on a 64GB MacBook Pro; a 128GB M4 Max or M5 Max gives more "
        "headroom.",
        min_memory=64,
        memory=64,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q4-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-Q4_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q4_K_XL",
        size_hint="49.6 GB",
        gpu_tip="~54GB total at 128K context. It's an 80B model, but the MoE architecture still "
        "plays nice with offloading: a 16GB GPU (e.g. RTX 4060 Ti 16GB) keeps the shared layers "
        "fast while ~48GB of DDR5 system RAM absorbs the rest of the experts.",
        mac_tip="Needs ~54GB — tight on a 64GB MacBook Pro; a 128GB M4 Max or M5 Max is the "
        "safe choice.",
        min_memory=64,
        memory=64,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-mxfp4-moe",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B MXFP4-MOE by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-MXFP4_MOE.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="MXFP4_MOE",
        size_hint="48.0 GB",
        gpu_tip="~52GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) handles the always-on "
        "layers, and ~48GB of DDR5 system RAM covers the offloaded MXFP4 experts — llama.cpp's "
        "MoE offloading keeps speed close to a full-VRAM fit.",
        mac_tip="Needs ~52GB — tight on a 64GB MacBook Pro; a 128GB M4 Max or M5 Max gives more "
        "headroom.",
        min_memory=64,
        memory=64,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-iq4-nl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-IQ4_NL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-IQ4_NL.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-IQ4_NL",
        size_hint="39.2 GB",
        gpu_tip="~43GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) keeps the "
        "shared layers fast, and llama.cpp's MoE expert offloading covers the rest with a "
        "32GB DDR5 kit.",
        mac_tip="Needs ~43GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
        "a 48GB config is tight.",
        min_memory=48,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-iq4-xs",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-IQ4_XS by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-IQ4_XS.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-IQ4_XS",
        size_hint="38.4 GB",
        gpu_tip="~42GB total at 128K context. A 16GB GPU (e.g. RTX 5070 Ti) keeps the shared "
        "layers fast, and llama.cpp's MoE expert offloading covers the rest with a 32GB "
        "DDR5 kit.",
        mac_tip="Needs ~42GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
        "a 48GB config is tight.",
        min_memory=48,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q3-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q3_K_XL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-Q3_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q3_K_XL",
        size_hint="36.3 GB",
        gpu_tip="~40GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus ~48GB of DDR5 "
        "system RAM is enough — llama.cpp's MoE expert offloading keeps this 80B model fast "
        "without a workstation card.",
        mac_tip="Needs ~40GB — fits a 64GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
        "a 48GB config is tight.",
        min_memory=48,
        memory=64,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-iq3-s",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-IQ3_S by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-IQ3_S.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-IQ3_S",
        size_hint="29.7 GB",
        gpu_tip="~34GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus ~32GB of DDR5 "
        "system RAM is enough — llama.cpp's MoE expert offloading keeps this 80B model fast "
        "without a workstation card.",
        mac_tip="Needs ~34GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-iq3-xxs",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-IQ3_XXS by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-IQ3_XXS.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-IQ3_XXS",
        size_hint="28.5 GB",
        gpu_tip="~33GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus ~32GB of DDR5 "
        "system RAM is enough, with llama.cpp's MoE offloading keeping this close to "
        "full-GPU speed.",
        mac_tip="Needs ~33GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-q2-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-Q2_K_XL by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-Q2_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-Q2_K_XL",
        size_hint="26.8 GB",
        gpu_tip="~31GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 32GB DDR5 kit "
        "is enough — llama.cpp's MoE expert offloading keeps this 80B model fast on modest "
        "hardware.",
        mac_tip="Needs ~31GB — fits a 32GB MacBook Pro (M4 or M5) if you trim context a bit, "
        "or a 48GB config (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=32,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-iq2-m",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-IQ2_M by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-IQ2_M.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-IQ2_M",
        size_hint="25 GB",
        gpu_tip="~29GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 32GB DDR5 kit "
        "covers it comfortably, with llama.cpp's MoE offloading doing the heavy lifting.",
        mac_tip="Needs ~29GB — fits a 32GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen3-coder-next-80b-iq2-xxs",
        kind="hardcoded_hf",
        description="Qwen 3 Coder 80B UD-IQ2_XXS by Unsloth",
        repo_id="unsloth/Qwen3-Coder-Next-GGUF",
        filename="Qwen3-Coder-Next-UD-IQ2_XXS.gguf",
        context_window=262_144,
        base_llm="Qwen3-Coder-Next-80B",
        quant_author="Unsloth",
        quant_type="UD-IQ2_XXS",
        size_hint="23.3 GB",
        gpu_tip="~27GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 32GB DDR5 "
        "kit is enough — the smallest of the Qwen 3 Coder Next 80B builds, and llama.cpp's "
        "MoE offloading keeps it fast even on modest hardware.",
        mac_tip="Needs ~27GB — fits a 32GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-qwen35-9b-q8-k-xl",
        kind="hardcoded_hf",
        description="Qwen 3.5 9B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/Qwen3.5-9B-MTP-GGUF",
        filename="Qwen3.5-9B-UD-Q8_K_XL.gguf",
        context_window=262_144,
        base_llm="Qwen35-9B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="13.2 GB",
        gpu_tip="~17GB total at 128K context. Any 8GB GPU (e.g. RTX 5060) plus a basic 16GB DDR5 "
        "kit is plenty — this one barely needs the offloading trick at all.",
        mac_tip="Needs ~17GB — fits a 24GB MacBook Pro (M4, M4 Pro, M5, or M5 Pro) comfortably.",
        min_memory=24,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gpt-oss-120b-f16",
        kind="hardcoded_hf",
        description="GPT OSS 120B F16 by Unsloth",
        repo_id="unsloth/gpt-oss-120b-GGUF",
        filename="gpt-oss-120b-F16.gguf",
        flavors=(LlamaFlavor.make_default_kv_fp16(),),
        context_window=131_072,
        base_llm="GPT-OSS-120B",
        quant_author="Unsloth",
        quant_type="F16",
        size_hint="65.4 GB",
        gpu_tip="~75GB total at 128K context — this is a big one, but it's GPT-OSS's sparse MoE "
        "architecture at its best. A 16GB GPU (e.g. RX 7800 XT) runs the shared layers at full "
        "speed while llama.cpp offloads the experts to ~96GB of DDR5 system RAM — no datacenter "
        "card required.",
        mac_tip="Needs ~75GB — a 128GB MacBook Pro (M4 Max or M5 Max) is required, and it's right "
        "at the edge even there.",
        min_memory=128,
        memory=128,
    ),
    LocalLLMEntry(
        name="unsloth-gpt-oss-20b-f16",
        kind="hardcoded_hf",
        description="GPT OSS 20B F16 by Unsloth",
        repo_id="unsloth/gpt-oss-20b-GGUF",
        filename="gpt-oss-20b-F16.gguf",
        flavors=(LlamaFlavor.make_default_kv_fp16(),),
        context_window=131_072,
        base_llm="GPT-OSS-20B",
        quant_author="Unsloth",
        quant_type="F16",
        size_hint="13.8 GB",
        gpu_tip="~20GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus ~24GB of DDR5 system "
        "RAM covers it comfortably — llama.cpp's MoE offloading keeps GPT-OSS-20B fast even on a "
        "modest card.",
        mac_tip="Needs ~20GB — fits a 32GB MacBook Pro (M4 or M5) comfortably; a 24GB config is"
        "tight.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-gpt-oss-20b-q8-k-xl",
        kind="hardcoded_hf",
        description="GPT OSS 20B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/gpt-oss-20b-GGUF",
        filename="gpt-oss-20b-UD-Q8_K_XL.gguf",
        context_window=131_072,
        base_llm="GPT-OSS-20B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="13.2 GB",
        gpu_tip="~16GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 16GB DDR5 kit is "
        "all this needs — MoE offloading makes the 8GB card feel roomier than the raw total "
        "suggests.",
        mac_tip="Needs ~16GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gpt-oss-20b-q8-0",
        kind="hardcoded_hf",
        description="GPT OSS 20B Q8_0 by Unsloth",
        repo_id="unsloth/gpt-oss-20b-GGUF",
        filename="gpt-oss-20b-Q8_0.gguf",
        context_window=131_072,
        base_llm="GPT-OSS-20B",
        quant_author="Unsloth",
        quant_type="Q8_0",
        size_hint="12.1 GB",
        gpu_tip="~15GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 16GB DDR5 kit "
        "covers it easily, with llama.cpp offloading the inactive experts to RAM.",
        mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gpt-oss-20b-q6-k-xl",
        kind="hardcoded_hf",
        description="GPT OSS 20B UD-Q6_K_XL by Unsloth",
        repo_id="unsloth/gpt-oss-20b-GGUF",
        filename="gpt-oss-20b-UD-Q6_K_XL.gguf",
        context_window=131_072,
        base_llm="GPT-OSS-20B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K_XL",
        size_hint="12.0 GB",
        gpu_tip="~15GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 16GB DDR5 kit is "
        "enough — the sparse MoE architecture keeps offloaded performance close to "
        "a full-VRAM fit.",
        mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gpt-oss-20b-q4-k-xl",
        kind="hardcoded_hf",
        description="GPT OSS 20B UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/gpt-oss-20b-GGUF",
        filename="gpt-oss-20b-UD-Q4_K_XL.gguf",
        context_window=131_072,
        base_llm="GPT-OSS-20B",
        quant_author="Unsloth",
        quant_type="UD-Q4_K_XL",
        size_hint="11.9 GB",
        gpu_tip="~15GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 16GB DDR5 kit "
        "covers it comfortably via llama.cpp's MoE expert offloading.",
        mac_tip="Needs ~15GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-q8-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q8_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="27.6 GB",
        gpu_tip="~31GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 32GB DDR5 kit "
        "covers it comfortably — Gemma 4's MoE design (the A4B in its filename) lets llama.cpp "
        "offload inactive experts to RAM without much of a speed hit.",
        mac_tip="Needs ~31GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
        "a 32GB config is tight.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-q6-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-Q6_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q6_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K_XL",
        size_hint="23.3 GB",
        gpu_tip="~26GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 32GB DDR5 "
        "kit is enough — Gemma 4's MoE offloading (the A4B design) keeps this close to "
        "full-GPU speed.",
        mac_tip="Needs ~26GB — fits a 32GB MacBook Pro (M4 or M5) if you trim context a bit, "
        "or a 48GB config comfortably.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-q5-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-Q5_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q5_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-Q5_K_XL",
        size_hint="21.2 GB",
        gpu_tip="~24GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 24GB DDR5 kit "
        "covers it — Gemma 4's MoE design offloads inactive experts to RAM without much of a "
        "speed hit.",
        mac_tip="Needs ~24GB — fits a 32GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-q4-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-Q4_K_XL",
        size_hint="17.0 GB",
        gpu_tip="~20GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus ~24GB of DDR5 "
        "system RAM is enough — Gemma 4's MoE design (the A4B in its filename) lets llama.cpp "
        "offload inactive experts to RAM without much of a speed hit.",
        mac_tip="Needs ~20GB — fits a 32GB MacBook Pro (M4 or M5) comfortably; a 24GB config "
        "is tight.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-qat-ud-q4-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B QAT UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-qat-GGUF",
        filename="gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="QAT-UD-Q4_K_XL",
        size_hint="14.2 GB",
        gpu_tip="~17GB total at 128K context. Any 8GB GPU (e.g. RTX 4060) plus a 24GB DDR5 "
        "kit is plenty — the QAT build barely needs the offloading trick at all.",
        mac_tip="Needs ~17GB — fits a 24GB MacBook Pro (M4, M4 Pro, M5, or M5 Pro) comfortably.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-q3-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-Q3_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q3_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-Q3_K_XL",
        size_hint="12.9 GB",
        gpu_tip="~16GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 16GB DDR5 kit "
        "covers it comfortably, with Gemma 4's MoE offloading filling in the gap.",
        mac_tip="Needs ~16GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-q2-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-Q2_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-Q2_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-Q2_K_XL",
        size_hint="10.5 GB",
        gpu_tip="~14GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 16GB DDR5 "
        "kit is enough for this small a build.",
        mac_tip="Needs ~14GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=16,
        memory=16,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-iq4-xs",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-IQ4_XS by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-IQ4_XS.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-IQ4_XS",
        size_hint="13.6 GB",
        gpu_tip="~17GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 24GB DDR5 kit "
        "covers it comfortably via Gemma 4's MoE expert offloading.",
        mac_tip="Needs ~17GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=24,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-iq3-xxs",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-IQ3_XXS by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-IQ3_XXS.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-IQ3_XXS",
        size_hint="11.4 GB",
        gpu_tip="~14GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 16GB DDR5 kit "
        "is enough, with llama.cpp's MoE offloading barely costing any speed.",
        mac_tip="Needs ~14GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=16,
        memory=16,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-26b-ud-iq2-xxs",
        kind="hardcoded_hf",
        description="Gemma 4 26B A4B UD-IQ2_XXS by Unsloth",
        repo_id="unsloth/gemma-4-26B-A4B-it-GGUF",
        filename="gemma-4-26B-A4B-it-UD-IQ2_XXS.gguf",
        context_window=262_144,
        base_llm="Gemma4-26B-A4B",
        quant_author="Unsloth",
        quant_type="UD-IQ2_XXS",
        size_hint="9.9 GB",
        gpu_tip="~13GB total at 128K context. Any 8GB GPU (e.g. RTX 5060) plus a 16GB DDR5 kit "
        "handles this, the smallest of the Gemma 4 26B A4B builds.",
        mac_tip="Needs ~13GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=16,
        memory=16,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-q8-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-Q8_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-Q8_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-Q8_K_XL",
        size_hint="35 GB",
        gpu_tip="~40GB total at 128K context. An 8GB GPU (e.g. RTX 4060 Ti) plus a 48GB DDR5 "
        "kit covers it — llama.cpp splits this dense model layer-by-layer between GPU and CPU.",
        mac_tip="Needs ~40GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably; "
        "a 64GB config gives more headroom.",
        min_memory=48,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-q8-0",
        kind="hardcoded_hf",
        description="Gemma 4 31B Q8_0 by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-Q8_0.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="Q8_0",
        size_hint="32.6 GB",
        gpu_tip="~38GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 48GB DDR5 "
        "kit is enough, with llama.cpp's layer offloading filling in the gap.",
        mac_tip="Needs ~38GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=48,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-q6-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-Q6_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-Q6_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-Q6_K_XL",
        size_hint="27.5 GB",
        gpu_tip="~33GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus ~32GB of DDR5 "
        "system RAM covers it — llama.cpp keeps as many layers on the GPU as fit and runs the "
        "rest from RAM.",
        mac_tip="Needs ~33GB — fits a 48GB MacBook Pro comfortably; a 36GB M4 Max or M5 Max is "
        "tight.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-q5-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-Q5_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-Q5_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-Q5_K_XL",
        size_hint="21.9 GB",
        gpu_tip="~27GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 32GB DDR5 kit "
        "is enough, with llama.cpp's layer offloading filling in the gap.",
        mac_tip="Needs ~27GB — fits a 32GB MacBook Pro comfortably, or a 48GB config with "
        "headroom to spare.",
        min_memory=32,
        memory=48,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-q4-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-Q4_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-Q4_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-Q4_K_XL",
        size_hint="18.8 GB",
        gpu_tip="~24GB total at 128K context. An 8GB GPU (e.g. RTX 4060) plus a 24GB DDR5 kit "
        "covers it comfortably — a typical gaming rig once llama.cpp splits the layers.",
        mac_tip="Needs ~24GB — fits a 32GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-q3-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-Q3_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-Q3_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-Q3_K_XL",
        size_hint="15.4 GB",
        gpu_tip="~20GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus a 24GB DDR5 "
        "kit covers it, with llama.cpp's layer offloading handling the rest.",
        mac_tip="Needs ~20GB — fits a 32GB MacBook Pro (M4 or M5) comfortably; a 24GB config "
        "is tight.",
        min_memory=24,
        memory=32,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-q2-k-xl",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-Q2_K_XL by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-Q2_K_XL.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-Q2_K_XL",
        size_hint="11.8 GB",
        gpu_tip="~17GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus a 16GB DDR5 kit "
        "covers it comfortably.",
        mac_tip="Needs ~17GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=16,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-iq3-xxs",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-IQ3_XXs by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-IQ3_XXS.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-IQ3_XXS",
        size_hint="11.8 GB",
        gpu_tip="~17GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus a 16GB DDR5 kit "
        "covers it comfortably.",
        mac_tip="Needs ~17GB — fits a 24GB MacBook Pro comfortably; a 16GB M5 is tight.",
        min_memory=16,
        memory=16,
    ),
    LocalLLMEntry(
        name="unsloth-gemma4-31b-ud-iq2-xxs",
        kind="hardcoded_hf",
        description="Gemma 4 31B UD-IQ2_XXS by Unsloth",
        repo_id="unsloth/gemma-4-31B-it-GGUF",
        filename="gemma-4-31B-it-UD-IQ2_XXS.gguf",
        context_window=262_144,
        base_llm="Gemma4-31B",
        quant_author="Unsloth",
        quant_type="UD-IQ2_XXS",
        size_hint="8.5 GB",
        gpu_tip="~14GB total at 128K context. Any 8GB GPU (e.g. RTX 4060) plus a 16GB DDR5 "
        "kit handles this, the smallest of the Gemma 4 31B builds.",
        mac_tip="Needs ~14GB — fits a 16GB MacBook Pro (M4 or M5) comfortably.",
        min_memory=16,
        memory=16,
    ),
    LocalLLMEntry(
        name="deepreinforce-ornith10-35b-bf16",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B BF16 by DeepReinforce",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-bf16.gguf",
        context_window=262_144,
        base_llm="Ornith10-35B",
        quant_author="DeepReinforce",
        quant_type="BF16",
        size_hint="69.4 GB",
        gpu_tip="~103GB total at 128K context — the BF16 build is the heaviest way to run "
        "Ornith 1.0, and since it's dense (not MoE), every offloaded layer costs real speed. "
        "A 16GB GPU (e.g. RTX 4080) plus a 128GB DDR5 kit will run it, but if raw speed matters "
        "more than bit-perfect precision, the quantized builds below hit similar quality at "
        "a fraction of the memory.",
        mac_tip="Needs ~103GB — tight on a 128GB M4 Max or M5 Max.",
        min_memory=128,
        memory=128,
    ),
    LocalLLMEntry(
        name="deepreinforce-ornith10-35b-q8-0",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q8_0 by DeepReinforce",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q8_0.gguf",
        context_window=262_144,
        base_llm="Ornith10-35B",
        quant_author="DeepReinforce",
        quant_type="Q8_0",
        size_hint="36.9 GB",
        gpu_tip="~54GB total at 128K context. A 16GB GPU (e.g. RTX 4060 Ti 16GB) plus ~48GB of "
        "DDR5 system RAM covers it via llama.cpp's layer offloading — no need for the BF16 "
        "build's 128GB ask.",
        mac_tip="Needs ~54GB — tight on a 64GB MacBook Pro; a 128GB M4 Max or M5 Max is the "
        "safe choice.",
        min_memory=64,
        memory=128,
    ),
    LocalLLMEntry(
        name="deepreinforce-ornith10-35b-q6-k",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q6_K by DeepReinforce",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q6_K.gguf",
        context_window=262_144,
        base_llm="Ornith10-35B",
        quant_author="DeepReinforce",
        quant_type="Q6_K",
        size_hint="28.5 GB",
        gpu_tip="~45GB total at 128K context. An 8GB GPU (e.g. RTX 3060 Ti) plus ~48GB of DDR5 "
        "system RAM handles it comfortably, with llama.cpp keeping as many layers on the GPU as "
        "VRAM allows.",
        mac_tip="Needs ~45GB — a 48GB MacBook Pro is close to its limit; a 64GB config "
        "(M4 Pro/Max or M5 Pro/Max) is safer.",
        min_memory=48,
        memory=64,
    ),
    LocalLLMEntry(
        name="deepreinforce-ornith10-35b-q5-k-m",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q5_K by DeepReinforce",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q5_K_m.gguf",
        context_window=262_144,
        base_llm="Ornith10-35B",
        quant_author="DeepReinforce",
        quant_type="Q5_K_M",
        size_hint="24.7 GB",
        gpu_tip="~42GB total at 128K context. An 8GB GPU (e.g. RTX 5060) plus ~48GB of DDR5 "
        "system RAM is enough — llama.cpp's layer offloading fills in the gap without needing "
        "a big card.",
        mac_tip="Needs ~42GB — fits a 64GB MacBook Pro comfortably; a 48GB config is tight.",
        min_memory=48,
        memory=64,
    ),
    LocalLLMEntry(
        name="deepreinforce-ornith10-35b-q4-k-m",
        kind="hardcoded_hf",
        description="Ornith 1.0 35B Q4_K by DeepReinforce",
        repo_id="deepreinforce-ai/Ornith-1.0-35B-GGUF",
        filename="ornith-1.0-35b-Q4_K_m.gguf",
        context_window=262_144,
        base_llm="Ornith10-35B",
        quant_author="DeepReinforce",
        quant_type="Q4_K_M",
        size_hint="21.2 GB",
        gpu_tip="~38GB total at 128K context. An 8GB GPU (e.g. RX 7600) plus ~48GB of DDR5 "
        "system RAM covers the whole model via llama.cpp's layer offloading.",
        mac_tip="Needs ~38GB — fits a 48GB MacBook Pro (M4 Pro/Max or M5 Pro/Max) comfortably.",
        min_memory=48,
        memory=48,
    ),
)


# ---------------------------------------------------------------------------
# External file I/O
# ---------------------------------------------------------------------------


def _registry_file(kodo_dir: Path) -> Path:
    return kodo_dir.joinpath(*_REGISTRY_RELATIVE_PATH)


def parse_llama_args(raw: object) -> dict[str, str]:
    """Coerce a WS-payload/JSON value into the ``llama_args`` shape.

    Anything that isn't a ``dict`` (missing field, wrong type from a
    malformed request) is treated as "no extra args" rather than raising —
    callers are parsing untrusted request payloads.
    """
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def parse_llama_args_text(raw: object) -> dict[str, str]:
    """Parse the "manage flavors" modal's raw multi-line text box into ``llama_args``.

    One flag per line, e.g. ``--ctx-size 1048576``. Each non-blank line is
    split on the first run of whitespace into ``(flag, value)``; a line with
    no value (a bare flag) gets an empty-string value, which
    :class:`~kodo.llms.llamacpp.LlamaServerConfig`'s command builder then
    emits without a following empty argument. Lines that don't start with
    ``-`` are silently skipped rather than rejected outright — this is a
    convenience parser for pasted llama.cpp command lines, not a strict
    format; the kodo-vsix modal does its own live validation before sending.

    Args:
        raw: The WS payload value, expected to be a ``str`` (anything else —
            missing field, wrong type — is treated as empty text).

    Returns:
        dict[str, str]: The parsed ``{flag: value}`` mapping.
    """
    if not isinstance(raw, str):
        return {}
    args: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("-"):
            continue
        parts = line.split(None, 1)
        args[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return args


def _entry_from_json(raw: dict[str, object]) -> LocalLLMEntry | None:
    name = str(raw.get("name", "")).strip()
    kind = str(raw.get("kind", "")).strip()
    if not name or kind not in _CUSTOM_KINDS:
        _log.warning("Skipping invalid local-llm-registry.json entry: %r", raw)
        return None
    return LocalLLMEntry(
        name=name,
        kind=kind,
        description=str(raw.get("description", "")),
        repo_id=str(raw.get("repo_id", "")),
        filename=str(raw.get("filename", "")),
        context_window=int(cast(int, raw.get("context_window", 0)) or 0),
        flavors=(),
        path=str(raw.get("path", "")),
        url=str(raw.get("url", "")),
    )


def _entry_to_json(entry: LocalLLMEntry) -> dict[str, object]:
    return {
        "name": entry.name,
        "kind": entry.kind,
        "description": entry.description,
        "repo_id": entry.repo_id,
        "filename": entry.filename,
        "context_window": entry.context_window,
        "path": entry.path,
        "url": entry.url,
    }


def _load_raw(kodo_dir: Path) -> dict[str, object]:
    """The whole ``local-llm-registry.json`` as a plain dict, ``{}`` if absent/unreadable.

    Shared low-level accessor for every top-level key in the file (``entries``,
    ``llama_server_override_path``, ``flavors``, ``active_flavors``) — callers
    that only care about one key still go through this so a round trip never
    clobbers keys it doesn't know about (see :func:`_save_external`,
    :func:`add_flavor`, etc., all of which load-modify-save the same dict).
    """
    path = _registry_file(kodo_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("Could not load %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_raw(kodo_dir: Path, data: dict[str, object]) -> None:
    path = _registry_file(kodo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_external(kodo_dir: Path) -> tuple[list[LocalLLMEntry], str | None]:
    data = _load_raw(kodo_dir)
    raw_entries = data.get("entries", [])
    entries: list[LocalLLMEntry] = []
    if isinstance(raw_entries, list):
        for raw in raw_entries:
            if isinstance(raw, dict):
                entry = _entry_from_json(raw)
                if entry is not None:
                    entries.append(entry)
    override_raw = data.get("llama_server_override_path")
    override = str(override_raw) if isinstance(override_raw, str) and override_raw else None
    return entries, override


def _save_external(kodo_dir: Path, entries: list[LocalLLMEntry], override_path: str | None) -> None:
    data = _load_raw(kodo_dir)
    data["entries"] = [_entry_to_json(e) for e in entries]
    data["llama_server_override_path"] = override_path
    _save_raw(kodo_dir, data)


# ---------------------------------------------------------------------------
# Flavors: custom (user-added) definitions + active-flavor selection.
#
# Stored as two sibling top-level keys in the same local-llm-registry.json,
# both keyed by *entry name* (any kind except custom_server_url — a flavor is
# meaningless for a server kodo doesn't launch): ``flavors: {entry_name:
# [flavor...]}`` for custom flavor definitions, ``active_flavors: {entry_name:
# flavor_id}`` for which one (if any) is currently selected. Predefined
# flavors live in code instead, on the hardcoded entry's own ``flavors``
# tuple — see LlamaFlavor and get_flavors().
# ---------------------------------------------------------------------------


def _flavor_from_json(raw: dict[str, object]) -> LlamaFlavor | None:
    flavor_id = str(raw.get("id", "")).strip()
    name = str(raw.get("name", "")).strip()
    if not flavor_id or not name:
        return None
    return LlamaFlavor(
        id=flavor_id,
        name=name,
        description=str(raw.get("description", "")),
        llama_args=parse_llama_args(raw.get("llama_args", {})),
        min_ram=int(cast(int, raw.get("min_ram", 0)) or 0),
        min_vram=int(cast(int, raw.get("min_vram", 0)) or 0),
    )


def _flavor_to_json(flavor: LlamaFlavor) -> dict[str, object]:
    return {
        "id": flavor.id,
        "name": flavor.name,
        "description": flavor.description,
        "llama_args": flavor.llama_args,
        "min_ram": flavor.min_ram,
        "min_vram": flavor.min_vram,
    }


def _all_custom_flavors(data: dict[str, object]) -> dict[str, list[LlamaFlavor]]:
    raw = data.get("flavors")
    result: dict[str, list[LlamaFlavor]] = {}
    if not isinstance(raw, dict):
        return result
    for entry_name, raw_list in raw.items():
        if not isinstance(raw_list, list):
            continue
        flavors = [
            f
            for f in (_flavor_from_json(item) for item in raw_list if isinstance(item, dict))
            if f is not None
        ]
        if flavors:
            result[str(entry_name)] = flavors
    return result


def _write_custom_flavors(
    data: dict[str, object], all_flavors: dict[str, list[LlamaFlavor]]
) -> None:
    data["flavors"] = {
        entry_name: [_flavor_to_json(f) for f in flavors]
        for entry_name, flavors in all_flavors.items()
    }


def _all_active_flavors(data: dict[str, object]) -> dict[str, str]:
    raw = data.get("active_flavors")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v}


def _slugify_flavor_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "flavor"


def get_flavors(kodo_dir: Path, entry: LocalLLMEntry) -> tuple[LlamaFlavor, ...]:
    """Predefined + custom flavors available for *entry*, predefined slots first.

    A custom flavor whose ``id`` matches a predefined one is an **override**
    — its definition would be used in place of the predefined one (same list
    position), rather than being dropped. Nothing in the public API can
    create one any more: :func:`add_flavor` always auto-generates an id that
    can't collide with a predefined one, and :func:`update_flavor` rejects a
    predefined ``flavor_id`` outright (predefined flavors are strictly
    read-only). This merge is kept purely for resilience against a
    same-id override written to ``~/.kodo/etc/local-llm-registry.json`` by
    an older kodo version, before that restriction existed — new ones can't
    be created going forward. Custom flavors that don't collide with any
    predefined id are appended after, in the order they were added.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to look up flavors for.

    Returns:
        tuple[LlamaFlavor, ...]: Ordered, predefined slots first (each
        possibly override-replaced), then any additional custom flavors.
    """
    custom = _all_custom_flavors(_load_raw(kodo_dir)).get(entry.name, [])
    custom_by_id = {f.id: f for f in custom}
    predefined_ids = {f.id for f in entry.flavors}
    merged = tuple(custom_by_id.get(f.id, f) for f in entry.flavors)
    extra = tuple(f for f in custom if f.id not in predefined_ids)
    return merged + extra


def add_flavor(
    kodo_dir: Path,
    entry_name: str,
    name: str,
    *,
    description: str = "",
    llama_args: dict[str, str] | None = None,
    min_ram: int = 0,
    min_vram: int = 0,
) -> LlamaFlavor:
    """Add a brand-new custom flavor to *entry_name*, auto-assigning its ``id`` from *name*.

    Always creates a new flavor slot, never an override of an existing one —
    the "Add" side of the "manage flavors" modal. Use :func:`update_flavor`
    to change an *existing custom* flavor's definition in place (predefined
    flavors are read-only, see that function's docstring).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The registry entry (hardcoded or custom) to attach this
            flavor to.
        name: Display name; also the source for the auto-generated ``id``
            (slugified, de-duplicated against every flavor — predefined or
            custom — *entry_name* already has, e.g. ``my-flavor``,
            ``my-flavor-2``).
        description: Optional human-readable explanation.
        llama_args: CLI flags, same shape as ``LlamaFlavor.llama_args``.
        min_ram: See ``LlamaFlavor.min_ram``. Defaults to ``0`` (unknown/no
            requirement — the hardware-fit check stays inactive).
        min_vram: See ``LlamaFlavor.min_vram``. Same default as *min_ram*.

    Returns:
        LlamaFlavor: The created flavor, with its assigned ``id``.

    Raises:
        ValueError: If *entry_name* is unknown, is a ``custom_server_url``
            (flavors are meaningless for a server kodo doesn't launch),
            *name* is blank, or a flavor named *name* already exists for
            *entry_name*.
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if entry.kind == "custom_server_url":
        raise ValueError("custom_server_url entries do not support flavors")
    name = name.strip()
    if not name:
        raise ValueError("Flavor name is required")

    existing_flavors = get_flavors(kodo_dir, entry)
    if any(f.name == name for f in existing_flavors):
        raise ValueError(f"A flavor named {name!r} already exists for {entry_name!r}")

    existing_ids = {f.id for f in existing_flavors}
    base_id = _slugify_flavor_id(name)
    flavor_id = base_id
    suffix = 2
    while flavor_id in existing_ids:
        flavor_id = f"{base_id}-{suffix}"
        suffix += 1

    flavor = LlamaFlavor(
        id=flavor_id,
        name=name,
        description=description,
        llama_args=dict(llama_args or {}),
        min_ram=min_ram,
        min_vram=min_vram,
    )
    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    all_flavors.setdefault(entry_name, []).append(flavor)
    _write_custom_flavors(data, all_flavors)
    _save_raw(kodo_dir, data)
    return flavor


def update_flavor(
    kodo_dir: Path,
    entry_name: str,
    flavor_id: str,
    name: str,
    *,
    description: str = "",
    llama_args: dict[str, str] | None = None,
    min_ram: int = 0,
    min_vram: int = 0,
) -> LlamaFlavor:
    """Overwrite an existing *custom* flavor's definition in place, keeping its ``id``.

    Predefined flavors are strictly read-only: this rejects *flavor_id*
    outright if it names one of *entry_name*'s predefined flavors (checked
    against ``entry.flavors``, the hardcoded tuple — same check
    :func:`remove_flavor` uses), even if a stale custom override from before
    this restriction existed happens to sit under that id. Anyone who wants
    a predefined flavor's config with different values should use
    :func:`add_flavor` to create a new custom flavor (copying the
    predefined one's ``llama_args`` as a starting point) rather than
    mutating the original.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry the flavor belongs to.
        flavor_id: The existing *custom* flavor's id (from
            :func:`get_flavors`) — unlike :func:`add_flavor`, this is never
            re-derived from *name*.
        name: New display name.
        description: New description.
        llama_args: New CLI flags, same shape as ``LlamaFlavor.llama_args``.
        min_ram: See ``LlamaFlavor.min_ram``. Defaults to ``0``, same as
            :func:`add_flavor` — unlike the pre-read-only behavior, this no
            longer carries the original flavor's value forward automatically;
            the caller must resend it to keep it unchanged.
        min_vram: See ``LlamaFlavor.min_vram``. Same default as *min_ram*.

    Returns:
        LlamaFlavor: The updated flavor.

    Raises:
        ValueError: If *entry_name* is unknown, is a ``custom_server_url``,
            *flavor_id* names a predefined flavor, *name* is blank,
            *flavor_id* isn't one of *entry_name*'s current flavors, or
            another flavor of *entry_name* already has *name*.
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if entry.kind == "custom_server_url":
        raise ValueError("custom_server_url entries do not support flavors")
    if any(f.id == flavor_id for f in entry.flavors):
        raise ValueError(f"{flavor_id!r} is a predefined flavor and cannot be edited")
    name = name.strip()
    if not name:
        raise ValueError("Flavor name is required")

    existing_flavors = get_flavors(kodo_dir, entry)
    original = next((f for f in existing_flavors if f.id == flavor_id), None)
    if original is None:
        raise ValueError(f"Unknown flavor {flavor_id!r} for {entry_name!r}")
    if any(f.name == name and f.id != flavor_id for f in existing_flavors):
        raise ValueError(f"A flavor named {name!r} already exists for {entry_name!r}")

    flavor = LlamaFlavor(
        id=flavor_id,
        name=name,
        description=description,
        llama_args=dict(llama_args or {}),
        min_ram=min_ram,
        min_vram=min_vram,
    )
    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    existing = all_flavors.get(entry_name, [])
    replaced = False
    new_list: list[LlamaFlavor] = []
    for f in existing:
        if f.id == flavor_id:
            new_list.append(flavor)
            replaced = True
        else:
            new_list.append(f)
    if not replaced:
        new_list.append(flavor)
    all_flavors[entry_name] = new_list
    _write_custom_flavors(data, all_flavors)
    _save_raw(kodo_dir, data)
    return flavor


def remove_flavor(kodo_dir: Path, entry_name: str, flavor_id: str) -> None:
    """Remove a custom flavor. Predefined flavors cannot be removed.

    A predefined flavor is rejected even if it currently has a custom
    *override* (see :func:`update_flavor`) — removing the override would
    silently revert it to the hardcoded definition, which is not "removing a
    flavor" from the user's perspective (the "Remove" button stays disabled
    for these ids in the UI for the same reason).

    If *flavor_id* was the active flavor for *entry_name*, the active
    selection resets to "" (Default — the entry's own launch config); the
    caller is responsible for restarting llama-server if *entry_name* is the
    currently running model (mirrors :func:`set_active_flavor`).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry the flavor belongs to.
        flavor_id: The flavor to remove.

    Raises:
        ValueError: If *flavor_id* is not a custom flavor of *entry_name*
            (includes the case where it's predefined, overridden or not).
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is not None and any(f.id == flavor_id for f in entry.flavors):
        raise ValueError(f"{flavor_id!r} is a predefined flavor and cannot be removed")

    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    current = all_flavors.get(entry_name, [])
    remaining = [f for f in current if f.id != flavor_id]
    if len(remaining) == len(current):
        raise ValueError(f"No custom flavor {flavor_id!r} for {entry_name!r}")
    if remaining:
        all_flavors[entry_name] = remaining
    else:
        all_flavors.pop(entry_name, None)
    _write_custom_flavors(data, all_flavors)

    active = _all_active_flavors(data)
    if active.get(entry_name) == flavor_id:
        active.pop(entry_name, None)
        data["active_flavors"] = active
    _save_raw(kodo_dir, data)


def get_active_flavor(kodo_dir: Path, entry_name: str) -> str:
    """The active flavor id for *entry_name*, or ``""`` for Default (the entry's own config)."""
    return _all_active_flavors(_load_raw(kodo_dir)).get(entry_name, "")


def set_active_flavor(kodo_dir: Path, entry_name: str, flavor_id: str) -> None:
    """Set (or clear) the active flavor for *entry_name*.

    Purely a persistence op — it does not touch a running llama-server.
    Callers that just changed the *currently active local model*'s flavor
    are responsible for restarting it (see ``local_llm.set_active_flavor``'s
    handler in ``kodo/server/_app.py``, doc/WS_PROTOCOL.md §7.6).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry_name: The entry to set the active flavor for.
        flavor_id: A flavor id from :func:`get_flavors`, or ``""`` for
            Default.

    Raises:
        ValueError: If *entry_name* is unknown, or *flavor_id* is non-empty
            and not one of *entry_name*'s flavors.
    """
    entry = get_local_registry(kodo_dir).get(entry_name)
    if entry is None:
        raise ValueError(f"Unknown local model: {entry_name!r}")
    if flavor_id and not any(f.id == flavor_id for f in get_flavors(kodo_dir, entry)):
        raise ValueError(f"Unknown flavor {flavor_id!r} for {entry_name!r}")

    data = _load_raw(kodo_dir)
    active = _all_active_flavors(data)
    if flavor_id:
        active[entry_name] = flavor_id
    else:
        active.pop(entry_name, None)
    data["active_flavors"] = active
    _save_raw(kodo_dir, data)


def resolve_context_window(entry: LocalLLMEntry, flavor: LlamaFlavor | None) -> int:
    """The effective context window (tokens) for *entry* launched with *flavor*.

    Deduced from *flavor*'s own ``-c``/``--ctx-size`` launch arg when it
    parses to a positive integer (``--ctx-size`` is checked first, since
    that's the flag every built-in flavor sets — see
    :meth:`LlamaFlavor.make_default_kv_q8`); otherwise (absent, ``0``, or
    unparseable — e.g. ``--ctx-size 0``'s "read the GGUF's own trained
    context length" sentinel) falls back to *entry*'s own
    ``context_window``. There is no separate ``context_window`` field on
    :class:`LlamaFlavor` any more — this function is the single place that
    turns launch args into a token-budgeting number (see
    :func:`kodo.llms.get_context_window`, which uses it via
    :func:`resolve_effective_llama_config`).

    Args:
        entry: The registry entry supplying the fallback value.
        flavor: The flavor about to be launched, or ``None`` (falls straight
            back to *entry*'s own ``context_window``).

    Returns:
        int: The effective context window in tokens.
    """
    if flavor is not None:
        raw = flavor.llama_args.get("--ctx-size", flavor.llama_args.get("-c"))
        if raw is not None:
            try:
                value = int(str(raw).strip())
            except ValueError:
                value = 0
            if value > 0:
                return value
    return entry.context_window


def get_effective_flavor_id(kodo_dir: Path, entry: LocalLLMEntry) -> str:
    """The flavor id that would actually be launched for *entry* right now.

    - The active flavor (:func:`get_active_flavor`), if set and still
      present among :func:`get_flavors`.
    - Otherwise (unset, or a stale id whose definition was removed since it
      was selected — "Default" in the UI) the first available flavor's id.
    - ``""`` if *entry* has no flavors at all.

    Callers that need to decide whether editing/removing a specific flavor
    id would change what's currently launched (e.g. whether to restart
    llama-server) compare against this, not the raw
    :func:`get_active_flavor` value — an *unset* active flavor still
    resolves to a real one (the first available), so a change to that one
    is effectively an active-flavor change too.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to resolve.

    Returns:
        str: A flavor id from :func:`get_flavors`, or ``""``.
    """
    flavors = get_flavors(kodo_dir, entry)
    flavor_id = get_active_flavor(kodo_dir, entry.name)
    if flavor_id and any(f.id == flavor_id for f in flavors):
        return flavor_id
    return flavors[0].id if flavors else ""


def resolve_effective_llama_config(
    kodo_dir: Path, entry: LocalLLMEntry
) -> tuple[dict[str, str], int]:
    """The ``(llama_args, context_window)`` actually launched for *entry*.

    Flavors are the only source of ``llama_args`` — *entry* itself carries
    none — so this always resolves to some flavor's args, selected via
    :func:`get_effective_flavor_id`. If *entry* has no flavors at all (only
    possible for a flavor-less ``custom_*`` entry whose sole flavor was
    since removed, or a ``custom_server_url`` entry, which is never actually
    launched this way), returns ``({}, entry.context_window)`` — no CLI args
    beyond the server-management ones in
    :class:`kodo.llms.llamacpp.LlamaServerConfig`.

    ``context_window`` is resolved via :func:`resolve_context_window` from
    the chosen flavor's own launch args, falling back to ``entry``'s.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry about to be launched.

    Returns:
        tuple[dict[str, str], int]: ``(llama_args, context_window)`` to use
        for this launch.
    """
    flavors = get_flavors(kodo_dir, entry)
    flavor_id = get_effective_flavor_id(kodo_dir, entry)
    flavor = next((f for f in flavors if f.id == flavor_id), None) if flavor_id else None
    if flavor is None:
        return {}, entry.context_window
    return dict(flavor.llama_args), resolve_context_window(entry, flavor)


# ---------------------------------------------------------------------------
# Public registry API
# ---------------------------------------------------------------------------


def get_local_registry(kodo_dir: Path) -> dict[str, LocalLLMEntry]:
    """Return the merged local registry: hardcoded entries + the user's custom ones.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.

    Returns:
        dict[str, LocalLLMEntry]: Map of entry name to :class:`LocalLLMEntry`.
    """
    merged: dict[str, LocalLLMEntry] = {e.name: e for e in _HARDCODED_LOCAL_MODELS}
    external, _ = _load_external(kodo_dir)
    for entry in external:
        if entry.name in merged:
            _log.warning("Custom local LLM %r shadows a hardcoded entry — skipping", entry.name)
            continue
        merged[entry.name] = entry
    return merged


def add_local_entry(kodo_dir: Path, entry: LocalLLMEntry) -> None:
    """Add a custom entry to the external collection.

    Forces ``entry.flavors`` to ``()`` regardless of what the caller passed
    in — a custom entry's dataclass field default would otherwise silently
    attach the built-in ``"default"`` :class:`LlamaFlavor` (meant for
    ``hardcoded_hf`` entries that don't override it), which would then shadow
    (and permanently hide) any *custom* flavor later added under the same
    ``"default"`` id — see :func:`get_flavors`'s predefined-wins collision
    rule. This is the single enforcement point for that invariant; every
    ``local_llm.add_*`` handler in ``kodo/server/_app.py`` relies on it
    rather than repeating ``flavors=()`` itself.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        entry: The entry to add; ``entry.kind`` must be one of the custom kinds.

    Raises:
        ValueError: If ``entry.kind`` is not a custom kind, or ``entry.name``
            already exists (hardcoded or custom).
    """
    if entry.kind not in _CUSTOM_KINDS:
        raise ValueError(f"Cannot add a local LLM entry of kind {entry.kind!r}")
    if entry.name in get_local_registry(kodo_dir):
        raise ValueError(f"A local LLM named {entry.name!r} already exists")
    if entry.flavors:
        entry = replace(entry, flavors=())
    external, override = _load_external(kodo_dir)
    external.append(entry)
    _save_external(kodo_dir, external, override)


def remove_local_entry(kodo_dir: Path, name: str) -> None:
    """Remove a custom entry from the external collection.

    Does not touch any downloaded GGUF file on disk — callers that want to
    free disk space should uninstall first via
    :func:`kodo.llms.llamacpp.get_local_model_manager`'s ``uninstall`` method
    before removing. Also drops any custom flavors and active-flavor
    selection stored for *name* — they would otherwise be permanently
    orphaned (nothing else ever cleans them up, and a future custom entry
    added under the same name would silently inherit them).

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        name: Entry name to remove.

    Raises:
        ValueError: If *name* is a hardcoded entry or does not exist.
    """
    if any(e.name == name for e in _HARDCODED_LOCAL_MODELS):
        raise ValueError(f"{name!r} is a built-in local LLM and cannot be removed")
    external, override = _load_external(kodo_dir)
    remaining = [e for e in external if e.name != name]
    if len(remaining) == len(external):
        raise ValueError(f"No custom local LLM named {name!r}")
    _save_external(kodo_dir, remaining, override)

    data = _load_raw(kodo_dir)
    all_flavors = _all_custom_flavors(data)
    active = _all_active_flavors(data)
    changed = False
    if all_flavors.pop(name, None) is not None:
        _write_custom_flavors(data, all_flavors)
        changed = True
    if active.pop(name, None) is not None:
        data["active_flavors"] = active
        changed = True
    if changed:
        _save_raw(kodo_dir, data)


def get_llama_server_override_path(kodo_dir: Path) -> str | None:
    """Return the global llama-server binary override path, or ``None``."""
    _, override = _load_external(kodo_dir)
    return override


def set_llama_server_override_path(kodo_dir: Path, path: str) -> None:
    """Set the global llama-server binary override path.

    Kept entirely separate from the model list — this replaces the
    *executable* kodo launches (keeping its own CLI-argument-generation logic
    intact), it is not itself a model.

    Args:
        kodo_dir: User-level ``~/.kodo`` directory.
        path: Absolute path to a llama-server-compatible executable/script.

    Raises:
        ValueError: If *path* does not exist.
    """
    if not Path(path).is_file():
        raise ValueError(f"No such file: {path}")
    external, _ = _load_external(kodo_dir)
    _save_external(kodo_dir, external, path)


def clear_llama_server_override_path(kodo_dir: Path) -> None:
    """Clear the global llama-server binary override, reverting to the bundled binary."""
    external, _ = _load_external(kodo_dir)
    _save_external(kodo_dir, external, None)
