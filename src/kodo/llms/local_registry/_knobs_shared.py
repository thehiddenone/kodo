"""The shared knobs — offered on the Default profile of *every* llama-server entry.

Shared knobs are the ones whose meaning does not depend on which GGUF is
loaded: KV-cache precision, the three sampling axes, and the offload
controls. Anything that needs per-model knowledge (a YaRN recipe needs the
model's architecture key and native context length) is a *private* knob built
by the family module instead — see :mod:`._knobs_context`.

Every hardcoded entry lists :data:`SHARED_KNOBS` plus whatever private knobs
it has; a user-added (``custom_*``) entry gets :data:`SHARED_KNOBS` alone, on
top of the launch args typed into its "Add local LLM" form (which become its
``base_llama_args``).

Three sampling axes, never conflated
------------------------------------

:data:`TAIL_CULLING_KNOB`, :data:`NUCLEUS_SAMPLING_KNOB` and
:data:`TEMPERATURE_KNOB` are deliberately separate controls, each fixed to
its own flags: tail culling only ever writes ``--min-p``/``--top-nsigma``,
nucleus sampling only ``--top-k``/``--top-p``, and temperature only
``--temp``. This is the same "one axis moves at a time" rule the five
predecessor sampling *flavors* followed by convention (doc/QUANT_SAMPLING.md
§4) — as knobs it is enforced structurally, since
:func:`~kodo.llms.local_registry._knobs.validate_knobs` rejects two knobs that
own the same flag. Splitting them also makes combinations reachable that the
old fixed presets never offered (strong tail culling *and* a low temperature,
or either of those layered on top of a fixed nucleus cutoff).

**No knob enables a repetition penalty** — not DRY, not ``--repeat-penalty``,
not presence/frequency. Penalising a token sequence already in context is
exactly what quoting back an attachment UUID, a file path or an identifier
requires, and it broke ``read_attachment`` in practice; loop handling belongs
to the watchdog instead (doc/QUANT_SAMPLING.md §3f, doc/STUCK_DETECTION.md
§2.7/§2.10). The rule binds what kodo *ships*, not what a user may do: a
repetition penalty is still reachable on a user-defined profile (it is in
:mod:`kodo.llms._arg_catalog`) and as a per-session override, because hiding
it in one of the two editors while offering it in the other would be an
arbitrary split. No knob offers one.
"""

from __future__ import annotations

from ._knobs import KnobKind, KnobOption, LlamaKnob

__all__ = [
    "CPU_MOE_KNOB",
    "GPU_LAYERS_KNOB",
    "KV_CACHE_F16_DEFAULT",
    "KV_CACHE_KNOB",
    "NUCLEUS_SAMPLING_KNOB",
    "SHARED_KNOBS",
    "TAIL_CULLING_KNOB",
    "TEMPERATURE_KNOB",
]

#: Flags every Default profile carries regardless of knob selection, and that
#: no knob may own. ``--ctx-size 0`` is the exception to "no knob may own a
#: base flag": a context knob (:mod:`._knobs_context`) overrides it, which is
#: the documented base-args-are-the-floor rule (see :mod:`._knobs`). An entry
#: adds to this via :attr:`~kodo.llms.local_registry.LocalLLMEntry.base_llama_args`.
BASE_LLAMA_ARGS: dict[str, str] = {
    "--ctx-size": "0",
    "--reasoning-format": "auto",
    "--jinja": "",
}

#: KV-cache precision. Replaces the old ``make_default_kv_q8`` /
#: ``make_default_kv_fp16`` pair of predefined flavors: an entry that used to
#: ship the fp16 variant now just declares
#: ``knob_defaults=KV_CACHE_F16_DEFAULT`` (below).
KV_CACHE_KNOB = LlamaKnob(
    id="kv-cache",
    name="KV cache precision",
    description=(
        "How the attention key/value cache is stored. Quantizing it roughly halves the memory "
        "the context window costs, which is what makes a large context fit at all on most "
        "machines; f16 keeps full precision at twice the footprint."
    ),
    kind=KnobKind.DROPDOWN,
    options=(
        KnobOption(
            id="q8_0",
            name="q8_0 (8-bit)",
            description=(
                "8-bit keys and values. About half the memory of f16 for a quality difference "
                "that is hard to measure on a model that is already quantized. The right default "
                "for essentially every build."
            ),
            llama_args={"--cache-type-k": "q8_0", "--cache-type-v": "q8_0"},
        ),
        KnobOption(
            id="q4_0",
            name="q4_0 (4-bit)",
            description=(
                "4-bit keys and values — a quarter of the memory of f16, half of q8_0. Noticeably "
                "more quality loss than q8_0, so reach for this only when context length is the "
                "binding constraint and q8_0 still doesn't leave enough room."
            ),
            llama_args={"--cache-type-k": "q4_0", "--cache-type-v": "q4_0"},
        ),
        KnobOption(
            id="f16",
            name="f16 (full precision)",
            description=(
                "Unquantized keys and values — twice the memory per token of context. Worth it "
                "only on an F16 GGUF, where the weights are unquantized too and the cache would "
                "otherwise be the least precise thing in the pipeline."
            ),
            llama_args={"--cache-type-k": "f16", "--cache-type-v": "f16"},
        ),
    ),
    default_option="q8_0",
)

