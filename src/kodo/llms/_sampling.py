"""Request-level sampling parameters for ``llama-server`` (doc/SAMPLING.md).

Every knob ``llama-server`` accepts in a ``POST /v1/chat/completions`` body
that Kōdo lets a user tune, described **once** — in
:data:`SAMPLING_PARAM_SPECS` — and consumed from there by everything else:
JSON validation (:meth:`SamplingParams.from_json`), the request body
(:meth:`SamplingParams.to_request_body`), the flavor editor's CLI-vs-request
conflict warning (:func:`cli_flag_conflicts`), and the kodo-vsix sampling
modal, which renders its fields from the spec table shipped over the wire
rather than hardcoding a second copy of it.

The central invariant, and the reason every field is optional:

    **Unset means omitted, not "send the default".**

``llama-server`` seeds each request's sampling config from the values the
process was *launched* with and then overwrites only the fields the request
body actually contains. So omitting ``temperature`` against a server started
with ``--temp 0.6`` runs that request at 0.6, whereas sending llama.cpp's
built-in 0.8 would silently defeat the flavor's CLI arg. :class:`SamplingParams`
therefore stores only the parameters that are genuinely set, and never
materialises a default for one that isn't.

See doc/SAMPLING.md for what each parameter does to generated text, and
§9 there for the three-layer flavor-CLI → flavor-defaults → session-override
model this file underpins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "RESERVED_SAMPLING_FIELDS",
    "SAMPLER_NAMES",
    "SAMPLING_PARAM_SPECS",
    "SamplingParamSpec",
    "SamplingParams",
    "cli_flag_conflicts",
    "sampling_param_spec",
    "sampling_specs_to_json",
]

_log = logging.getLogger(__name__)

#: Sampler stage names accepted in the ``samplers`` ordering list. Matches
#: llama.cpp's ``common_sampler_type_from_name``; an unrecognised name is
#: dropped by :meth:`SamplingParams.from_json` rather than passed through,
#: since llama-server rejects the whole request on one bad entry (unlike an
#: unknown *field*, which it silently ignores — doc/SAMPLING.md §1a).
SAMPLER_NAMES: frozenset[str] = frozenset(
    {
        "dry",
        "top_k",
        "typ_p",
        "top_p",
        "min_p",
        "xtc",
        "temperature",
        "penalties",
        "top_n_sigma",
    }
)

#: Request-body fields a flavor or a session override may never set, with the
#: reason each is off limits. Enforced by :meth:`SamplingParams.from_json`,
#: which drops any of these keys before they can reach a request. The parallel
#: CLI-side restriction is ``RESERVED_REASONING_CAP_ARGS``
#: (``kodo/llms/_local_registry.py``); these two lists are deliberately
#: separate — that one guards *launch* args, this one guards *request* fields.
#: See doc/SAMPLING.md §9.
RESERVED_SAMPLING_FIELDS: dict[str, str] = {
    "max_tokens": (
        "computed per request from the session's thinking tier — a user value can "
        "starve the Qwen reasoning-budget mechanism of headroom"
    ),
    "n_predict": "llama.cpp's own spelling of max_tokens; same reason",
    "json_schema": "already carried by response_format for structured LLM calls",
    "grammar": "collides with the lazy tool-call grammar --jinja installs",
    "ignore_eos": "would stop any turn from ever ending cleanly",
    "logit_bias": "needs model-specific token IDs, not obtainable from the UI",
    "n_probs": "response-shape debugging; kodo ignores the extra response fields",
    "post_sampling_probs": "response-shape debugging; kodo ignores the extra fields",
}


@dataclass(frozen=True)
class SamplingParamSpec:
    """Static description of one tunable request-level sampling parameter.

    One instance per entry in :data:`SAMPLING_PARAM_SPECS`. Everything the UI
    needs to render a field, and everything the server needs to validate one,
    lives here so there is exactly one place to edit when llama.cpp gains or
    renames a knob.

    Attributes:
        name: The request-body key, spelled exactly as ``llama-server``
            expects it. Also the key used in :attr:`SamplingParams.values`
            and in the JSON persisted on a flavor or a session.
        kind: ``"float"``, ``"int"``, or ``"str_list"`` — drives both
            coercion in :meth:`SamplingParams.from_json` and which control
            kodo-vsix renders.
        label: Human-readable field label for the UI.
        advanced: ``False`` for the curated set the sampling modal shows
            up front, ``True`` for the ones behind its collapsed "Advanced"
            section. Purely a presentation hint — an advanced parameter is
            as settable as a curated one.
        minimum: Hard lower bound, or ``None`` for unbounded. Deliberately
            generous: these are validation limits (a value outside them is
            clamped and logged), not the "sensible range" guidance, which
            lives in :attr:`help` and doc/SAMPLING.md.
        maximum: Hard upper bound, or ``None`` for unbounded.
        step: UI step size for a numeric input. Ignored for ``"str_list"``.
        neutral: The value that *disables* this sampler, as a display string
            (``""`` when the parameter has no such value, e.g. ``seed``).
            Shown in the UI so "turn this off" is distinguishable from
            "leave it unset" — they are not the same thing (doc/SAMPLING.md
            §8a).
        cli_flags: Equivalent ``llama-server`` CLI flags. Drives the flavor
            editor's warning when the same knob is set both as a launch arg
            and as a request-level default. Empty for ``min_keep``, which is
            request-level only.
        help: One-line explanation, used as the UI field hint.
    """

    name: str
    kind: Literal["float", "int", "str_list"]
    label: str
    advanced: bool
    minimum: float | None
    maximum: float | None
    step: float | None
    neutral: str
    cli_flags: tuple[str, ...]
    help: str


#: Every tunable request-level sampling parameter, curated set first. Ordering
#: is the display order in the kodo-vsix sampling modal (and, within each
#: group, groups related knobs together — temperature, then the truncation
#: samplers, then the penalties).
#:
#: Parameters llama-server accepts but Kōdo deliberately does not expose are
#: listed in :data:`RESERVED_SAMPLING_FIELDS` with their reasons, and in
#: doc/SAMPLING.md §7/§9.
SAMPLING_PARAM_SPECS: tuple[SamplingParamSpec, ...] = (
    # --- curated -----------------------------------------------------------
    SamplingParamSpec(
        name="temperature",
        kind="float",
        label="Temperature",
        advanced=False,
        minimum=0.0,
        maximum=4.0,
        step=0.05,
        neutral="1.0",
        cli_flags=("--temp", "--temperature"),
        help="Randomness. 0 is greedy/deterministic; 0.0–0.3 suits code and tool calls, "
        "0.6–0.8 general chat, above 1.0 gets creative and format-fragile.",
    ),
    SamplingParamSpec(
        name="top_k",
        kind="int",
        label="Top-K",
        advanced=False,
        minimum=0,
        maximum=1000000,
        step=1,
        neutral="0",
        cli_flags=("--top-k",),
        help="Keep only the K most probable tokens. 20–50 typical; 0 disables. "
        "Absolute, so it ignores how confident the model actually is.",
    ),
    SamplingParamSpec(
        name="top_p",
        kind="float",
        label="Top-P (nucleus)",
        advanced=False,
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        neutral="1.0",
        cli_flags=("--top-p",),
        help="Keep the smallest set of tokens whose probabilities sum to P. "
        "0.9–0.95 typical; 1.0 disables.",
    ),
    SamplingParamSpec(
        name="min_p",
        kind="float",
        label="Min-P",
        advanced=False,
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        neutral="0.0",
        cli_flags=("--min-p",),
        help="Keep tokens at least this fraction as likely as the top token. 0.05 typical; "
        "0 disables. Scales with the model's confidence, so it is the most robust "
        "truncation sampler at high temperature.",
    ),
    SamplingParamSpec(
        name="repeat_penalty",
        kind="float",
        label="Repeat penalty",
        advanced=False,
        minimum=0.0,
        maximum=2.0,
        step=0.01,
        neutral="1.0",
        cli_flags=("--repeat-penalty",),
        help="Divides the logit of any recently-seen token. 1.0 disables; 1.05–1.15 is the "
        "usable band. Blind to legitimate repetition, so it damages code — prefer DRY.",
    ),
    SamplingParamSpec(
        name="repeat_last_n",
        kind="int",
        label="Repeat lookback",
        advanced=False,
        minimum=-1,
        maximum=1000000,
        step=1,
        neutral="0",
        cli_flags=("--repeat-last-n",),
        help="How many recent tokens the repeat penalty considers. 64 default, 0 disables, "
        "-1 means the whole context.",
    ),
    SamplingParamSpec(
        name="presence_penalty",
        kind="float",
        label="Presence penalty",
        advanced=False,
        minimum=-2.0,
        maximum=2.0,
        step=0.01,
        neutral="0.0",
        cli_flags=("--presence-penalty",),
        help="Flat one-off penalty for any token already used. 0 disables; 0.1–0.6 nudges "
        "toward new vocabulary.",
    ),
    SamplingParamSpec(
        name="frequency_penalty",
        kind="float",
        label="Frequency penalty",
        advanced=False,
        minimum=-2.0,
        maximum=2.0,
        step=0.01,
        neutral="0.0",
        cli_flags=("--frequency-penalty",),
        help="Penalty proportional to how often a token was already used. 0 disables. "
        "Harsher than presence penalty on code.",
    ),
    SamplingParamSpec(
        name="seed",
        kind="int",
        label="Seed",
        advanced=False,
        minimum=-1,
        maximum=2147483647,
        step=1,
        neutral="",
        cli_flags=("-s", "--seed"),
        help="RNG seed; -1 picks a new one per request. Reproducibility also requires an "
        "identical prompt, model and build — temperature 0 is the stronger lever.",
    ),
    # --- advanced ----------------------------------------------------------
    SamplingParamSpec(
        name="typical_p",
        kind="float",
        label="Typical-P",
        advanced=True,
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        neutral="1.0",
        cli_flags=("--typical", "--typical-p"),
        help="Locally typical sampling: keeps tokens whose surprisal is near the "
        "distribution's entropy. 0.9–0.95 when used; 1.0 disables. Use instead of "
        "Top-P/Min-P, not alongside.",
    ),
    SamplingParamSpec(
        name="top_n_sigma",
        kind="float",
        label="Top-N-sigma",
        advanced=True,
        minimum=-1.0,
        maximum=10.0,
        step=0.1,
        neutral="-1.0",
        cli_flags=("--top-nsigma", "--top-n-sigma"),
        help="Keep tokens within N standard deviations of the top logit. ~1.0 when used; "
        "negative disables. Works on logits, so the surviving set barely changes with "
        "temperature.",
    ),
    SamplingParamSpec(
        name="min_keep",
        kind="int",
        label="Min keep",
        advanced=True,
        minimum=0,
        maximum=100,
        step=1,
        neutral="0",
        cli_flags=(),
        help="Floor on how many candidates any truncation sampler may leave. 0 disables. "
        "Request-level only — there is no CLI equivalent.",
    ),
    SamplingParamSpec(
        name="dynatemp_range",
        kind="float",
        label="Dynamic temp range",
        advanced=True,
        minimum=0.0,
        maximum=4.0,
        step=0.05,
        neutral="0.0",
        cli_flags=("--dynatemp-range",),
        help="Varies temperature by ± this much per token based on entropy. 0 disables. "
        "Adds variance exactly where the model is least sure — avoid for structured output.",
    ),
    SamplingParamSpec(
        name="dynatemp_exponent",
        kind="float",
        label="Dynamic temp exponent",
        advanced=True,
        minimum=0.0,
        maximum=10.0,
        step=0.1,
        neutral="1.0",
        cli_flags=("--dynatemp-exp",),
        help="Biases where in the dynamic-temperature band sampling sits. Above 1.0 leans "
        "cold, below 1.0 leans hot. Only matters when the range is non-zero.",
    ),
    SamplingParamSpec(
        name="xtc_probability",
        kind="float",
        label="XTC probability",
        advanced=True,
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        neutral="0.0",
        cli_flags=("--xtc-probability",),
        help="Chance of removing the most likely tokens outright, forcing a less obvious "
        "continuation. 0 disables. Never enable for code or tool calls — it deletes the "
        "one valid token.",
    ),
    SamplingParamSpec(
        name="xtc_threshold",
        kind="float",
        label="XTC threshold",
        advanced=True,
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        neutral="",
        cli_flags=("--xtc-threshold",),
        help="Only tokens above this probability are eligible for XTC removal. 0.1 typical; "
        "above 0.5 disables XTC entirely.",
    ),
    SamplingParamSpec(
        name="dry_multiplier",
        kind="float",
        label="DRY multiplier",
        advanced=True,
        minimum=0.0,
        maximum=10.0,
        step=0.05,
        neutral="0.0",
        cli_flags=("--dry-multiplier",),
        help="Master switch for DRY repetition suppression. 0 disables; 0.8 typical. "
        "Penalises replaying a whole n-gram rather than reusing a token, so it is far "
        "safer on code than the repeat penalty.",
    ),
    SamplingParamSpec(
        name="dry_base",
        kind="float",
        label="DRY base",
        advanced=True,
        minimum=1.0,
        maximum=10.0,
        step=0.05,
        neutral="",
        cli_flags=("--dry-base",),
        help="How fast the DRY penalty grows with match length. 1.75 typical.",
    ),
    SamplingParamSpec(
        name="dry_allowed_length",
        kind="int",
        label="DRY allowed length",
        advanced=True,
        minimum=0,
        maximum=1000,
        step=1,
        neutral="",
        cli_flags=("--dry-allowed-length",),
        help="Repeats up to this many tokens are free. 2 default; raise to 4–6 for code, "
        "which repeats short sequences legitimately.",
    ),
    SamplingParamSpec(
        name="dry_penalty_last_n",
        kind="int",
        label="DRY lookback",
        advanced=True,
        minimum=-1,
        maximum=1000000,
        step=1,
        neutral="0",
        cli_flags=("--dry-penalty-last-n",),
        help="How far back DRY looks for repeats. -1 means the whole context, 0 disables.",
    ),
    SamplingParamSpec(
        name="dry_sequence_breakers",
        kind="str_list",
        label="DRY sequence breakers",
        advanced=True,
        minimum=None,
        maximum=None,
        step=None,
        neutral="",
        cli_flags=("--dry-sequence-breaker",),
        help="Strings that reset DRY's match tracking. Defaults to newline, colon, quote "
        "and asterisk; adding tab, semicolon, comma and brace helps on source code.",
    ),
    SamplingParamSpec(
        name="mirostat",
        kind="int",
        label="Mirostat mode",
        advanced=True,
        minimum=0,
        maximum=2,
        step=1,
        neutral="0",
        cli_flags=("--mirostat",),
        help="0 disables, 1 is the original algorithm, 2 the simplified one. When on, it "
        "REPLACES Top-K/Top-P/Min-P/Typical-P rather than adding to them.",
    ),
    SamplingParamSpec(
        name="mirostat_tau",
        kind="float",
        label="Mirostat tau",
        advanced=True,
        minimum=0.0,
        maximum=20.0,
        step=0.1,
        neutral="",
        cli_flags=("--mirostat-ent",),
        help="Target entropy Mirostat holds output at. 5.0 default; lower is more focused. "
        "Note the CLI spelling is --mirostat-ent.",
    ),
    SamplingParamSpec(
        name="mirostat_eta",
        kind="float",
        label="Mirostat eta",
        advanced=True,
        minimum=0.0,
        maximum=1.0,
        step=0.01,
        neutral="",
        cli_flags=("--mirostat-lr",),
        help="Mirostat's feedback learning rate. 0.1 default. Note the CLI spelling is "
        "--mirostat-lr.",
    ),
    SamplingParamSpec(
        name="adaptive_target",
        kind="float",
        label="Adaptive-P target",
        advanced=True,
        minimum=-1.0,
        maximum=1.0,
        step=0.01,
        neutral="-1.0",
        cli_flags=("--adaptive-target",),
        help="Target probability for the adaptive-p sampler; negative disables. Recent "
        "llama.cpp addition — silently inert on an older build.",
    ),
    SamplingParamSpec(
        name="adaptive_decay",
        kind="float",
        label="Adaptive-P decay",
        advanced=True,
        minimum=0.0,
        maximum=0.99,
        step=0.01,
        neutral="",
        cli_flags=("--adaptive-decay",),
        help="Decay rate of adaptive-p's running estimate. 0.90 default. Only matters when "
        "the adaptive-p target is enabled.",
    ),
    SamplingParamSpec(
        name="samplers",
        kind="str_list",
        label="Sampler order",
        advanced=True,
        minimum=None,
        maximum=None,
        step=None,
        neutral="",
        cli_flags=("--samplers",),
        help="Order the sampler stages run in. Temperature runs last by default, so "
        "truncation always sees raw probabilities. Valid names: "
        + ", ".join(sorted(SAMPLER_NAMES))
        + ".",
    ),
)

_SPECS_BY_NAME: dict[str, SamplingParamSpec] = {s.name: s for s in SAMPLING_PARAM_SPECS}

#: ``{cli_flag: request_field_name}`` for every flag with a request-level twin,
#: derived from the spec table so the two can never drift apart. Used by
#: :func:`cli_flag_conflicts`.
_CLI_FLAG_TO_FIELD: dict[str, str] = {
    flag: spec.name for spec in SAMPLING_PARAM_SPECS for flag in spec.cli_flags
}


def sampling_param_spec(name: str) -> SamplingParamSpec | None:
    """The :class:`SamplingParamSpec` for request field *name*, if tunable.

    Args:
        name (str): A request-body field name, e.g. ``"temperature"``.

    Returns:
        SamplingParamSpec | None: The spec, or ``None`` for an unknown or
        reserved field.
    """
    return _SPECS_BY_NAME.get(name)


def sampling_specs_to_json() -> list[dict[str, object]]:
    """The whole spec table, JSON-ready, for the kodo-vsix sampling modal.

    Shipped to the client (doc/WS_PROTOCOL.md) so the modal renders its
    fields, bounds, hints and grouping from the server's table instead of
    keeping a second hand-maintained copy that could drift — the same reason
    the thinking-tier families are pushed rather than hardcoded
    (doc/LLM_REGISTRY.md §4.5).

    Returns:
        list[dict[str, object]]: One dict per parameter, in display order.
    """
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "label": s.label,
            "advanced": s.advanced,
            "minimum": s.minimum,
            "maximum": s.maximum,
            "step": s.step,
            "neutral": s.neutral,
            "cli_flags": list(s.cli_flags),
            "help": s.help,
        }
        for s in SAMPLING_PARAM_SPECS
    ]


def _coerce(spec: SamplingParamSpec, raw: object) -> float | int | list[str] | None:
    """Coerce+validate one raw JSON value against *spec*, or ``None`` to drop it.

    Out-of-range numbers are clamped (and logged) rather than dropped: the
    bounds are a safety net behind a UI that already enforces the same ones,
    and clamping keeps roughly the requested behaviour instead of silently
    falling back to the server's launch-time value, which is what dropping
    would mean (doc/SAMPLING.md §1).
    """
    if raw is None:
        return None

    if spec.kind == "str_list":
        if not isinstance(raw, list):
            _log.warning("Sampling parameter %s expects a list, got %r — dropped", spec.name, raw)
            return None
        items = [str(item) for item in raw]
        if spec.name == "samplers":
            valid = [item for item in items if item in SAMPLER_NAMES]
            if len(valid) != len(items):
                # One bad name makes llama-server reject the entire request,
                # so unknown stages are dropped rather than forwarded.
                _log.warning(
                    "Dropped unknown sampler name(s) %s from `samplers`",
                    sorted(set(items) - set(valid)),
                )
            items = valid
        return items or None

    try:
        # Via str() for both kinds, so a JSON string ("0.4", sent by a webview
        # number input) parses the same as a real number, and a bool — which
        # float() would happily accept as 0.0/1.0 — falls out as a ValueError.
        text = str(raw).strip()
        number: float | int = float(text) if spec.kind == "float" else int(float(text))
    except (TypeError, ValueError):
        _log.warning("Sampling parameter %s expects a number, got %r — dropped", spec.name, raw)
        return None

    if spec.minimum is not None and number < spec.minimum:
        _log.warning("Sampling parameter %s=%s below %s — clamped", spec.name, number, spec.minimum)
        number = spec.minimum if spec.kind == "float" else int(spec.minimum)
    if spec.maximum is not None and number > spec.maximum:
        _log.warning("Sampling parameter %s=%s above %s — clamped", spec.name, number, spec.maximum)
        number = spec.maximum if spec.kind == "float" else int(spec.maximum)
    return number


@dataclass(frozen=True)
class SamplingParams:
    """A sparse set of request-level sampling parameters.

    Holds **only** the parameters that are actually set — an absent key means
    "don't send this field", which lets ``llama-server`` fall back to whatever
    the flavor's CLI args launched it with. There is deliberately no
    "everything, with defaults filled in" representation anywhere in this
    module; see the module docstring.

    Instances are used in two places, with the same shape and different
    lifetimes: as a flavor's request-level *defaults*
    (``LlamaFlavor.sampling``, global, edited in the flavor editor) and as a
    session's per-quant *overrides* (``SessionState.sampling``, session-scoped,
    edited in the chat footer's sampling modal). :meth:`merged_with` combines
    the two. See doc/SAMPLING.md §9.

    Attributes:
        values: ``{request_field_name: value}`` for set parameters only. Always
            validated — construct via :meth:`from_json` rather than passing a
            raw dict, unless the values are already known-good.
    """

    values: dict[str, float | int | list[str]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: object) -> SamplingParams:
        """Build from untrusted JSON, dropping anything unusable.

        Unknown keys, :data:`RESERVED_SAMPLING_FIELDS`, wrong-typed values and
        ``None``\\ s are dropped; out-of-range numbers are clamped. Never
        raises — a malformed persisted flavor or a stale client should degrade
        to "fewer parameters sent", never to a crashed session.

        Args:
            raw (object): Typically a ``dict`` loaded from JSON. Anything else
                yields an empty instance.

        Returns:
            SamplingParams: A validated, possibly empty instance.
        """
        if not isinstance(raw, dict):
            return cls()
        values: dict[str, float | int | list[str]] = {}
        for key, value in raw.items():
            name = str(key)
            if name in RESERVED_SAMPLING_FIELDS:
                _log.warning(
                    "Ignoring reserved sampling field %r — %s",
                    name,
                    RESERVED_SAMPLING_FIELDS[name],
                )
                continue
            spec = _SPECS_BY_NAME.get(name)
            if spec is None:
                _log.warning("Ignoring unknown sampling field %r", name)
                continue
            coerced = _coerce(spec, value)
            if coerced is not None:
                values[name] = coerced
        return cls(values=values)

    def to_json(self) -> dict[str, float | int | list[str]]:
        """A plain JSON-serialisable copy, for persistence and the wire."""
        return dict(self.values)

    def to_request_body(self) -> dict[str, object]:
        """The fields to merge into a ``chat.completions.create`` ``extra_body``.

        Identical content to :meth:`to_json` — a separate method because the
        two have different reasons to change (persistence shape vs. wire shape
        for llama-server), and because the call site reads better.

        Returns:
            dict[str, object]: Only the set parameters. ``{}`` when nothing is
            set, in which case the caller sends no sampling fields at all.
        """
        return dict(self.values)

    def merged_with(self, other: SamplingParams) -> SamplingParams:
        """Self overlaid by *other*, per parameter — *other* wins.

        Used to resolve a flavor's defaults (``self``) against a session's
        per-quant overrides (*other*). Note this is a per-*field* merge, so a
        flavor default survives unless the session overrides that exact
        parameter.

        Args:
            other (SamplingParams): The higher-priority set.

        Returns:
            SamplingParams: The combined set.
        """
        return SamplingParams(values={**self.values, **other.values})

    @property
    def is_empty(self) -> bool:
        """``True`` when no parameter is set (so nothing goes on the wire)."""
        return not self.values


def cli_flag_conflicts(llama_args: dict[str, str], sampling: SamplingParams) -> dict[str, str]:
    """Knobs set both as a launch flag and as a request-level parameter.

    Not an error — the request-level value simply wins for Kōdo's own calls,
    while the CLI value still applies to any other client pointed at the same
    server. It is almost always accidental though, so the flavor editor warns
    on it (doc/SAMPLING.md §9).

    Args:
        llama_args (dict[str, str]): A flavor's CLI args, as stored.
        sampling (SamplingParams): The same flavor's request-level defaults.

    Returns:
        dict[str, str]: ``{cli_flag: request_field_name}`` for each knob set in
        both places. Empty when there is no overlap.
    """
    conflicts: dict[str, str] = {}
    for flag in llama_args:
        field_name = _CLI_FLAG_TO_FIELD.get(flag)
        if field_name is not None and field_name in sampling.values:
            conflicts[flag] = field_name
    return conflicts
