"""Core data shapes: :class:`LocalLLMEntry` and :class:`LlmProfile`.

Every ``hardcoded_*`` catalog module (``_local_llm_*.py``) and every other
module in this package imports from here — this module depends only on
:mod:`._knobs` (for the knob types an entry declares).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._knobs import LlamaKnob
from ._knobs_shared import BASE_LLAMA_ARGS, SHARED_KNOBS

__all__ = [
    "LlmProfile",
    "LocalLLMEntry",
]


@dataclass(frozen=True)
class LlmProfile:
    """A named, **user-defined** set of ``llama-server`` launch arguments.

    One of the two kinds of launch configuration an entry can run under (see
    doc/LLM_REGISTRY.md §4.6):

    - The **Default profile**, which kodo builds from the entry's
      ``base_llama_args`` plus its knobs (:mod:`._knobs`). It is not an
      :class:`LlmProfile` at all — it has no stored args, only a knob
      selection — and it can neither be edited as raw text nor deleted.
    - Zero or more :class:`LlmProfile`\\s, which *are* raw arg sets: the user
      builds them in the "Manage profiles" editor by picking flags out of
      :data:`kodo.llms.LLAMA_ARG_CATALOG` (or typing them), and they have no
      knobs at all.

    Selecting a profile **fully replaces** whatever was active before — a
    profile is never merged with the Default profile's args or with another
    profile's (see
    :func:`~kodo.llms.local_registry._profiles.resolve_effective_llama_config`).
    A profile that wants the Default profile's KV-cache flags must repeat them.

    Every profile is custom by construction: there are no predefined
    profiles, because everything that used to be a predefined *flavor* is now
    a knob on the Default profile. That is what makes this dataclass so much
    smaller than the ``LlamaFlavor`` it replaces — no ``predefined`` flag, no
    ``platform``, no ``min_ram``/``min_vram`` (per-configuration hardware and
    platform gating was removed with the flavor model; the entry-level
    ``min_memory``/``memory`` warning is now the only hardware check).

    Attributes:
        id: Stable slug, unique among one entry's profiles. Auto-generated
            from ``name`` (see
            :func:`~kodo.llms.local_registry._profiles.add_profile`). Never
            ``""`` — the empty string is reserved on the wire and in
            ``active_profiles`` to mean "the Default profile".
        name: Human-readable display name, shown in the sidebar's profile
            picker.
        description: Optional human-readable explanation.
        llama_args: CLI flags passed verbatim to ``llama-server`` while this
            profile is active — the complete set, not extras layered onto
            some default. A bare/valueless flag is represented with an
            empty-string value. The effective context size is deduced from
            this dict's own ``-c``/``--ctx-size`` value (see
            :meth:`get_context_size`).
    """

    id: str
    name: str
    description: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)

    def get_context_size(self) -> int:
        """The context size (tokens) this profile's launch args declare.

        Scans :attr:`llama_args` for ``--ctx-size`` (checked first, since
        that's the flag kodo's own configurations set) or ``-c``, parsed as
        an integer. Does not fall back to the entry's own ``context_window``
        — that's
        :func:`~kodo.llms.local_registry._profiles.resolve_context_window`'s
        job, which calls this and falls back itself when it returns ``0``.

        Returns:
            int: The parsed context size, or ``0`` if neither key is present
            or the value doesn't parse to an integer (including the
            ``--ctx-size 0`` "use the GGUF's own trained context length"
            sentinel).
        """
        raw = self.llama_args.get("--ctx-size", self.llama_args.get("-c"))
        if raw is None:
            return 0
        try:
            return int(str(raw).strip())
        except ValueError:
            return 0


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
            :func:`kodo.llms.get_context_window`); the active configuration's
            own ``-c``/``--ctx-size`` launch arg (if positive) takes
            precedence, see
            :func:`~kodo.llms.local_registry._profiles.resolve_context_window`.
        base_llama_args: Launch args every configuration of this entry starts
            from, before knob args are layered on top (knob args win — see
            :mod:`._knobs`). Defaults to
            :data:`~kodo.llms.local_registry._knobs_shared.BASE_LLAMA_ARGS`,
            which is what nearly every ``hardcoded_hf`` entry wants; a
            ``custom_hf``/``custom_file`` entry stores whatever was typed into
            the "Add local LLM" form here, and
            :func:`~kodo.llms.local_registry._entries._with_custom_entry_knobs`
            merges it over those same shared base args on load. Not used at
            all by a *user-defined* profile, which carries its own complete
            arg set.
        knobs: The configurable controls this entry's Default profile offers
            (:class:`~kodo.llms.local_registry.LlamaKnob`), in display order.
            Defaults to
            :data:`~kodo.llms.local_registry._knobs_shared.SHARED_KNOBS`; a
            family that ships a private per-model knob (e.g. a YaRN context
            knob) declares ``knobs=SHARED_KNOBS + (ITS_KNOB,)`` instead.
            Validated at import time by
            :func:`~kodo.llms.local_registry._knobs.validate_knobs`: no two
            knobs on one entry may own the same CLI flag. Explicitly ``()``
            only for ``custom_server_url`` (not a process kodo launches, so it
            has no launch args to configure) and for a ``custom_*`` entry as
            persisted — knobs are code, re-attached on every load, never
            stored (see
            :func:`~kodo.llms.local_registry._entries.add_local_entry`).
        knob_defaults: Per-entry overrides of a knob's own default state,
            ``{knob_id: selection}``. This is how one entry starts from a
            different position than the shared knob's own default — e.g. an
            F16 GGUF declaring ``{"kv-cache": "f16"}``, which replaces the old
            ``make_default_kv_fp16`` predefined flavor. A key naming a knob
            this entry doesn't list is ignored.
        path: Absolute path to the GGUF file on disk (``custom_file`` only).
        url: Base URL of the externally-managed server (``custom_server_url``
            only), e.g. ``'http://192.168.1.50:8042'``.
        base_llm: Slug identifying the original (unquantized) model this
            quant was created from, e.g. ``'qwen36-27b'``. ``hardcoded_hf``
            only — always ``""`` for every other kind.
        llm_author: Company of Team who produced the original LLM, e.g.
            ``'OpenAI'``. ``hardcoded_hf`` only — always ``""`` for every
            other kind.
        license_name: Human-readable name of the original (unquantized)
            model's license, e.g. ``'Apache License 2.0'`` or
            ``'OpenMDW-1.1'``. Read off the base model's own HuggingFace
            page (its ``cardData.license``/``license_link``), not the GGUF
            quant repo — quant repos rarely restate it. When the base
            model's page doesn't host a working license link itself (a
            dead ``license_link``, or none at all), this falls back to the
            license's own canonical text (e.g. the Apache Software
            Foundation's copy) rather than a broken HuggingFace URL. All
            quants sharing one ``base_llm`` share the same license, since
            quantization doesn't change licensing terms. ``hardcoded_hf``
            only — always ``""`` for every other kind.
        license_url: Link to the license text described by ``license_name``.
            ``hardcoded_hf`` only — always ``""`` for every other kind.
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
            ``0`` for every other kind. Since per-configuration hardware
            gating was removed along with flavors, this and ``memory`` are
            the *only* hardware checks left.
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
    base_llama_args: dict[str, str] = field(default_factory=lambda: dict(BASE_LLAMA_ARGS))
    knobs: tuple[LlamaKnob, ...] = SHARED_KNOBS
    knob_defaults: dict[str, str] = field(default_factory=dict)
    path: str = ""
    url: str = ""
    base_llm: str = ""
    llm_author: str = ""
    license_name: str = ""
    license_url: str = ""
    quant_author: str = ""
    quant_type: str = ""
    size_hint: str = ""
    gpu_tip: str = ""
    mac_tip: str = ""
    min_memory: int = 0
    memory: int = 0
    llamacpp_version: int = 0