#: Per-entry :attr:`~kodo.llms.local_registry.LocalLLMEntry.knob_defaults`
#: override for an F16/BF16 (unquantized-weight) GGUF, where :data:`KV_CACHE_KNOB`
#: should default to ``f16`` instead of its own global ``q8_0`` default — see
#: that knob's ``f16`` option above for why. Every hardcoded entry whose
#: ``quant_type`` is ``"F16"``/``"BF16"`` should set
#: ``knob_defaults=KV_CACHE_F16_DEFAULT``.
KV_CACHE_F16_DEFAULT: dict[str, str] = {"kv-cache": "f16"}

#: How hard the probability tail is truncated via min-p (plus top-n-sigma in
#: the strongest state). This knob only ever writes ``--min-p``/``--top-nsigma``
#: — it leaves ``--top-k``/``--top-p`` alone entirely, so it always stacks on
#: top of whatever :data:`NUCLEUS_SAMPLING_KNOB` (or, if that is off,
#: llama.cpp's own ``top-k 40``/``top-p 0.95`` defaults) is doing.
TAIL_CULLING_KNOB = LlamaKnob(
    id="tail-culling",
    name="Tail culling",
    description=(
        "How aggressively unlikely tokens are discarded before sampling. Quantization adds a "
        "roughly constant amount of noise to every logit, which shows up as a floor of "
        "plausible-looking junk in the tail — culling relative to the top token is the most "
        "direct way to remove it. Reach for this before reaching for a lower temperature: it "
        "removes bad candidates outright rather than merely making them less likely."
    ),
    kind=KnobKind.DROPDOWN,
    options=(
        KnobOption(
            id="off",
            name="llama.cpp defaults",
            description=(
                "No explicit min-p or top-n-sigma settings. Fine for an 8-bit or larger quant, "
                "where there is little tail noise to cut in the first place."
            ),
        ),
        KnobOption(
            id="minimal",
            name="Minimal (min-p 0.02)",
            description=(
                "The gentlest explicit cull: a token needs 2% of the top token's probability to "
                "survive. Pairs with a near-greedy temperature, where the tail is already "
                "suppressed and heavy culling would leave nothing to choose between."
            ),
            llama_args={"--min-p": "0.02"},
        ),
        KnobOption(
            id="light",
            name="Light (min-p 0.05)",
            description=(
                "A token needs 5% of the top token's probability to survive — enough to cut the "
                "noise floor a 4-bit-and-below quant leaves in the tail. Start here and tighten "
                "only if you actually see a problem."
            ),
            llama_args={"--min-p": "0.05"},
        ),
        KnobOption(
            id="light-medium",
            name="Light-medium (min-p 0.065)",
            description=(
                "Between Light and Medium: reach for this when Light's 5% floor still lets an "
                "occasional wrong token through but Medium's 8% feels like more cut than the "
                "quant needs."
            ),
            llama_args={"--min-p": "0.065"},
        ),
        KnobOption(
            id="medium",
            name="Medium (min-p 0.08)",
            description=(
                "A tighter noise floor, for a quant that produces the occasional "
                "wrong-but-plausible token."
            ),
            llama_args={"--min-p": "0.08"},
        ),
        KnobOption(
            id="medium-strong",
            name="Medium-strong (min-p 0.10)",
            description=(
                "The last stop before Strong's logit-space cutoff: still pure min-p, tightened "
                "to a 10% floor for a quant that keeps wandering under Medium but does not yet "
                "need top-n-sigma's extra machinery."
            ),
            llama_args={"--min-p": "0.10"},
        ),
        KnobOption(
            id="strong",
            name="Strong (min-p 0.12 + top-n-sigma)",
            description=(
                "The most aggressive truncation: min-p 0.12 plus top-n-sigma 1.0, which cuts in "
                "logit space — the units quantization error is actually in — and is "
                "temperature-invariant, so it does not re-tune itself when you change the "
                "temperature. For heavily quantized builds that still wander under Medium."
            ),
            llama_args={"--min-p": "0.12", "--top-nsigma": "1.0"},
        ),
    ),
    default_option="off",
)

