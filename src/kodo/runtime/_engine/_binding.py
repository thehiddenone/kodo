"""Session ↔ LLM binding (doc/SESSION_LLM_BINDING.md).

A session binds to an LLM only when a prompt is dequeued
(:meth:`BindingMixin._bind_for_prompt`): its pending selection — or, for an
unbound session, the sidebar's card — becomes the binding, and every LLM
lookup the engine makes from then on reads the machine-wide settings with
that binding overlaid (:meth:`BindingMixin._effective_settings`). The same
moment diffs the session's parameters against the previous prompt's and
pushes the change element (``session.params``).

Also owns the session's hold on the managed llama-server
(:class:`~kodo.llms.LlamaLease`): its command line goes into every local
request's routing, and the worker releases the hold when a task ends.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path

from kodo.llms import (
    LLAMA_ARG_CATALOG,
    MTP_DRAFT_MODEL_FLAG,
    LaunchChoice,
    get_context_window,
    get_local_registry,
    get_profiles,
    resolve_profile_id,
    sampling_param_spec,
)
from kodo.llms.llamacpp import RemoteLlamaEndpoint, plan_llama_command, resolve_llama_launch
from kodo.project import kodo_user_dir

from .._llm_selection import CLOUD_TIERS, LlmSelection, resolve_model_key
from .._session_params import ParamsSnapshot, ParamValue, diff_params
from ._llm import _thinking_default_for
from ._proto import EngineHost

_log = logging.getLogger(__name__)

#: llama-server flags never listed in the change element: where things live
#: (paths, address, log file), and the fixed reasoning-budget sentence.
_HIDDEN_FLAGS = frozenset(
    {
        "--model",
        "-m",
        "--host",
        "--port",
        "--log-file",
        "--log-timestamps",
        "--alias",
        "--reasoning-budget-message",
    }
)
_FLAG_LABELS: dict[str, str] = {spec.flag: spec.label for spec in LLAMA_ARG_CATALOG}


def _dict(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _flag_value(flag: str, args: Mapping[str, str]) -> str | None:
    """How *flag* reads in a change element: absent → ``None``, bare → ``"on"``."""
    if flag not in args:
        return None
    value = args[flag]
    if not value:
        return "on"
    return Path(value).name if flag == MTP_DRAFT_MODEL_FLAG else value


def _scalar(value: object) -> str:
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return str(value)


def _local_params(
    selection: LlmSelection, kodo_dir: Path, sampling: Mapping[str, Mapping[str, object]]
) -> dict[str, ParamValue]:
    """Profile, llama-server flags and sampling overrides of a local card."""
    rows: dict[str, ParamValue] = {}
    entry = get_local_registry(kodo_dir).get(selection.entry)
    if entry is not None and entry.kind != "custom_server_url":
        choice = selection.launch_choice
        profile_id = resolve_profile_id(kodo_dir, entry, choice)
        names = {p.id: p.name for p in get_profiles(kodo_dir, entry)}
        rows["profile"] = ParamValue(
            "Profile", profile_id, "", {"display": names.get(profile_id, "Default")}
        )
        try:
            launch = resolve_llama_launch(entry, kodo_dir, choice).llama_args
            default = resolve_llama_launch(entry, kodo_dir, LaunchChoice()).llama_args
        except RuntimeError:
            launch, default = {}, {}
        for flag in [*default, *(f for f in launch if f not in default)]:
            if flag in _HIDDEN_FLAGS:
                continue
            rows[f"flag:{flag}"] = ParamValue(
                _FLAG_LABELS.get(flag, flag), _flag_value(flag, launch), _flag_value(flag, default)
            )
    for name, value in sorted(sampling.get(selection.entry, {}).items()):
        spec = sampling_param_spec(name)
        rows[f"sampling:{name}"] = ParamValue(
            spec.label if spec is not None else name, _scalar(value), None
        )
    return rows


def _cloud_params(selection: LlmSelection, defaults: Mapping[str, object]) -> dict[str, ParamValue]:
    """A cloud card's tier snapshot, against ``settings.json``'s shipped defaults."""
    models = _dict(defaults.get("models"))
    vendor_defaults = _dict(_dict(models.get("cloud")).get(selection.vendor))
    uniform_default = _dict(_dict(models.get("cloud_uniform")).get(selection.vendor))
    rows: dict[str, ParamValue] = {}
    for tier in CLOUD_TIERS:
        default = vendor_defaults.get(tier)
        rows[f"tier:{tier}"] = ParamValue(
            f"{tier.capitalize()} effort model",
            selection.tiers.get(tier),
            None if default is None else str(default),
        )
    default_uniform = uniform_default.get("model_id") if uniform_default.get("enabled") else None
    rows["uniform"] = ParamValue(
        "One model for all",
        (selection.uniform_model or None) if selection.uniform_enabled else None,
        None if default_uniform is None else str(default_uniform),
    )
    if selection.vendor == "openrouter":
        rows["openrouter_auto"] = ParamValue(
            "OpenRouter Auto",
            "on" if selection.openrouter_auto else "off",
            "on" if defaults.get("openrouter_auto_mode") else "off",
        )
    return rows


class BindingMixin:
    """Which LLM the session runs on, and the change element at each prompt."""

    _get_default_settings: Callable[[], dict[str, object]]

    # -- the two records ------------------------------------------------------

    def _bound_selection(self: EngineHost) -> LlmSelection | None:
        """What the last prompt ran with; ``None`` while the session is unbound."""
        return LlmSelection.from_json(self._transient.llm_binding)

    def _pending_selection(self: EngineHost) -> LlmSelection | None:
        """What the next prompt binds to; ``None`` while the session is unbound."""
        return LlmSelection.from_json(self._transient.llm_selection)

    def _effective_settings(self: EngineHost) -> dict[str, object]:
        """The settings every LLM call of this session resolves against.

        The machine-wide settings, with the session's binding overlaid once it
        has one (doc/SESSION_LLM_BINDING.md §2.1) — so another window's sidebar
        pick never moves a bound session.
        """
        settings = self._get_settings()
        binding = self._bound_selection()
        return binding.overlay(settings) if binding is not None else settings

    def _selection_settings(self: EngineHost) -> dict[str, object]:
        """The settings the *next* prompt will run with — what thinking tiers describe."""
        settings = self._get_settings()
        pending = self._pending_selection()
        return pending.overlay(settings) if pending is not None else settings

    def _launch_choice_for(self: EngineHost, model_key: str) -> LaunchChoice | None:
        """The bound profile and knobs for local *model_key*; ``None`` = the registry's."""
        binding = self._bound_selection()
        if binding is not None and binding.is_local and binding.entry == model_key:
            return binding.launch_choice
        return None

    # -- the llama-server lease -------------------------------------------------

    def _lease_holder(self: EngineHost) -> str:
        """This session's name in the gateway's llama-server lease."""
        return self._orch_session_id or self._session.session_id

    def _llama_command_for(self: EngineHost, model_key: str) -> tuple[str, ...] | None:
        """The managed llama-server argv a local request on *model_key* needs.

        ``None`` when kodo does not manage the server — a self-hosted entry, a
        headless ``--llama-url`` attach — or the model cannot be launched at
        all (the plugin then reports why).
        """
        if RemoteLlamaEndpoint.url() is not None:
            return None
        kodo_dir = kodo_user_dir()
        entry = get_local_registry(kodo_dir).get(model_key)
        if entry is None or entry.kind == "custom_server_url":
            return None
        try:
            return plan_llama_command(entry, kodo_dir, self._launch_choice_for(model_key))
        except RuntimeError:
            return None

    def _release_llama_lease(self: EngineHost) -> None:
        """End this session's hold on the managed llama-server (the turn is over)."""
        self._gateway.release_local(self._lease_holder())

    # -- binding -------------------------------------------------------------

    async def _bind_for_prompt(self: EngineHost) -> bool:
        """Bind the session for the prompt just dequeued (doc/SESSION_LLM_BINDING.md §2.2).

        Returns:
            bool: ``False`` when the prompt must not run — nothing usable is
            selected; the user has already been told why.
        """
        kodo_dir = kodo_user_dir()
        settings = self._get_settings()
        target = self._pending_selection() or LlmSelection.from_settings(settings, kodo_dir)
        if target.is_local and target.entry not in get_local_registry(kodo_dir):
            message = (
                f"The local LLM {target.entry!r} is no longer installed. "
                "Pick another one in the Kōdo sidebar, then send the prompt again."
                if target.entry
                else "No LLM is selected for this session. Pick one in the Kōdo sidebar, "
                "then send the prompt again."
            )
            await self._emitters.emit_error(message, recoverable=True)
            await self._emitters.emit_state()
            return False

        previous_binding = self._bound_selection()
        if previous_binding is not None and previous_binding != target:
            new_key = resolve_model_key(target.overlay(settings), self._top_agent_capability())
            choice = target.launch_choice if target.is_local else None
            await self._compactor.compact_before_switch(
                get_context_window(new_key, kodo_dir, choice)
            )

        binding = target.to_json()
        if target.is_local:
            entry = get_local_registry(kodo_dir).get(target.entry)
            command: tuple[str, ...] = ()
            if entry is not None and entry.kind != "custom_server_url":
                try:
                    command = plan_llama_command(entry, kodo_dir, target.launch_choice)
                except RuntimeError as exc:
                    _log.info("Binding %r without a planned command line: %s", target.entry, exc)
            binding["command"] = list(command)
        self._transient.set_llm_state(selection=target.to_json(), binding=binding)
        await self._sync_thinking_level_to_model()
        self._compactor.note_active_model(self._resolve_model_key(self._top_agent_capability()))

        snapshot = self._params_snapshot()
        previous = ParamsSnapshot.from_json(self._transient.params_snapshot)
        rows = diff_params(previous, snapshot)
        if previous is None and self._main_messages:
            # A session from before bindings existed (D14): it simply binds.
            rows = []
        self._transient.set_llm_state(params_snapshot=snapshot.to_json())
        self._refresh_llm_state()
        if rows:
            await self._emitters.emit_session_params(previous is None, rows)
        await self._emitters.emit_state()
        return True

    async def handle_llm_selection_capture(self: EngineHost) -> bool:
        """Take the sidebar's current card as this session's pending selection (D7).

        Returns:
            bool: Whether the session is bound (an unbound one ignores it —
            it follows the sidebar anyway).
        """
        if self._bound_selection() is None:
            return False
        selection = LlmSelection.from_settings(self._get_settings(), kodo_user_dir())
        self._transient.set_llm_state(selection=selection.to_json())
        await self._sync_thinking_level_to_model()
        self._refresh_llm_state()
        await self._emitters.emit_state()
        return True

    def _refresh_llm_state(self: EngineHost) -> None:
        """Recompute ``state.llm`` from the two records."""
        kodo_dir = kodo_user_dir()
        settings = self._get_settings()
        capability = self._top_agent_capability()
        binding = self._bound_selection()
        pending = self._pending_selection()
        label = binding.label(settings, capability, kodo_dir) if binding is not None else ""
        next_label = ""
        if binding is not None and pending is not None and pending != binding:
            next_label = pending.label(settings, capability, kodo_dir)
            if next_label == label:
                next_label = f"{label} (changed settings)"
        self._session.llm = {
            "bound": binding is not None,
            "binding": binding.to_json() if binding is not None else None,
            "selection": pending.to_json() if pending is not None else None,
            "label": label,
            "next_label": next_label,
        }

    def _params_snapshot(self: EngineHost) -> ParamsSnapshot:
        """Every session parameter as of now, against the bound LLM (§4)."""
        kodo_dir = kodo_user_dir()
        binding = self._bound_selection()
        assert binding is not None, "_params_snapshot runs after binding"
        capability = self._top_agent_capability()
        agent = self._top_agent_name()
        agent_labels = {a.name: a.label for a in self._registry.top_agents()}
        base_llm = self._current_base_llm()
        params: dict[str, ParamValue] = {
            "agent": ParamValue(
                "Agent",
                agent,
                self._registry.default_top_agent(),
                {"display": agent_labels.get(agent, agent)},
            ),
            "mode": ParamValue(
                "Mode", "autonomous" if self._session.autonomous else "interactive", "interactive"
            ),
            "edit_control": ParamValue("Edit Control", self._session.edit_control, "smart"),
            "command_control": ParamValue("Tool Control", self._session.command_control, "smart"),
            "thinking": ParamValue(
                "Thinking",
                self._session.thinking_level,
                _thinking_default_for(base_llm),
                {"base_llm": base_llm},
            ),
        }
        if binding.is_local:
            params.update(_local_params(binding, kodo_dir, self._session.sampling))
            identity = f"local:{binding.entry}"
        else:
            params.update(_cloud_params(binding, self._get_default_settings()))
            model = resolve_model_key(self._effective_settings(), capability)
            identity = f"cloud:{binding.vendor}:{model}"
        return ParamsSnapshot(
            llm_identity=identity,
            llm_label=binding.label(self._get_settings(), capability, kodo_dir),
            params=params,
        )
