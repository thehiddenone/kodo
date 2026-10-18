"""Which LLM a session runs on (doc/SESSION_LLM_BINDING.md §2).

An :class:`LlmSelection` is one LLM card as the sidebar shows it: a local
registry entry with the profile and knob values it launches with, or a cloud
vendor with a snapshot of its tier models. A session keeps two —
``llm_binding`` (what its last prompt ran with) and ``llm_selection`` (what
its next prompt binds to) — in ``transient.json``; an unbound session keeps
neither and follows the sidebar.

The engine never branches on a selection: it overlays it onto the
machine-wide settings (:meth:`LlmSelection.overlay`) and lets the existing
settings lookup (:func:`resolve_model_key`) do the rest.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from kodo.llms import (
    LaunchChoice,
    get_cloud_entry,
    get_cloud_registry,
    get_cloud_vendor_display_name,
    get_launch_choice,
    get_local_registry,
)

__all__ = ["CLOUD_TIERS", "DEFAULT_LOCAL_MODEL", "LlmSelection", "resolve_model_key"]

#: The four cloud effort tiers, in display order.
CLOUD_TIERS: tuple[str, ...] = ("low", "medium", "high", "max")

#: What ``models.local`` means when ``settings.json`` has no such key.
DEFAULT_LOCAL_MODEL = "unsloth-qwen38-27b-ud-q4-k-xl"


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _str_map(value: object) -> dict[str, str]:
    return {str(k): str(v) for k, v in _dict(value).items() if v is not None}


def resolve_model_key(settings: Mapping[str, object], capability: str) -> str:
    """The registry model key *settings* select for *capability*.

    In ``local`` mode every capability maps to the single selected local
    model. Otherwise OpenRouter's Auto mode wins, then the vendor's "use one
    model for all effort levels" shortcut, then the per-capability model,
    falling back through the other tiers (``medium`` first) and finally to the
    vendor's first hardcoded registry model. A bare capability name is never
    returned for a known vendor — an incomplete ``models.cloud.<vendor>`` map
    would otherwise 404 against the provider.

    Args:
        settings: A settings dict — the machine-wide one, or a session's
            overlay (:meth:`LlmSelection.overlay`).
        capability: ``'max'``, ``'high'``, ``'medium'``, or ``'low'``.

    Returns:
        str: A local registry name (local mode) or a cloud ``model_id``.
    """
    mode = str(settings.get("mode", "cloud"))
    models_map = _dict(settings.get("models", {}))
    if mode == "local":
        return str(models_map.get("local", DEFAULT_LOCAL_MODEL))

    vendor = str(settings.get("active_cloud_vendor", "anthropic"))
    # Auto mode overrides every tier to the router pseudo-model, regardless of
    # what models.cloud.openrouter holds -- that map is deliberately left
    # untouched so toggling Auto back off restores the Manual picks.
    if vendor == "openrouter" and bool(settings.get("openrouter_auto_mode", False)):
        return "openrouter/auto"
    uniform_vendor = _dict(_dict(models_map.get("cloud_uniform", {})).get(vendor, {}))
    if uniform_vendor.get("enabled"):
        uniform_model_id = uniform_vendor.get("model_id")
        if uniform_model_id:
            return str(uniform_model_id)
    vendor_map = _dict(_dict(models_map.get("cloud", {})).get(vendor, {}))
    for key in (capability, "medium", "high", "max", "low"):
        model_id = vendor_map.get(key)
        if model_id:
            return str(model_id)
    registry_models = get_cloud_registry().get(vendor, ())
    if registry_models:
        return registry_models[0].model_id
    return capability


@dataclass(frozen=True)
class LlmSelection:
    """One LLM card: a local entry with its launch choice, or a cloud vendor snapshot.

    Attributes:
        kind: ``"local"`` (kodo-managed GGUF or self-hosted
            ``custom_server_url`` entry) or ``"cloud"``.
        entry: Local registry entry name (``kind == "local"``); ``""`` means
            nothing is selected.
        profile_id: Local user-defined profile id, ``""`` for the Default profile.
        knobs: Local Default-profile knob values.
        vendor: Cloud vendor key (``kind == "cloud"``).
        tiers: Cloud ``{tier: model_id}`` for :data:`CLOUD_TIERS`.
        uniform_enabled: Whether the vendor's "one model for all" is on.
        uniform_model: That one model's id (``""`` when unset).
        openrouter_auto: OpenRouter's Auto mode (meaningful for ``openrouter`` only).
    """

    kind: str
    entry: str = ""
    profile_id: str = ""
    knobs: Mapping[str, str] = field(default_factory=dict)
    vendor: str = ""
    tiers: Mapping[str, str] = field(default_factory=dict)
    uniform_enabled: bool = False
    uniform_model: str = ""
    openrouter_auto: bool = False

    @property
    def is_local(self) -> bool:
        """Whether this is a local card (managed or self-hosted)."""
        return self.kind == "local"

    @property
    def launch_choice(self) -> LaunchChoice:
        """The profile and knobs a local card launches with (empty for cloud)."""
        return LaunchChoice(profile_id=self.profile_id, knobs=dict(self.knobs))

    @classmethod
    def from_json(cls, raw: object) -> LlmSelection | None:
        """Parse a stored or wire selection; ``None`` when *raw* is not one.

        Args:
            raw: A dict as written by :meth:`to_json` (extra keys, e.g. a
                binding's ``command``, are ignored).

        Returns:
            LlmSelection | None: The selection.
        """
        data = _dict(raw)
        kind = data.get("kind")
        if kind == "local":
            return cls(
                kind="local",
                entry=str(data.get("entry", "")),
                profile_id=str(data.get("profile_id", "")),
                knobs=_str_map(data.get("knobs")),
            )
        if kind == "cloud":
            uniform = _dict(data.get("uniform"))
            return cls(
                kind="cloud",
                vendor=str(data.get("vendor", "")),
                tiers={t: m for t, m in _str_map(data.get("tiers")).items() if t in CLOUD_TIERS},
                uniform_enabled=bool(uniform.get("enabled", False)),
                uniform_model=str(uniform.get("model_id") or ""),
                openrouter_auto=bool(data.get("openrouter_auto", False)),
            )
        return None

    def to_json(self) -> dict[str, object]:
        """The stored / wire form (doc/SESSION_LLM_BINDING.md §2).

        Returns:
            dict[str, object]: JSON-serialisable.
        """
        if self.is_local:
            return {
                "kind": "local",
                "entry": self.entry,
                "profile_id": self.profile_id,
                "knobs": dict(self.knobs),
            }
        return {
            "kind": "cloud",
            "vendor": self.vendor,
            "tiers": dict(self.tiers),
            "uniform": {"enabled": self.uniform_enabled, "model_id": self.uniform_model or None},
            "openrouter_auto": self.openrouter_auto,
        }

    @classmethod
    def from_settings(cls, settings: Mapping[str, object], kodo_dir: Path) -> LlmSelection:
        """The card the sidebar shows: *settings*' selection plus the entry's stored launch choice.

        Args:
            settings: The machine-wide settings.
            kodo_dir: User-level ``~/.kodo`` (for the local registry).

        Returns:
            LlmSelection: A snapshot — later sidebar edits do not change it.
        """
        models_map = _dict(settings.get("models", {}))
        if str(settings.get("mode", "cloud")) == "local":
            name = str(models_map.get("local", DEFAULT_LOCAL_MODEL))
            entry = get_local_registry(kodo_dir).get(name)
            choice = get_launch_choice(kodo_dir, entry) if entry is not None else LaunchChoice()
            return cls(
                kind="local",
                entry=name,
                profile_id=choice.profile_id,
                knobs=dict(choice.knobs),
            )
        vendor = str(settings.get("active_cloud_vendor", "anthropic"))
        uniform = _dict(_dict(models_map.get("cloud_uniform", {})).get(vendor, {}))
        return cls(
            kind="cloud",
            vendor=vendor,
            tiers={
                t: m
                for t, m in _str_map(_dict(models_map.get("cloud", {})).get(vendor)).items()
                if t in CLOUD_TIERS
            },
            uniform_enabled=bool(uniform.get("enabled", False)),
            uniform_model=str(uniform.get("model_id") or ""),
            openrouter_auto=vendor == "openrouter"
            and bool(settings.get("openrouter_auto_mode", False)),
        )

    def overlay(self, settings: Mapping[str, object]) -> dict[str, object]:
        """*settings* with this card selected in them, for every settings-driven lookup.

        Only the keys a card owns change: ``mode`` and ``models.local``, or
        ``mode``, ``active_cloud_vendor``, ``models.cloud.<vendor>``,
        ``models.cloud_uniform.<vendor>`` and ``openrouter_auto_mode``.

        Args:
            settings: The machine-wide settings (not mutated).

        Returns:
            dict[str, object]: A new settings dict.
        """
        result = copy.deepcopy(dict(settings))
        models_map = _dict(result.get("models", {}))
        result["models"] = models_map
        if self.is_local:
            result["mode"] = "local"
            models_map["local"] = self.entry
            return result
        result["mode"] = "cloud"
        result["active_cloud_vendor"] = self.vendor
        cloud = _dict(models_map.get("cloud", {}))
        cloud[self.vendor] = dict(self.tiers)
        models_map["cloud"] = cloud
        uniform = _dict(models_map.get("cloud_uniform", {}))
        uniform[self.vendor] = {
            "enabled": self.uniform_enabled,
            "model_id": self.uniform_model or None,
        }
        models_map["cloud_uniform"] = uniform
        if self.vendor == "openrouter":
            result["openrouter_auto_mode"] = self.openrouter_auto
        return result

    def label(self, settings: Mapping[str, object], capability: str, kodo_dir: Path) -> str:
        """The card's display name, as the header line and the change element show it.

        Args:
            settings: The machine-wide settings (the overlay base).
            capability: The top-level agent's capability tier — which of a
                cloud card's models the label names.
            kodo_dir: User-level ``~/.kodo``.

        Returns:
            str: A local entry's description (else its name), or
            ``"<Vendor> · <model>"`` for a cloud card.
        """
        if self.is_local:
            entry = get_local_registry(kodo_dir).get(self.entry)
            return (entry.description or entry.name) if entry is not None else self.entry
        model_id = resolve_model_key(self.overlay(settings), capability)
        cloud_entry = get_cloud_entry(self.vendor, model_id)
        model = cloud_entry.name if cloud_entry is not None else model_id
        return f"{get_cloud_vendor_display_name(self.vendor)} · {model}"