#: Temperature only. The default option writes llama.cpp's own default value
#: explicitly rather than nothing, so that the flag list the Configure modal
#: shows is always the complete story of what the sampler will do.
#:
#: The option *names* are a single monotone ladder — "loose" above the default,
#: then focused → tight → strict → rigid → near-greedy below it, each family
#: optionally qualified with "Very" — with the value repeated in parentheses so
#: the ordering is never in doubt. The option *ids* are older than that ladder
#: and deliberately no longer echo it (``moderate`` is "Tight (0.5)",
#: ``low`` is "Strict (0.3)", ``very-low`` is "Rigid (0.15)"): an id is the
#: persisted wire value that appears in saved profiles, per-session overrides
#: and validator configs, so renaming one would silently invalidate a stored
#: selection. Rename a display name freely; never rename an id.
TEMPERATURE_KNOB = LlamaKnob(
    id="temperature",
    name="Temperature",
    description=(
        "How much randomness is applied to the token distribution. Temperature scales "
        "quantization error along with the signal, so lowering it attenuates the noise floor "
        "rather than truncating it — the right first move when *format* correctness is what is "
        "failing (JSON tool-call arguments, strict syntax, an identifier copied from context). "
        "Note that a very low temperature also makes a model more likely to get stuck in a loop, "
        "not less."
    ),
    kind=KnobKind.DROPDOWN,
    options=(
        KnobOption(
            id="very-high",
            name="Extremely loose (1.5)",
            description=(
                "The widest sampling on offer: maximum variety and the fewest verbatim repeats, "
                "at the cost of noticeably less reliable formatting. For creative or open-ended "
                "prompts, not agentic or tool-call work."
            ),
            llama_args={"--temp": "1.5"},
        ),
        KnobOption(
            id="high",
            name="Very loose (1.2)",
            description=(
                "Well above the default: more variety in wording and approach, and a smaller "
                "hit to formatting than 1.5. Still too loose for strict-format output."
            ),
            llama_args={"--temp": "1.2"},
        ),
        KnobOption(
            id="t10",
            name="Loose (1.0)",
            description=(
                "Just above the default: a little more variety in phrasing and word choice, with "
                "formatting largely intact. The mildest way to shake a model out of repetitive "
                "output."
            ),
            llama_args={"--temp": "1.0"},
        ),
        KnobOption(
            id="default",
            name="Default (0.8)",
            description=(
                "llama.cpp's own default. Works well for agentic work on most builds — start "
                "here and move only once you have a symptom that points somewhere else."
            ),
            llama_args={"--temp": "0.8"},
        ),
        KnobOption(
            id="t07",
            name="Focused (0.7)",
            description=(
                "A light tightening: phrasing gets more consistent run to run, with essentially "
                "no loss of the model's ability to recover from a bad opening token."
            ),
            llama_args={"--temp": "0.7"},
        ),
        KnobOption(
            id="t06",
            name="Very focused (0.6)",
            description=(
                "Visibly steadier than the default while still sampling freely among genuinely "
                "plausible tokens. A reasonable resting point for long agentic runs that only "
                "occasionally misformat."
            ),
            llama_args={"--temp": "0.6"},
        ),
        KnobOption(
            id="moderate",
            name="Tight (0.5)",
            description=(
                "Takes real weight off the tail: unlikely tokens still exist but rarely win. The "
                "first value worth trying when strict syntax breaks now and then and the default "
                "feels too permissive."
            ),
            llama_args={"--temp": "0.5"},
        ),
        KnobOption(
            id="t04",
            name="Very tight (0.4)",
            description=(
                "A step further for a model that still slips on strict syntax at 0.5. Variety in "
                "ordinary prose is noticeably reduced."
            ),
            llama_args={"--temp": "0.4"},
        ),
        KnobOption(
            id="low",
            name="Strict (0.3)",
            description=(
                "Substantially more deterministic, while still leaving enough probability "
                "elsewhere to escape a bad opening token on a retry. The usual answer to "
                "malformed tool calls."
            ),
            llama_args={"--temp": "0.3"},
        ),
        KnobOption(
            id="t02",
            name="Very strict (0.2)",
            description=(
                "Below the point where wording varies much between runs: the same prompt returns "
                "near-identical output, and only genuinely close calls are still decided by "
                "sampling."
            ),
            llama_args={"--temp": "0.2"},
        ),
        KnobOption(
            id="very-low",
            name="Rigid (0.15)",
            description=(
                "For a model that still drifts on strict-format output at 0.3: the distribution "
                "is sharp enough that a non-top token needs a near-tie to win. Repetition and "
                "loops get more likely from here down."
            ),
            llama_args={"--temp": "0.15"},
        ),
        KnobOption(
            id="t01",
            name="Very rigid (0.1)",
            description=(
                "Almost the greedy path, with a sliver of variety left for retries. Pick this "
                "over 0.05 when you want that retry to have some chance of coming out different."
            ),
            llama_args={"--temp": "0.1"},
        ),
        KnobOption(
            id="near-greedy",
            name="Near-greedy (0.05)",
            description=(
                "Effectively deterministic — a token essentially cannot win unless it was "
                "already the top candidate. Maximum format reliability, at the cost of variety, "
                "and of any chance to escape a bad opening token by retrying."
            ),
            llama_args={"--temp": "0.05"},
        ),
    ),
    default_option="default",
)

