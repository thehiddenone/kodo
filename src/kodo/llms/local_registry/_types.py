"""Core data shapes: :class:`LocalLLMEntry`, :class:`LlamaFlavor`, host-platform matching.

Every ``hardcoded_*`` catalog module (``_local_llm_*.py``) and every other
module in this package imports from here — this module itself has no
in-package dependencies.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "LlamaFlavor",
    "LlamaFlavorPlatform",
    "LocalLLMEntry",
    "current_host_platform",
]


class LlamaFlavorPlatform(StrEnum):
    """Which host platform(s) a :class:`LlamaFlavor` may be launched on.

    A single-pool ("mac") vs. dual-pool ("gpu") launch config are often not
    interchangeable — e.g. a huge YaRN-extended context flavor may only be
    practical on Apple Silicon's unified memory, never on a discrete-GPU +
    system-RAM split. This is a str :class:`Enum` (not two independent
    booleans) so a flavor always declares exactly one of three states, and
    so it serializes to/from JSON as a plain string via
    :func:`~kodo.llms.local_registry._io._flavor_to_json`/
    :func:`~kodo.llms.local_registry._io._flavor_from_json`.

    Values:
        MAC: Only compatible with Apple Silicon (unified memory).
        GPU: Only compatible with a Windows/Linux discrete-GPU PC.
        BOTH: Compatible with either platform — the default for every
            built-in flavor unless a docstring says otherwise.
    """

    MAC = "mac"
    GPU = "gpu"
    BOTH = "both"


def current_host_platform() -> LlamaFlavorPlatform:
    """The :class:`LlamaFlavorPlatform` bucket this kodo process is running on.

    ``LlamaFlavorPlatform.MAC`` on macOS (Apple Silicon's unified-memory
    pool), otherwise ``LlamaFlavorPlatform.GPU`` (Windows/Linux, a
    discrete-GPU-plus-system-RAM PC) — mirrors the same ``sys.platform ==
    "darwin"`` check :func:`kodo.llms.detect_vram_gb`/:func:`detect_ram_gb`
    already use to distinguish the two hardware models. There is no
    finer-grained detection (e.g. "actually has an NVIDIA GPU") — a non-Mac
    host is always treated as the "gpu" bucket regardless of whether a
    discrete GPU is actually present, matching the existing convention.
    """
    return LlamaFlavorPlatform.MAC if sys.platform == "darwin" else LlamaFlavorPlatform.GPU


def _flavor_compatible_with_host(flavor: LlamaFlavor) -> bool:
    """Whether *flavor* may be launched on :func:`current_host_platform`."""
    return flavor.platform in (LlamaFlavorPlatform.BOTH, current_host_platform())


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
    :func:`~kodo.llms.local_registry._flavors.resolve_effective_llama_config`);
    a flavor that wants another flavor's KV-cache-type flags (or anything
    else) must repeat them itself.

    Attributes:
        id: Stable slug, unique among the flavors available for one entry
            (predefined + custom together). Auto-generated from ``name`` for
            custom flavors (see
            :func:`~kodo.llms.local_registry._flavors.add_flavor`);
            hardcoded ones set it explicitly as a literal.
        name: Human-readable display name shown in the flavor dropdown.
        platform: Which host platform(s) this flavor may be launched on —
            see :class:`LlamaFlavorPlatform`. Defaults to ``BOTH``. Used by
            :func:`~kodo.llms.local_registry._flavors.get_effective_flavor_id`
            to skip an incompatible flavor when auto-selecting a default (no
            active flavor set yet); has no effect on an *explicit*
            :func:`~kodo.llms.local_registry._flavors.set_active_flavor`
            choice, which is never overridden.
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
            :func:`~kodo.llms.local_registry._flavors.resolve_context_window`.
        min_ram: Minimum system RAM (GB) this flavor needs to run, or the
            minimum *unified memory* on Apple Silicon — kodo-vsix reads
            ``detected_vram_gb`` for the unified-memory figure there (see
            ``kodo/llms/_hardware.py``), so a Mac-oriented flavor should set
            ``min_ram`` and leave ``min_vram`` at ``0``. ``0`` means
            "unknown/no requirement — don't check". Editable via
            :func:`~kodo.llms.local_registry._flavors.add_flavor`/
            :func:`~kodo.llms.local_registry._flavors.update_flavor` for a
            *custom* flavor; a predefined flavor's value is fixed at its
            hardcoded literal, since ``update_flavor`` rejects predefined
            ``flavor_id``\\s outright (see its docstring) — the only way to
            get a different threshold on a predefined flavor's config is to
            copy it into a new custom flavor.
        min_vram: Minimum discrete GPU VRAM (GB) this flavor needs, for a
            Windows/Linux GPU setup (``0`` on Apple Silicon — see
            ``min_ram``). ``0`` means "unknown/no requirement — don't
            check". If both ``min_ram`` and ``min_vram`` are ``0`` the
            hardware-fit check is inactive and the flavor is treated as
            runnable everywhere. Editable the same way as ``min_ram``.
    There is no separate request-level sampling-defaults field any more —
    the kodo-vsix flavor editor's structured sampling form is a shortcut for
    editing ``llama_args`` itself (via each :class:`kodo.llms.SamplingParamSpec`'s
    ``cli_flags``), not a distinct piece of state, so a flavor's sampling
    knobs always take a llama-server restart to apply, exactly like every
    other launch arg (doc/SAMPLING.md §9). Only a *session's* per-quant
    overrides (``SessionState.sampling``) remain request-level and hot —
    see :class:`kodo.llms.SamplingParams`.
    """

    id: str
    name: str
    platform: LlamaFlavorPlatform = LlamaFlavorPlatform.BOTH
    description: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)
    min_ram: int = 0
    min_vram: int = 0

    def get_context_size(self) -> int:
        """The context size (tokens) this flavor's launch args declare.

        Scans :attr:`llama_args` for ``--ctx-size`` (checked first, since
        that's the flag every built-in flavor sets) or ``-c``, parsed as an
        integer. Does not fall back to the entry's own ``context_window`` —
        that's
        :func:`~kodo.llms.local_registry._flavors.resolve_context_window`'s
        job, which calls this method and falls back itself when it returns
        ``0``.

        Returns:
            int: The parsed context size, or ``0`` if neither key is
            present or the value doesn't parse to an integer (including the
            ``--ctx-size 0`` "use the GGUF's own trained context length"
            sentinel every built-in flavor sets by default).
        """
        raw = self.llama_args.get("--ctx-size", self.llama_args.get("-c"))
        if raw is None:
            return 0
        try:
            return int(str(raw).strip())
        except ValueError:
            return 0

    @staticmethod
    def make_default_kv_q8() -> LlamaFlavor:
        return LlamaFlavor(
            id="default",
            name="default",
            platform=LlamaFlavorPlatform.BOTH,
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
            platform=LlamaFlavorPlatform.BOTH,
            description="Default flavor",
            llama_args={
                "--cache-type-k": "f16",
                "--cache-type-v": "f16",
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
            over this one, see
            :func:`~kodo.llms.local_registry._flavors.resolve_context_window`.
        flavors: Predefined alternate launch configurations shipped with this
            entry (see :class:`LlamaFlavor`) — ``hardcoded_hf`` only.
            Entries without an explicit ``flavors=`` literal get exactly one,
            via this dataclass field's default factory
            (:meth:`LlamaFlavor.default_flavours_field`); the few that need a
            different built-in default (e.g. the F16 GGUFs' KV cache) set
            ``flavors=`` explicitly instead. Always ``()`` for every
            ``custom_*`` kind —
            :func:`~kodo.llms.local_registry._entries.add_local_entry` forces
            this regardless of what's passed in (a caller-supplied non-empty
            value would otherwise silently shadow a same-id custom flavor
            added later), and ``_entry_from_json`` passes ``flavors=()``
            explicitly too, since loading from disk doesn't go through
            ``add_local_entry``. A custom entry's launch args live entirely
            in the *custom* flavor store instead (see
            :func:`~kodo.llms.local_registry._flavors.get_flavors`/
            :func:`~kodo.llms.local_registry._flavors.add_flavor`) — a
            ``custom_hf``/``custom_file`` entry gets its first custom flavor
            seeded from its "Add local LLM" form at creation time;
            ``custom_server_url`` never gets one at all (not a process kodo
            launches).
        path: Absolute path to the GGUF file on disk (``custom_file`` only).
        url: Base URL of the externally-managed server (``custom_server_url``
            only), e.g. ``'http://192.168.1.50:8042'``.
        base_llm: Slug identifying the original (unquantized) model this
            quant was created from, e.g. ``'qwen36-27b'``. ``hardcoded_hf``
            only — always ``""`` for every other kind.
        llm_author: Company of Team who produced the original LLM, e.g.
            ``'OpenAI'``. ``hardcoded_hf`` only — always ``""`` for every
            other kind.
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
        llamacpp_version: Llama.cpp version required for this LLM to run.
            All versions that are less than this one will likely to fail
            with this LLM. ``0`` means any version will work.
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
    llm_author: str = ""
    quant_author: str = ""
    quant_type: str = ""
    size_hint: str = ""
    gpu_tip: str = ""
    mac_tip: str = ""
    min_memory: int = 0
    memory: int = 0
    llamacpp_version: int = 0
