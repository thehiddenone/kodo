"""The curated ``llama-server`` command-line argument catalog (doc/LLM_REGISTRY.md §4.7).

What drives the "Add argument" picker in kodo-vsix's **user-defined profile**
editor: a hand-maintained table of the ``llama-server`` flags worth exposing,
each with a type, a value domain and a one-line explanation, so a profile can
be built by picking flags from a list instead of typing raw command lines.

Deliberately *curated*, not exhaustive. ``llama-server --help`` lists roughly
two hundred flags, most of which are irrelevant to running a coding agent
(embedding endpoints, RPC backends, LoRA plumbing, deprecated aliases), and
transcribing all of them would create a table that silently rots against every
llama.cpp release. Anything not in here is still reachable — the profile
editor keeps a raw "one flag per line" box beside the picker for exactly that
(:func:`kodo.llms.local_registry.parse_llama_args_text`).

Two things this table is *not*:

- **Not validation.** Nothing here is enforced server-side; a profile may hold
  any flag at all. ``minimum``/``maximum``/``choices`` drive input widgets and
  the advisory ⚠, in the same advisory spirit as
  :attr:`~kodo.llms.SamplingParamSpec.sensible_minimum`.
- **Not the knob framework.** Knobs (:mod:`kodo.llms.local_registry._knobs`)
  are curated *combinations* of flags on the Default profile, chosen by kodo;
  this table is individual flags, chosen by the user, on their own profiles.

Sampling flags are not duplicated here — they are derived from
:data:`~kodo.llms.SAMPLING_PARAM_SPECS` (:func:`_sampling_arg_specs`), so the
recommended bands, the ``samplers`` name whitelist and the help text stay
single-sourced with the session sampling modal (doc/SAMPLING.md §8d/§8e). That
includes the repetition penalties: kodo never *ships* a configuration that
enables one (doc/QUANT_SAMPLING.md §3f), but a user who wants one on their own
profile is entitled to it, and hiding the flag here while offering it in the
per-session ⚙ modal would be an arbitrary split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ._sampling import SAMPLING_PARAM_SPECS
from .local_registry import RESERVED_LLAMA_ARGS

__all__ = [
    "LLAMA_ARG_CATALOG",
    "LlamaArgSpec",
    "llama_arg_catalog_to_json",
]


@dataclass(frozen=True)
class LlamaArgSpec:
    """One ``llama-server`` flag offered in the profile editor's argument picker.

    Attributes:
        flag: The flag exactly as it appears on the command line, long form
            (``"--ctx-size"``). This is the key written into a profile's
            ``llama_args``.
        label: Human-readable name for the picker's list and the row label.
        kind: Which input to render and how the value is spelled.
            ``"bool"`` is a *bare* flag — present with an empty-string value,
            no input at all. ``"enum"`` renders a ``<select>`` over
            :attr:`choices`. ``"str_list"`` is a delimiter-joined list (only
            ``samplers``, semicolon-joined — doc/SAMPLING.md §8b).
        category: Grouping header in the picker, e.g. ``"Context & memory"``.
            Free text; the UI groups by exact string in first-seen order.
        help: One-line explanation of what the flag does. Shown under the row.
        advanced: ``True`` puts the flag behind the picker's "Advanced"
            grouping. Same meaning as
            :attr:`~kodo.llms.SamplingParamSpec.advanced`.
        minimum: Advisory lower bound for a numeric input, or ``None``.
        maximum: Advisory upper bound, or ``None``.
        step: Numeric input step, or ``None``.
        choices: The accepted values for an ``"enum"`` flag, in display order.
            Empty for every other kind.
        placeholder: Hint text for a free-form ``"str"`` input, e.g. an
            example ``--override-tensor`` pattern. Empty when there is nothing
            useful to suggest.
        default: What llama.cpp does when the flag is absent, as display text
            (``"-1 (all layers)"``). Empty when there is no meaningful
            default to name. Purely informational.
        sensible_minimum: Lower end of the recommended band, when this flag
            mirrors a sampling parameter that ships one — carried through from
            :data:`~kodo.llms.SAMPLING_PARAM_SPECS` so the profile editor can
            raise the same ⚠ as the session sampling modal. ``None`` for every
            non-sampling flag.
        sensible_maximum: Upper end of the recommended band, or ``None``.
        valid_values: Hard whitelist for a ``"str_list"`` flag (only
            ``samplers``), or ``None``. An entry outside it is a hard error,
            not advice — see :attr:`~kodo.llms.SamplingParamSpec.valid_values`.
    """

    flag: str
    label: str
    kind: Literal["str", "int", "float", "bool", "enum", "str_list"]
    category: str
    help: str
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    placeholder: str = ""
    default: str = ""
    sensible_minimum: float | None = None
    sensible_maximum: float | None = None
    valid_values: tuple[str, ...] | None = None


#: The KV-cache precisions llama.cpp accepts for ``--cache-type-k``/``-v``,
#: cheapest-memory last. Shared by both flags.
_CACHE_TYPES: tuple[str, ...] = (
    "f32",
    "f16",
    "bf16",
    "q8_0",
    "q5_1",
    "q5_0",
    "q4_1",
    "q4_0",
    "iq4_nl",
)

_CONTEXT_ARGS: tuple[LlamaArgSpec, ...] = (
    LlamaArgSpec(
        flag="--ctx-size",
        label="Context size",
        kind="int",
        category="Context & memory",
        help="Context window in tokens. 0 uses the length the GGUF was trained at. Going beyond "
        "that needs the rope-scaling flags below.",
        minimum=0,
        step=1024,
        default="0 (the GGUF's own trained length)",
    ),
    LlamaArgSpec(
        flag="--cache-type-k",
        label="KV cache type (keys)",
        kind="enum",
        category="Context & memory",
        help="Precision of the attention key cache. Quantizing roughly halves what each token of "
        "context costs in memory.",
        choices=_CACHE_TYPES,
        default="f16",
    ),
    LlamaArgSpec(
        flag="--cache-type-v",
        label="KV cache type (values)",
        kind="enum",
        category="Context & memory",
        help="Precision of the attention value cache. Normally set to the same type as the key "
        "cache.",
        choices=_CACHE_TYPES,
        default="f16",
    ),
    LlamaArgSpec(
        flag="--rope-scaling",
        label="Rope scaling method",
        kind="enum",
        category="Context & memory",
        help="How positional encoding is stretched past the trained context length. YaRN is what "
        "modern long-context recipes use.",
        choices=("none", "linear", "yarn"),
        default="none",
    ),
    LlamaArgSpec(
        flag="--rope-scale",
        label="Rope scale factor",
        kind="float",
        category="Context & memory",
        help="How far the context is stretched — target context size divided by the model's "
        "native one. 4.0 takes a 256K model to 1M.",
        minimum=1.0,
        step=0.5,
        default="1.0 (no stretching)",
    ),
    LlamaArgSpec(
        flag="--yarn-orig-ctx",
        label="YaRN original context",
        kind="int",
        category="Context & memory",
        help="The model's native context length, which YaRN scales relative to. Must be the "
        "trained value, not the target.",
        minimum=0,
        step=1024,
    ),
    LlamaArgSpec(
        flag="--rope-freq-base",
        label="Rope frequency base",
        kind="float",
        category="Context & memory",
        help="Overrides the model's RoPE theta directly. An alternative to rope-scaling for "
        "context extension; do not use both at once.",
        advanced=True,
        minimum=0.0,
        step=1000.0,
        default="taken from the GGUF",
    ),
    LlamaArgSpec(
        flag="--yarn-ext-factor",
        label="YaRN extrapolation factor",
        kind="float",
        category="Context & memory",
        help="Mix between YaRN extrapolation and interpolation. -1.0 uses the model's own value; "
        "0.0 is pure interpolation.",
        advanced=True,
        minimum=-1.0,
        maximum=1.0,
        step=0.1,
        default="-1.0",
    ),
    LlamaArgSpec(
        flag="--yarn-attn-factor",
        label="YaRN attention factor",
        kind="float",
        category="Context & memory",
        help="Scales attention magnitude under YaRN. Leave alone unless a model card tells you "
        "otherwise.",
        advanced=True,
        minimum=0.0,
        step=0.1,
        default="1.0",
    ),
    LlamaArgSpec(
        flag="--defrag-thold",
        label="KV cache defrag threshold",
        kind="float",
        category="Context & memory",
        help="Fragmentation level at which the KV cache is compacted. Only matters with many "
        "parallel slots.",
        advanced=True,
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        default="0.1",
    ),
    LlamaArgSpec(
        flag="--keep",
        label="Tokens to keep",
        kind="int",
        category="Context & memory",
        help="How many tokens from the start of the prompt survive a context shift. -1 keeps all "
        "of them.",
        advanced=True,
        minimum=-1,
        step=1,
        default="0",
    ),
    LlamaArgSpec(
        flag="--mlock",
        label="Lock model in RAM",
        kind="bool",
        category="Context & memory",
        help="Prevents the OS from paging the model out. Stops swap thrash on a machine that is "
        "tight on memory — and makes it worse if the model does not fit.",
        advanced=True,
    ),
    LlamaArgSpec(
        flag="--no-mmap",
        label="Disable memory mapping",
        kind="bool",
        category="Context & memory",
        help="Loads the whole model up front instead of mapping it from disk. Slower to start, "
        "but avoids stalls later on a slow or networked filesystem.",
        advanced=True,
    ),
)

_OFFLOAD_ARGS: tuple[LlamaArgSpec, ...] = (
    LlamaArgSpec(
        flag="--n-gpu-layers",
        label="Layers on GPU",
        kind="int",
        category="GPU & performance",
        help="How many layers to offload to the GPU. -1 offloads all of them; 0 keeps everything "
        "on the CPU.",
        minimum=-1,
        step=1,
        default="-1 (all layers)",
    ),
    LlamaArgSpec(
        flag="--n-cpu-moe",
        label="MoE expert layers on CPU",
        kind="int",
        category="GPU & performance",
        help="On a sparse mixture-of-experts model, keeps this many layers' expert weights in "
        "system RAM while the shared layers stay on the GPU. Does nothing on a dense model.",
        minimum=0,
        step=1,
        default="0 (none)",
    ),
    LlamaArgSpec(
        flag="--split-mode",
        label="Multi-GPU split mode",
        kind="enum",
        category="GPU & performance",
        help="How a model is divided across several GPUs. 'layer' splits by layer, 'row' splits "
        "individual tensors, 'none' uses one GPU only.",
        choices=("none", "layer", "row"),
        default="layer",
    ),
    LlamaArgSpec(
        flag="--tensor-split",
        label="Tensor split ratios",
        kind="str",
        category="GPU & performance",
        help="Proportion of the model each GPU receives, comma-separated — e.g. 3,1 puts three "
        "quarters on the first GPU.",
        placeholder="3,1",
    ),
    LlamaArgSpec(
        flag="--main-gpu",
        label="Main GPU",
        kind="int",
        category="GPU & performance",
        help="Which GPU holds the small tensors that are not split. Index into the device list.",
        minimum=0,
        step=1,
        default="0",
    ),
    LlamaArgSpec(
        flag="--device",
        label="Devices",
        kind="str",
        category="GPU & performance",
        help="Restricts which backend devices are used, comma-separated. Run llama-server "
        "--list-devices to see the names.",
        advanced=True,
        placeholder="CUDA0,CUDA1",
    ),
    LlamaArgSpec(
        flag="--override-tensor",
        label="Tensor placement override",
        kind="str",
        category="GPU & performance",
        help="Forces tensors matching a regex onto a specific device — the manual version of the "
        "MoE-experts-on-CPU trick.",
        advanced=True,
        placeholder="exps=CPU",
    ),
    LlamaArgSpec(
        flag="--flash-attn",
        label="Flash attention",
        kind="enum",
        category="GPU & performance",
        help="A faster, more memory-efficient attention kernel. 'auto' lets llama.cpp decide "
        "based on the build and hardware.",
        choices=("auto", "on", "off"),
        default="auto",
    ),
    LlamaArgSpec(
        flag="--threads",
        label="CPU threads",
        kind="int",
        category="GPU & performance",
        help="Threads used for generation. Defaults to the number of physical cores; more is not "
        "always faster.",
        minimum=1,
        step=1,
        default="physical core count",
    ),
    LlamaArgSpec(
        flag="--threads-batch",
        label="CPU threads (batch)",
        kind="int",
        category="GPU & performance",
        help="Threads used for prompt processing, which parallelizes better than generation does.",
        advanced=True,
        minimum=1,
        step=1,
        default="same as CPU threads",
    ),
    LlamaArgSpec(
        flag="--batch-size",
        label="Logical batch size",
        kind="int",
        category="GPU & performance",
        help="Tokens submitted per prompt-processing batch. Larger is faster to first token and "
        "uses more memory.",
        minimum=1,
        step=64,
        default="2048",
    ),
    LlamaArgSpec(
        flag="--ubatch-size",
        label="Physical batch size",
        kind="int",
        category="GPU & performance",
        help="Tokens actually computed at once. Lower this before lowering the logical batch size "
        "if prompt processing runs out of memory.",
        minimum=1,
        step=64,
        default="512",
    ),
    LlamaArgSpec(
        flag="--parallel",
        label="Parallel slots",
        kind="int",
        category="GPU & performance",
        help="How many requests can be in flight at once. Each slot gets its own share of the "
        "context, so raising this shrinks the context per request.",
        minimum=1,
        step=1,
        default="1",
    ),
    LlamaArgSpec(
        flag="--no-warmup",
        label="Skip warm-up",
        kind="bool",
        category="GPU & performance",
        help="Skips the dummy pass llama.cpp normally runs at startup. Starts faster; the first "
        "real request pays for it instead.",
        advanced=True,
    ),
    LlamaArgSpec(
        flag="--numa",
        label="NUMA policy",
        kind="enum",
        category="GPU & performance",
        help="Memory placement strategy on a multi-socket machine. Irrelevant on a laptop or a "
        "single-socket desktop.",
        advanced=True,
        choices=("distribute", "isolate", "numactl"),
    ),
)

_BEHAVIOR_ARGS: tuple[LlamaArgSpec, ...] = (
    LlamaArgSpec(
        flag="--jinja",
        label="Use the model's chat template",
        kind="bool",
        category="Model behaviour",
        help="Applies the Jinja chat template embedded in the GGUF. Required for tool calling to "
        "work at all — leave this on unless you are replacing the template yourself.",
    ),
    LlamaArgSpec(
        flag="--reasoning-format",
        label="Reasoning format",
        kind="enum",
        category="Model behaviour",
        help="How a reasoning model's thinking block is parsed out of its output. 'auto' detects "
        "it from the template; 'none' leaves it inline.",
        choices=("auto", "none", "deepseek"),
        default="auto",
    ),
    LlamaArgSpec(
        flag="--chat-template",
        label="Chat template",
        kind="str",
        category="Model behaviour",
        help="Overrides the GGUF's built-in chat template with a named built-in one.",
        advanced=True,
        placeholder="chatml",
    ),
    LlamaArgSpec(
        flag="--chat-template-file",
        label="Chat template file",
        kind="str",
        category="Model behaviour",
        help="Overrides the chat template with a Jinja template read from a file.",
        advanced=True,
        placeholder="/path/to/template.jinja",
    ),
    LlamaArgSpec(
        flag="--override-kv",
        label="Metadata override",
        kind="str",
        category="Model behaviour",
        help="Overrides one metadata field inside the GGUF, as key=type:value. This is how a "
        "context-length cap recorded in the file is lifted.",
        advanced=True,
        placeholder="qwen35.context_length=int:1048576",
    ),
    LlamaArgSpec(
        flag="--n-predict",
        label="Max tokens to predict",
        kind="int",
        category="Model behaviour",
        help="Server-wide cap on generated tokens per request. -1 is unlimited; a low value will "
        "truncate the agent mid-answer.",
        advanced=True,
        minimum=-1,
        step=1,
        default="-1 (unlimited)",
    ),
)


def _sampling_arg_specs() -> tuple[LlamaArgSpec, ...]:
    """The sampling half of the catalog, derived from :data:`SAMPLING_PARAM_SPECS`.

    Uses each spec's ``cli_flags[0]`` (its canonical long flag) and carries the
    recommended band, ``samplers`` whitelist, step and help text straight
    through, so the profile editor raises exactly the same ⚠ as the session
    sampling modal without a second copy of the table. Specs with no CLI flag
    at all (``min_keep``) are skipped — there is nothing to write into a
    profile's launch args for them.
    """
    kinds: dict[str, Literal["str", "int", "float", "bool", "enum", "str_list"]] = {
        "float": "float",
        "int": "int",
        "str_list": "str_list",
    }
    return tuple(
        LlamaArgSpec(
            flag=spec.cli_flags[0],
            label=spec.label,
            kind=kinds[spec.kind],
            category="Sampling",
            help=spec.help,
            advanced=spec.advanced,
            minimum=spec.minimum,
            maximum=spec.maximum,
            step=spec.step,
            sensible_minimum=spec.sensible_minimum,
            sensible_maximum=spec.sensible_maximum,
            valid_values=spec.valid_values,
        )
        for spec in SAMPLING_PARAM_SPECS
        if spec.cli_flags
    )


#: Every flag the profile editor offers, grouped by
#: :attr:`LlamaArgSpec.category` in this order. Sampling comes last because it
#: is the largest group and the one a user is least likely to reach for on a
#: *profile* — per-session overrides (the ⚙ button in the chat footer) are the
#: better place for sampling experiments, since they apply without a restart.
LLAMA_ARG_CATALOG: tuple[LlamaArgSpec, ...] = (
    _CONTEXT_ARGS + _OFFLOAD_ARGS + _BEHAVIOR_ARGS + _sampling_arg_specs()
)


def _check_catalog() -> None:
    """Import-time sanity checks — a malformed catalog fails startup, not a launch."""
    seen: set[str] = set()
    for spec in LLAMA_ARG_CATALOG:
        if spec.flag in seen:
            raise ValueError(f"Duplicate flag in LLAMA_ARG_CATALOG: {spec.flag!r}")
        if spec.flag in RESERVED_LLAMA_ARGS:
            raise ValueError(
                f"{spec.flag!r} is reserved (kodo sets it per launch) and must not be offered "
                "in the argument catalog"
            )
        if spec.kind == "enum" and not spec.choices:
            raise ValueError(f"{spec.flag!r} is an enum flag but declares no choices")
        if spec.kind != "enum" and spec.choices:
            raise ValueError(f"{spec.flag!r} declares choices but is not an enum flag")
        seen.add(spec.flag)


_check_catalog()


def strip_reserved_llama_args(llama_args: dict[str, str]) -> dict[str, str]:
    """Drop every :data:`RESERVED_LLAMA_ARGS` key from *llama_args*.

    Applied to every user-supplied profile arg set before it is persisted (see
    :func:`kodo.llms.local_registry.add_profile`/
    :func:`~kodo.llms.local_registry.update_profile`), so a reserved flag can
    never reach :class:`~kodo.llms.llamacpp.LlamaServer`'s command line —
    where it would either be overridden anyway or break process management.

    Returns:
        dict[str, str]: A new dict; the input is not mutated.
    """
    return {k: v for k, v in llama_args.items() if k not in RESERVED_LLAMA_ARGS}


def llama_arg_catalog_to_json() -> list[dict[str, object]]:
    """:data:`LLAMA_ARG_CATALOG` as the wire payload kodo-vsix renders from.

    Shipped once with the local-registry payload (alongside ``sampling_specs``)
    rather than per session — it is a static table of help text that never
    changes at runtime. See doc/WS_PROTOCOL.md §5.12b.
    """
    return [
        {
            "flag": spec.flag,
            "label": spec.label,
            "kind": spec.kind,
            "category": spec.category,
            "help": spec.help,
            "advanced": spec.advanced,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "step": spec.step,
            "choices": list(spec.choices),
            "placeholder": spec.placeholder,
            "default": spec.default,
            "sensible_minimum": spec.sensible_minimum,
            "sensible_maximum": spec.sensible_maximum,
            "valid_values": list(spec.valid_values) if spec.valid_values is not None else None,
        }
        for spec in LLAMA_ARG_CATALOG
    ]
