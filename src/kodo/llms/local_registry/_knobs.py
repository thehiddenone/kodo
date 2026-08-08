"""The knob framework: typed, declarative llama-server launch-argument controls.

A **knob** is one user-facing control on an LLM's *Default profile* — a
checkbox, a dropdown, or a number input — that owns a fixed set of
``llama-server`` CLI flags and decides what those flags are set to. Knobs
replace the old one-flavor-at-a-time model, where every combination of
"512K context" x "medium tail culling" x "f16 KV cache" had to be enumerated
as its own :class:`~kodo.llms.local_registry.LlmProfile` literal. Knobs
compose instead: N knobs with k options each cover k**N configurations from
N declarations.

Every knob is **hardcoded in this package** — there is no user-defined knob,
and no way to add one over the wire. Shared knobs (offered on every entry)
live in :mod:`._knobs_shared`; a per-family private knob (e.g. Laguna's
YaRN-extended context sizes) is built by its family module, usually via
:mod:`._knobs_context`. An entry lists the knobs it offers in
:attr:`~kodo.llms.local_registry.LocalLLMEntry.knobs`.

The load-bearing invariant
--------------------------

**Two knobs on the same entry may never own the same CLI flag.**
:func:`validate_knobs` enforces it and every entry is checked at import time
(see :mod:`._catalog`). Without it, "which knob wins" would depend on
dictionary ordering and a user could set ``--temp`` from two controls that
silently disagree. It is also what lets the framework compose knob args by a
plain :func:`dict.update` in :func:`knob_selection_args` — no precedence
rules, no merge strategy, because no two knobs can ever collide.

A knob *may* own a flag that also appears in an entry's ``base_llama_args``
(``--ctx-size`` is the standard case: base pins it to ``0``, "use the GGUF's
own trained length", and a context knob's non-default option overrides it).
Base args are the floor, knob args are layered on top and win — that
direction is fixed and is the only precedence rule in the system.

Selections
----------

What the user picked is a flat ``dict[knob_id, str]`` (see
:func:`~kodo.llms.local_registry._profiles.get_knob_selections`), persisted
per entry. The string means different things per :class:`KnobKind`:

- ``CHECKBOX``/``DROPDOWN`` — an option id from :attr:`LlamaKnob.options`.
- ``NUMBER`` — the value as text, or ``""`` for "unset" (the flag is not
  emitted at all).

An absent, empty, or unrecognized entry always falls back to the knob's
default (:meth:`LlamaKnob.resolved_default`, possibly overridden per entry by
:attr:`~kodo.llms.local_registry.LocalLLMEntry.knob_defaults`) rather than
raising — a selection dict is persisted user data that can outlive the knob
definition it was written against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "KnobKind",
    "KnobOption",
    "LlamaKnob",
    "knob_owned_flags",
    "knob_selection_args",
    "resolve_knob_selections",
    "validate_knobs",
]

_log = logging.getLogger(__name__)


class KnobKind(StrEnum):
    """How a :class:`LlamaKnob` is rendered and what its selection string means.

    Values:
        CHECKBOX: Exactly two options, ids ``"off"``/``"on"``. Rendered as a
            checkbox; the selection is one of those two ids.
        DROPDOWN: Two or more options. Rendered as a ``<select>``; the
            selection is an option id.
        NUMBER: No options at all — a single flag (:attr:`LlamaKnob.flag`)
            whose numeric value the user types. Rendered as a number input
            with :attr:`LlamaKnob.minimum`/:attr:`~LlamaKnob.maximum`/
            :attr:`~LlamaKnob.step`; the selection is the value as text, or
            ``""`` meaning "don't pass this flag at all".
    """

    CHECKBOX = "checkbox"
    DROPDOWN = "dropdown"
    NUMBER = "number"


@dataclass(frozen=True)
class KnobOption:
    """One selectable state of a ``CHECKBOX``/``DROPDOWN`` knob.

    Attributes:
        id: Stable slug, unique within its knob. Persisted in the user's
            selection dict, so renaming one silently resets everybody who had
            it selected back to the knob's default — treat as a wire/storage
            identifier, not a label.
        name: Display name shown in the dropdown (or beside the checkbox).
        description: Human-readable explanation of what picking this state
            does and when to want it. Rendered under the control in the
            Configure modal, so it is prose, not a flag list — the flags are
            shown separately from :attr:`llama_args`.
        llama_args: The CLI flags this state contributes, ``{flag: value}``
            with ``""`` for a bare/valueless flag. May be empty, which is the
            normal shape of a "leave it to llama.cpp" default option.
    """

    id: str
    name: str
    description: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LlamaKnob:
    """One configurable control on an entry's Default profile.

    Attributes:
        id: Stable slug, unique across every knob in the process. Two entries
            that list a knob under the same id must list the *same knob*
            (:func:`validate_knobs` checks structural equality) — the wire
            payload deduplicates knob definitions by id into one top-level
            table, so a same-id-different-definition pair would make one
            entry's UI silently render the other's options.
        name: Display name (the control's label).
        description: What this knob controls, in prose. Always rendered; keep
            it useful on its own, since a checkbox has no per-option text.
        kind: See :class:`KnobKind`.
        advanced: ``False`` puts the knob in the Configure modal's main body;
            ``True`` hides it behind the "Advanced" section, collapsed by
            default. Same split as
            :attr:`kodo.llms.SamplingParamSpec.advanced`.
        options: The selectable states, in display order. Required (and only
            meaningful) for ``CHECKBOX``/``DROPDOWN``; always ``()`` for
            ``NUMBER``.
        default_option: Option id used when the user has not chosen one.
            ``""`` means "the first option". An entry may override this via
            :attr:`~kodo.llms.local_registry.LocalLLMEntry.knob_defaults`.
        flag: ``NUMBER`` only — the single CLI flag this knob writes.
        minimum: ``NUMBER`` only — inclusive lower bound for the input, or
            ``None`` for unbounded. Advisory (rendered as the input's ``min``);
            nothing clamps server-side, same posture as
            :attr:`kodo.llms.SamplingParamSpec.sensible_minimum`.
        maximum: ``NUMBER`` only — inclusive upper bound, or ``None``.
        step: ``NUMBER`` only — the input's step, or ``None`` for ``1``.
        unset_label: ``NUMBER`` only — what an empty value means, shown as the
            input's placeholder (e.g. ``"off"``). The flag is genuinely not
            emitted; this is not a synonym for zero.
        default_value: ``NUMBER`` only — the value used when the user has not
            typed one. ``""`` (the default) means unset, i.e. the flag is not
            emitted at all.
    """

    id: str
    name: str
    description: str = ""
    kind: KnobKind = KnobKind.DROPDOWN
    advanced: bool = False
    options: tuple[KnobOption, ...] = ()
    default_option: str = ""
    flag: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unset_label: str = ""
    default_value: str = ""

    def resolved_default(self) -> str:
        """This knob's own default selection string, before any per-entry override.

        The option id for ``CHECKBOX``/``DROPDOWN`` (:attr:`default_option`,
        falling back to the first option's id, or ``""`` for the
        never-legitimate no-options case), or :attr:`default_value` for
        ``NUMBER``.
        """
        if self.kind is KnobKind.NUMBER:
            return self.default_value
        if self.default_option:
            return self.default_option
        return self.options[0].id if self.options else ""

    def option(self, option_id: str) -> KnobOption | None:
        """The :class:`KnobOption` with *option_id*, or ``None`` if there is none."""
        return next((o for o in self.options if o.id == option_id), None)

    def llama_args_for(self, selection: str) -> dict[str, str]:
        """The CLI flags this knob contributes when set to *selection*.

        Args:
            selection: An option id (``CHECKBOX``/``DROPDOWN``) or a numeric
                value as text (``NUMBER``). An unrecognized option id, or a
                blank selection, falls back to :meth:`resolved_default` — a
                persisted selection can outlive the definition it names.

        Returns:
            dict[str, str]: A fresh dict, safe for the caller to mutate.
            Empty when the resolved state contributes nothing (a "leave it to
            llama.cpp" option, or an unset ``NUMBER``).
        """
        if self.kind is KnobKind.NUMBER:
            value = selection.strip() or self.default_value.strip()
            if not value or not self.flag:
                return {}
            return {self.flag: value}
        chosen = self.option(selection) or self.option(self.resolved_default())
        return dict(chosen.llama_args) if chosen is not None else {}


def knob_owned_flags(knob: LlamaKnob) -> frozenset[str]:
    """Every CLI flag *knob* can write, across all of its states.

    The union of every option's ``llama_args`` keys for a
    ``CHECKBOX``/``DROPDOWN`` knob, or ``{knob.flag}`` for a ``NUMBER`` one.
    This — not "the flags the current selection happens to set" — is what
    :func:`validate_knobs` compares, since two knobs whose *reachable* flag
    sets intersect are a collision waiting for the wrong pair of selections,
    not a collision only in the states that happen to collide today.
    """
    if knob.kind is KnobKind.NUMBER:
        return frozenset({knob.flag}) if knob.flag else frozenset()
    return frozenset(flag for option in knob.options for flag in option.llama_args)


def validate_knobs(knobs: tuple[LlamaKnob, ...], *, context: str) -> None:
    """Assert *knobs* form a legal knob set for one entry.

    Checks, in order:

    1. No duplicate knob ids (a knob listed twice, or two different knobs
       sharing an id).
    2. No two knobs own the same CLI flag (:func:`knob_owned_flags`) — the
       framework's load-bearing invariant, see the module docstring.
    3. Each knob is internally coherent: a ``CHECKBOX`` has exactly the two
       options ``off``/``on``, a ``DROPDOWN`` has at least two, a ``NUMBER``
       names a flag and carries no options, option ids are unique, and
       ``default_option`` (when set) names a real option.

    Called at import time for every catalog entry (:mod:`._catalog`), so a
    malformed knob declaration is a hard startup failure rather than a
    mystery at launch time.

    Args:
        knobs: The knobs one entry offers.
        context: What is being validated, for the error message (an entry
            name, or a table name like ``"SHARED_KNOBS"``).

    Raises:
        ValueError: On any of the above.
    """
    seen: dict[str, LlamaKnob] = {}
    owners: dict[str, str] = {}
    for knob in knobs:
        previous = seen.get(knob.id)
        if previous is not None:
            if previous != knob:
                raise ValueError(
                    f"{context}: two different knobs share the id {knob.id!r} — knob ids are "
                    "global identifiers, deduplicated into one wire-level table"
                )
            raise ValueError(f"{context}: knob {knob.id!r} is listed more than once")
        seen[knob.id] = knob

        _validate_knob_shape(knob, context=context)

        for flag in sorted(knob_owned_flags(knob)):
            other = owners.get(flag)
            if other is not None:
                raise ValueError(
                    f"{context}: knobs {other!r} and {knob.id!r} both own {flag!r} — two knobs "
                    "on one entry may never write the same llama-server flag"
                )
            owners[flag] = knob.id


def _validate_knob_shape(knob: LlamaKnob, *, context: str) -> None:
    """Per-knob structural checks — see :func:`validate_knobs` step 3."""
    where = f"{context}: knob {knob.id!r}"
    if not knob.id or not knob.name:
        raise ValueError(f"{where} must have a non-empty id and name")

    if knob.kind is KnobKind.NUMBER:
        if not knob.flag:
            raise ValueError(f"{where} is a NUMBER knob and must name a flag")
        if knob.options:
            raise ValueError(f"{where} is a NUMBER knob and must not declare options")
        return

    if knob.flag:
        raise ValueError(f"{where} is not a NUMBER knob and must not set flag")
    option_ids = [o.id for o in knob.options]
    if len(option_ids) != len(set(option_ids)):
        raise ValueError(f"{where} has duplicate option ids")
    if knob.kind is KnobKind.CHECKBOX:
        if option_ids != ["off", "on"]:
            raise ValueError(
                f"{where} is a CHECKBOX knob and must declare exactly two options, "
                f'"off" then "on" (got {option_ids})'
            )
    elif len(knob.options) < 2:
        raise ValueError(f"{where} is a DROPDOWN knob and needs at least two options")
    if knob.default_option and knob.default_option not in option_ids:
        raise ValueError(
            f"{where} has default_option {knob.default_option!r}, which is not one of its options"
        )


def knob_selection_args(
    knobs: tuple[LlamaKnob, ...],
    selections: dict[str, str],
    defaults: dict[str, str],
) -> dict[str, str]:
    """The combined CLI flags *knobs* contribute given *selections*.

    Applied in declaration order via a plain update — safe precisely because
    :func:`validate_knobs` guarantees no two knobs write the same flag, so
    the order can never change the result (see the module docstring).

    Args:
        knobs: The entry's knobs, in declaration order.
        selections: The user's persisted ``{knob_id: selection}`` map. Keys
            naming a knob this entry doesn't offer are ignored.
        defaults: The entry's ``knob_defaults`` — per-entry overrides of a
            knob's own :meth:`LlamaKnob.resolved_default`, consulted only
            where *selections* has nothing for that knob.

    Returns:
        dict[str, str]: The flags to layer on top of the entry's base args.
    """
    args: dict[str, str] = {}
    for knob in knobs:
        selection = selections.get(knob.id) or defaults.get(knob.id, "")
        args.update(knob.llama_args_for(selection))
    return args


def resolve_knob_selections(
    knobs: tuple[LlamaKnob, ...],
    selections: dict[str, str],
    defaults: dict[str, str],
) -> dict[str, str]:
    """The *effective* selection for every knob in *knobs*, defaults filled in.

    What the Configure modal renders as the current state: one entry per
    knob, never sparse, so the UI never has to re-derive a default. Ordering
    matches *knobs*.

    A stored selection naming an option the knob no longer has is replaced by
    the resolved default (and logged) rather than surfaced — the UI would
    otherwise show a ``<select>`` with no matching ``<option>``.
    """
    resolved: dict[str, str] = {}
    for knob in knobs:
        stored = selections.get(knob.id, "")
        fallback = defaults.get(knob.id, "") or knob.resolved_default()
        if knob.kind is KnobKind.NUMBER:
            resolved[knob.id] = stored.strip() or fallback
            continue
        if stored and knob.option(stored) is None:
            _log.info(
                "Knob %r has no option %r any more; falling back to %r",
                knob.id,
                stored,
                fallback,
            )
            stored = ""
        resolved[knob.id] = stored or fallback
    return resolved