#: GPU offload, as a raw layer count rather than invented "half"/"most"
#: buckets — the useful values depend on the model's layer count and the
#: machine's VRAM, neither of which a fixed option list can know.
GPU_LAYERS_KNOB = LlamaKnob(
    id="gpu-layers",
    name="Layers on GPU",
    description=(
        "How many of the model's layers are offloaded to the GPU. -1 means all of them (the "
        "default, and what you want whenever the model fits); 0 keeps everything on the CPU. A "
        "value in between splits the model, trading speed for VRAM — llama.cpp runs the "
        "offloaded layers on the GPU and the rest on the CPU."
    ),
    kind=KnobKind.NUMBER,
    advanced=True,
    flag="--n-gpu-layers",
    minimum=-1,
    step=1,
    default_value="-1",
    unset_label="llama.cpp default",
)

#: MoE-expert offload. Only meaningful on a sparse-MoE GGUF; harmless (and
#: simply unused by llama.cpp) elsewhere, which is why it is shared rather
#: than declared per MoE family.
CPU_MOE_KNOB = LlamaKnob(
    id="cpu-moe",
    name="MoE expert layers on CPU",
    description=(
        "On a sparse mixture-of-experts model, keeps this many layers' expert weights in system "
        "RAM while the always-on shared layers stay on the GPU. This is what makes a large MoE "
        "model practical on a modest card: the experts are mostly idle, so moving them off the "
        "GPU costs far less speed than offloading whole layers would. Leave empty on a dense "
        "model — it does nothing there."
    ),
    kind=KnobKind.NUMBER,
    advanced=True,
    flag="--n-cpu-moe",
    minimum=0,
    step=1,
    unset_label="off",
)

#: Nucleus sampling: a hard cutoff in cumulative-probability space, layered
#: independently on top of :data:`TAIL_CULLING_KNOB`'s min-p/top-n-sigma
#: (see the module docstring on why these are separate axes). Every active
#: option also pins ``--top-k 0`` so top-p is the only thing this knob's
#: cutoff depends on — otherwise llama.cpp's own ``top-k 40`` would silently
#: prune the candidate set before top-p ever saw it. Advanced because tail
#: culling (min-p) is the better first tool for quantization noise; this is
#: for reproducing a specific external nucleus-sampling recipe or chasing an
#: exact cutoff tail-culling's percentile framing does not express.
_NUCLEUS_SAMPLING_STEPS: tuple[str, ...] = (
    "1.00",
    "0.95",
    "0.90",
    "0.85",
    "0.80",
)

NUCLEUS_SAMPLING_KNOB = LlamaKnob(
    id="nucleus-sampling",
    name="Nucleus sampling (top-p)",
    description=(
        "A hard cutoff on the candidate token set: only the smallest group of tokens whose "
        "probabilities add up to at least top-p is kept, and top-k is disabled so top-p is the "
        "only truncation this knob applies. Independent of Tail culling above, and stacks with "
        "it — reach for this when you want a fixed, reproducible cutoff rather than the "
        "min-p-relative-to-the-top-token shape Tail culling uses."
    ),
    kind=KnobKind.DROPDOWN,
    advanced=True,
    options=(
        KnobOption(
            id="off",
            name="llama.cpp defaults",
            description=(
                "No explicit top-k/top-p settings — llama.cpp's own top-k 40 and top-p 0.95 "
                "apply (or whatever else is otherwise in effect)."
            ),
        ),
        *(
            KnobOption(
                id=f"p{value.replace('.', '')}",
                name=f"top-p {value}",
                description=(
                    f"Keeps the smallest set of tokens whose cumulative probability reaches "
                    f"{value}, with top-k disabled."
                ),
                llama_args={"--top-k": "0", "--top-p": value},
            )
            for value in _NUCLEUS_SAMPLING_STEPS
        ),
    ),
    default_option="off",
)

#: Every shared knob, in Configure-modal display order (non-advanced first —
#: the UI groups by :attr:`~kodo.llms.local_registry.LlamaKnob.advanced`, but
#: keeping the declaration order aligned makes the table read the way the
#: modal looks). A family module builds an entry's knob tuple as
#: ``SHARED_KNOBS + (private knobs...)``.
SHARED_KNOBS: tuple[LlamaKnob, ...] = (
    KV_CACHE_KNOB,
    TAIL_CULLING_KNOB,
    TEMPERATURE_KNOB,
    GPU_LAYERS_KNOB,
    CPU_MOE_KNOB,
    NUCLEUS_SAMPLING_KNOB,
)
