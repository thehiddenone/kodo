"""Session parameters as of one prompt, and what changed between two prompts.

The change element a session shows right before a prompt
(doc/SESSION_LLM_BINDING.md §4) is the diff of two :class:`ParamsSnapshot`
objects: the one recorded at the previous prompt and the one taken now. Each
parameter carries its default *for the LLM it was taken with*, which is what
the listing rule needs: a parameter is listed when it changed, unless it went
from the old LLM's default to the new LLM's default (D8). The very first
prompt lists every parameter that is not at its default (D10).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

__all__ = ["ParamValue", "ParamsSnapshot", "diff_params"]

#: The parameter id of the LLM itself — listed whenever its identity changes.
LLM_PARAM = "llm"


@dataclass(frozen=True)
class ParamValue:
    """One parameter's value and its default, as of one prompt.

    Attributes:
        label: Display name (the client's fallback when it has no own name).
        value: The value; ``None`` means "not set".
        default: The value it would have untouched, for the LLM it belongs to.
        extra: Context the client needs to display *value* (e.g. a thinking
            tier's ``base_llm``, an agent's ``display``); each key reaches the
            change element as ``from_<key>``/``to_<key>``.
    """

    label: str
    value: str | None
    default: str | None
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_default(self) -> bool:
        """Whether :attr:`value` equals :attr:`default`."""
        return self.value == self.default


_UNSET = ParamValue(label="", value=None, default=None)


@dataclass(frozen=True)
class ParamsSnapshot:
    """Every session parameter as of one prompt.

    Attributes:
        llm_identity: What makes two LLMs "the same" — the local entry name,
            or the cloud vendor and the model the top-level agent runs on.
        llm_label: The LLM's display name.
        params: ``{param_id: value}`` in display order (``llm`` excluded).
    """

    llm_identity: str
    llm_label: str
    params: Mapping[str, ParamValue]

    def to_json(self) -> dict[str, object]:
        """The ``params_snapshot`` stored in ``transient.json``.

        Returns:
            dict[str, object]: JSON-serialisable.
        """
        return {
            "llm_identity": self.llm_identity,
            "llm_label": self.llm_label,
            "params": [
                {
                    "id": pid,
                    "label": p.label,
                    "value": p.value,
                    "default": p.default,
                    "extra": dict(p.extra),
                }
                for pid, p in self.params.items()
            ],
        }

    @classmethod
    def from_json(cls, raw: object) -> ParamsSnapshot | None:
        """Parse a stored snapshot; ``None`` when *raw* is not one.

        Args:
            raw: What :meth:`to_json` wrote.

        Returns:
            ParamsSnapshot | None: The snapshot.
        """
        if not isinstance(raw, dict):
            return None
        data = cast(dict[str, object], raw)
        params: dict[str, ParamValue] = {}
        rows = data.get("params")
        for row in cast(list[object], rows) if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, object], row)
            value = item.get("value")
            default = item.get("default")
            extra = item.get("extra")
            params[str(item.get("id", ""))] = ParamValue(
                label=str(item.get("label", "")),
                value=None if value is None else str(value),
                default=None if default is None else str(default),
                extra=(
                    {str(k): str(v) for k, v in cast(dict[str, object], extra).items()}
                    if isinstance(extra, dict)
                    else {}
                ),
            )
        return cls(
            llm_identity=str(data.get("llm_identity", "")),
            llm_label=str(data.get("llm_label", "")),
            params=params,
        )


def _change(pid: str, old: ParamValue, new: ParamValue) -> dict[str, object]:
    change: dict[str, object] = {
        "param": pid,
        "label": new.label or old.label,
        "from": old.value,
        "to": new.value,
    }
    for key in sorted(set(old.extra) | set(new.extra)):
        change[f"from_{key}"] = old.extra.get(key)
        change[f"to_{key}"] = new.extra.get(key)
    return change


def diff_params(old: ParamsSnapshot | None, new: ParamsSnapshot) -> list[dict[str, object]]:
    """The change element's rows, going from *old* to *new* (doc/SESSION_LLM_BINDING.md §4).

    With no *old* (the first prompt) the LLM row comes first, followed by every
    parameter not at its default, each with ``from: None``. Otherwise the LLM
    row appears when its identity changed, and any other parameter is listed
    when its value changed and it did not merely go from *old*'s default to
    *new*'s default. A parameter only one side has counts as "not set, at its
    default" on the other.

    Args:
        old: The snapshot recorded at the previous prompt, or ``None``.
        new: The snapshot taken now.

    Returns:
        list[dict[str, object]]: ``{param, label, from, to, [from_<k>, to_<k>]}``
        rows; empty when nothing is worth listing.
    """
    if old is None:
        rows: list[dict[str, object]] = [
            {"param": LLM_PARAM, "label": "LLM", "from": None, "to": new.llm_label}
        ]
        rows += [_change(pid, _UNSET, p) for pid, p in new.params.items() if not p.is_default]
        return rows

    rows = []
    if old.llm_identity != new.llm_identity:
        rows.append(
            {"param": LLM_PARAM, "label": "LLM", "from": old.llm_label, "to": new.llm_label}
        )
    ids = list(new.params) + [pid for pid in old.params if pid not in new.params]
    for pid in ids:
        before = old.params.get(pid, _UNSET)
        after = new.params.get(pid, _UNSET)
        if before.value == after.value:
            continue
        if before.is_default and after.is_default:
            continue
        rows.append(_change(pid, before, after))
    return rows
