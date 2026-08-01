"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
Two mandatory preambles — the **security** preamble (``preamble_security.md``)
and the **performance** preamble (``preamble_performance.md``) — are prepended,
in that order, to every subagent's system prompt. Because the system prompt is
rebuilt fresh on every turn, both preambles are always present regardless of
context compaction (compaction only rewrites the conversation history, never the
system prompt).

An agent's granted tools are **not** described in its prompt. They reach the
model through the LLM tool-definition ``tools`` argument, whose description is
built from the spec by :func:`kodo.toolspecs.tool_description`. The registry's
only concern with ``tools:`` frontmatter is therefore validation (every declared
name must resolve to a :class:`~kodo.toolspecs.ToolSpec`, checked at load time)
and the autonomous filter: tools whose spec marks ``autonomous_mode`` as
``unavailable`` are dropped from the :attr:`SubAgent.tools` set returned by
:meth:`AgentRegistry.get` when ``autonomous=True``, so they are never offered.

Two tools are **generated per agent** rather than taken from the static catalog,
so their schemas are the sub-agents' real ones instead of an opaque ``object``:

- :meth:`AgentRegistry.run_subagent_specs` mints one ``run_subagent_<name>``
  tool per invocable sub-agent in a caller's allow-list, each declaring that
  sub-agent's ``input_schema`` inline. A sub-agent that declares a ``critic:``
  gets the loop contract (an optional ``max_rounds``; a ``review`` block in its
  output), because one such call runs the entire author→critic loop.
- :meth:`AgentRegistry.return_result_specs` mints the ``return_result`` an agent
  sees, with ``result`` bound to that agent's own ``output_schema``.

Both are fed to :func:`kodo.tools.tools_for_agent` as replacements for the
catalog's canonical ``run_subagent`` / ``return_result`` entries; see
:func:`kodo.runtime.agent_tool_specs`, which is the single place that assembles
them.

A caller agent (one with a ``subagents:`` allow-list) may also embed a
``{PLACEHOLDER:SUBAGENTS}`` token. It is replaced with a **sub-agent roster**:
an intro paragraph explaining the ``Kind`` and ``Review`` columns, then a table
with one row per *invocable* sub-agent (critics are absorbed into their author's
``Review`` cell, since no caller ever spawns one), followed by each listed
sub-agent's caller-agnostic ``## Purpose`` paragraph — in the caller's allow-list
order. The roster is built from the *callee* agents' frontmatter
(``role``/``critic``/``standalone``) and ``## Purpose`` body, so the description
lives once with each sub-agent and is reused by every caller. It carries **no**
schemas: those reach the caller as real JSON Schema on the generated tools above.
See :meth:`AgentRegistry.render_subagents_section`.

A schema-bearing agent's own system prompt never contains its input or output
schema either — no ``## Your Task Contract`` block, no JSON dump. It only gets
:data:`_INPUT_PARAMETERS_NOTE`, a short fixed pointer to where its real task
lands: the first user turn, rendered per call (with real values, per-field
descriptions, and the `return_result` reminder) by
:func:`kodo.runtime._engine._subagents._render_task_input`, never here.

Raises :class:`~._loader.AgentLoadError` on duplicate names, missing entries, a
tool with no matching :class:`~kodo.toolspecs.ToolSpec`, or a ``critic:`` that
does not resolve to an agent declaring ``role: critic``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from kodo.toolspecs import (
    ALL_TOOLS,
    RETURN_RESULT,
    RUN_SUBAGENT,
    ToolSpec,
    build_return_result_spec,
    build_run_subagent_spec,
    run_subagent_tool_name,
)

from ._loader import AgentLoadError, SubAgent, load_agent
from ._subagentspec import SubAgentSpec
from .specs import ALL_SUBAGENTS

_SECURITY_PREAMBLE_FILENAME = "preamble_security.md"
_PERFORMANCE_PREAMBLE_FILENAME = "preamble_performance.md"
_SUBAGENTS_PLACEHOLDER = "{PLACEHOLDER:SUBAGENTS}"

# The terminal tool every schema-bearing sub-agent is auto-granted (so it can
# return its result against its declared output schema). Granted in the registry
# rather than per-frontmatter so it can never drift from a spec's existence.
# Replaced per-agent by ``return_result_specs`` before it reaches the LLM.
_RETURN_RESULT_TOOL = RETURN_RESULT.name

# The catalog name an agent declares in its ``tools:`` frontmatter to opt into
# spawning sub-agents. It is never offered as-is: ``run_subagent_specs`` expands
# it into one variant per sub-agent the agent may invoke.
_RUN_SUBAGENT_TOOL = RUN_SUBAGENT.name

# Intro paragraph that precedes the roster table. Drawn from the callees'
# ``standalone``/``critic`` frontmatter, it tells the caller how to read the
# ``Kind`` column (**workflow** agents advance an ordered pipeline and depend on
# upstream artifacts; **standalone** agents are on-demand specialists with no
# such dependency) and the ``Review`` column (whether one call also runs the
# author/critic loop).
_SUBAGENTS_INTRO = (
    "Each row's **Tool** is the exact tool that invokes that sub-agent; its "
    "parameters are the sub-agent's own task shape, declared on the tool itself.\n\n"
    "The sub-agents come in two kinds, marked in the **Kind** column. "
    "**Workflow** sub-agents advance a pre-determined pipeline: each one consumes "
    "the artifacts produced by the stage before it, so they run in a fixed order "
    "and depend on upstream output. **Standalone** sub-agents are specialists you "
    "invoke whenever the need arises; they sit outside the pipeline and do not "
    "depend on the outcome of any other agent.\n\n"
    "The **Review** column says what happens after the sub-agent produces its "
    "file. Where it names a critic, one call runs the *entire* author/critic loop: "
    "the engine spawns the sub-agent, hands its primary file to that critic, and "
    "re-runs the sub-agent with the critic's concerns until the critic accepts or "
    "the round budget is spent. You never invoke a critic yourself and you never "
    'call the tool again to run "another round" — the returned `review` block '
    "tells you how the loop ended."
)

# Every tool spec, keyed by tool name (names are unique in the catalog).
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}

# Every sub-agent's typed interface, keyed by agent name. An agent that has an
# entry here is "schema-bearing": it is auto-granted ``return_result`` and gets
# ``_INPUT_PARAMETERS_NOTE`` rendered into its own prompt. Its schemas reach a
# *caller* as real JSON Schema on the generated ``run_subagent_<name>`` tool
# (never restated in any roster), and reach the agent *itself* as the real
# values under ``## Input Parameters`` at the bottom of its first message (see
# ``kodo.runtime._engine._subagents._render_task_input``) — no schema is ever
# shown in a system prompt. Entry agents (guide/problem_solver) have no spec
# and are left untouched.
SUBAGENT_SPECS_BY_NAME: dict[str, SubAgentSpec] = {s.name: s for s in ALL_SUBAGENTS}

# Short note injected into every schema-bearing agent's own system prompt,
# right before its own body (replacing the old ``## Your Task Contract``,
# which restated the raw input schema as prose — a real per-call rendering,
# not a schema dump, now lives in the first user turn instead; see
# ``_render_task_input``). Deliberately carries no schema and no per-agent
# detail: the concrete values, their descriptions, and the `return_result`
# reminder are rendered fresh per call, where the real task is actually known.
_INPUT_PARAMETERS_NOTE = (
    "Your task arrives as your first message: free-form `instructions`, "
    "followed by an **Input Parameters** section listing every other value "
    "you were given — the last part of that message, with a reminder there "
    "of how to return your result."
)


def _review_output_schema(output_schema: dict[str, object], critic: str) -> dict[str, object]:
    """Return *output_schema* with the author/critic loop's ``review`` block added.

    What a ``run_subagent_<author>`` call returns when the engine ran a review
    loop: everything the author itself declared, plus how the loop ended. The
    caller needs both — the author's ``primary_path``/``summary`` to schedule the
    next stage, and ``review`` to know whether this file is settled or needs its
    attention.

    Args:
        output_schema: The author sub-agent's own declared ``output_schema``.
        critic: Name of the critic that reviewed it (named in the prose so the
            caller can attribute the concerns).

    Returns:
        dict[str, object]: A new schema; the input is not mutated.
    """
    props_raw = output_schema.get("properties")
    properties: dict[str, object] = dict(props_raw) if isinstance(props_raw, dict) else {}
    required_raw = output_schema.get("required")
    required = [str(r) for r in required_raw] if isinstance(required_raw, list) else []

    properties["review"] = {
        "type": "object",
        "description": f"How the `{critic}` review loop ended.",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "The file's status in its evolution log after the last round: "
                    "'accepted', 'pending_acceptance', 'needs_revision', or "
                    "'pending_review'."
                ),
            },
            "outcome": {
                "type": "string",
                "description": (
                    "Why the loop stopped: 'accepted' (the critic accepted and any "
                    "user sign-off landed), 'escalated' (the author returned a "
                    "blocker in `reason` it cannot resolve, so no critic ran and "
                    "no further round was spent — resolve it and re-run), "
                    "'max_rounds' (the budget ran out with concerns outstanding), "
                    "'not_converging' (concerns stopped decreasing, so further "
                    "rounds were judged wasteful), or 'not_reviewed' (the author "
                    "reported no file to review). "
                    "Anything but 'accepted' needs your decision."
                ),
            },
            "rounds": {
                "type": "integer",
                "description": "How many author→critic rounds ran.",
            },
            "concerns": {
                "type": "array",
                "description": "The concerns still outstanding; empty when accepted.",
                "items": {"type": "object"},
            },
        },
        "required": ["status", "outcome", "rounds", "concerns"],
    }
    return {
        **output_schema,
        "properties": properties,
        "required": [*required, "review"],
    }


# Tools withheld entirely in autonomous mode.
_AUTONOMOUS_DISABLED: frozenset[str] = frozenset(
    t.name for t in ALL_TOOLS if t.autonomous_mode and "unavailable" in t.autonomous_mode.lower()
)


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent returned by :meth:`get` has the security and performance
    preambles prepended (in that order), its ``{PLACEHOLDER:SUBAGENTS}`` roster
    filled in (when it embeds one), and its tool set filtered for the requested
    mode.

    Args:
        agents_dir: Directory containing ``preamble_security.md``,
            ``preamble_performance.md``, any ``base_*.md`` shared snippets, the
            ``subagent_*.md`` files, and the ``agent_*.md`` entry-agent files
            (``guide``, ``problem_solver``).

    Raises:
        AgentLoadError: a preamble or base file is missing or empty, an agent
            references a tool with no matching :class:`~kodo.toolspecs.ToolSpec`,
            or an agent references a ``bases:`` entry with no ``base_*.md`` file.
    """

    __slots__ = ("__agents", "__preamble", "__bases")

    def __init__(self, agents_dir: Path) -> None:
        # Security first (it takes precedence), then performance. Both are always
        # re-prepended on every render, so compaction can never drop them.
        security = self.__load_preamble(agents_dir, _SECURITY_PREAMBLE_FILENAME)
        performance = self.__load_preamble(agents_dir, _PERFORMANCE_PREAMBLE_FILENAME)
        self.__preamble = f"{security}\n\n{performance}"
        # Shared base snippets (``base_<name>.md``), keyed by ``<name>``. Agents
        # opt into them via the frontmatter ``bases:`` list; they are never loaded
        # as agents (the agent globs are ``subagent_*.md`` and ``agent_*.md``).
        self.__bases: dict[str, str] = {}
        for path in sorted(agents_dir.glob("base_*.md")):
            name = path.stem[len("base_") :]
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise AgentLoadError(f"{path}: base file is empty")
            self.__bases[name] = text
        self.__agents: dict[str, SubAgent] = {}
        # Sub-agents (``subagent_*.md``) and the user-facing entry agents
        # (``agent_*.md`` — ``guide``, ``problem_solver``) share one registry,
        # looked up by name regardless of which filename prefix they use.
        agent_paths = sorted(agents_dir.glob("subagent_*.md")) + sorted(
            agents_dir.glob("agent_*.md")
        )
        for path in agent_paths:
            agent = load_agent(path)
            # Validate every declared tool resolves now, at load time, so a bad
            # frontmatter reference fails fast rather than at first dispatch.
            self.__validate_tools(agent.tools, path)
            # Validate every declared base exists, for the same fail-fast reason.
            for base in agent.bases:
                if base not in self.__bases:
                    raise AgentLoadError(
                        f"{path}: base {base!r} has no base_{base}.md in {agents_dir}"
                    )
            self.__agents[agent.name] = agent
        # Second pass — every agent is loaded now, so cross-agent references can
        # be validated. Fail-fast at construction, same as the tool/base checks
        # above: a declared ``critic:`` must resolve to a real agent that
        # actually declares ``role: critic`` (otherwise the engine would spawn
        # something that never records a verdict), and each
        # ``{PLACEHOLDER:SUBAGENTS}`` roster's listed sub-agents must exist and
        # carry a ``## Purpose`` section.
        for agent in self.__agents.values():
            # ``subagents:`` and the ``run_subagent`` grant are two halves of one
            # decision: the allow-list says *which* sub-agents, the tool grant is
            # what expands into the per-sub-agent tools. Either alone is silently
            # inert, so reject the mismatch here rather than let an agent ship
            # with an allow-list it has no way to act on.
            if bool(agent.subagents) != (_RUN_SUBAGENT_TOOL in agent.tools):
                raise AgentLoadError(
                    f"{agent.source_path}: a 'subagents:' allow-list and the "
                    f"'{_RUN_SUBAGENT_TOOL}' tool grant must be declared together "
                    f"(has allow-list: {bool(agent.subagents)}, has tool: "
                    f"{_RUN_SUBAGENT_TOOL in agent.tools})"
                )
            if agent.critic:
                paired = self.__agents.get(agent.critic)
                if paired is None:
                    raise AgentLoadError(
                        f"{agent.source_path}: critic {agent.critic!r} has no "
                        f"subagent_{agent.critic}.md in the registry"
                    )
                if not paired.is_critic:
                    raise AgentLoadError(
                        f"{agent.source_path}: critic {agent.critic!r} does not declare "
                        f"'role: critic' in its own frontmatter"
                    )
            if _SUBAGENTS_PLACEHOLDER in agent.system_prompt:
                self.__render_subagents_section(agent)

    @staticmethod
    def __load_preamble(agents_dir: Path, filename: str) -> str:
        path = agents_dir / filename
        if not path.is_file():
            raise AgentLoadError(f"{path}: preamble file is missing")
        preamble = path.read_text(encoding="utf-8").strip()
        if not preamble:
            raise AgentLoadError(f"{path}: preamble file is empty")
        return preamble

    @staticmethod
    def __validate_tools(agent_tools: frozenset[str], path: Path) -> None:
        """Fail fast when an agent's frontmatter names an unknown tool.

        Run at load time (not first render) so a typo in ``tools:`` surfaces when
        the registry is built rather than mid-session.
        """
        for name in sorted(agent_tools):
            if name not in _SPECS_BY_NAME:
                raise AgentLoadError(f"{path}: tool {name!r} has no ToolSpec in kodo.toolspecs")

    def __render_subagents_section(self, caller: SubAgent) -> str:
        """Render the sub-agent roster that fills *caller*'s ``{PLACEHOLDER:SUBAGENTS}``.

        Three parts, in this order:

        1. An **intro paragraph** (:data:`_SUBAGENTS_INTRO`) explaining how to
           read the ``Kind`` column — workflow (ordered, upstream-dependent) vs
           standalone (on-demand specialist) — and that a sub-agent with a critic
           reviews itself.
        2. A **roster table** with one row per *invocable* sub-agent in the
           caller's ``subagents:`` allow-list order, naming the
           ``run_subagent_<name>`` tool that invokes it. A **critic**
           (``role: critic``) is not invocable — the engine spawns it as part of
           its author's loop — so it is absorbed into that author's ``Review``
           column and gets no row. The ``Kind`` column reads ``standalone`` when
           the callee declares ``standalone: true``, else ``workflow``.
        3. A **purpose paragraph** per sub-agent in the allow-list — invocable
           ones and critics alike — so an author and its critic read adjacent.
           Each is the caller-agnostic ``## Purpose`` body from that agent's file.

        The callees' input/output **schemas are deliberately absent**: each one
        now reaches the caller as the real JSON Schema on its own
        ``run_subagent_<name>`` tool definition, so restating it here would
        duplicate the authoritative copy in a channel that cannot stay in sync
        with it.

        Validates (fail-fast) that every listed sub-agent exists and carries a
        ``## Purpose`` section.
        """
        order = caller.subagent_order
        for sub in order:
            if sub not in self.__agents:
                raise AgentLoadError(
                    f"{caller.source_path}: subagents entry {sub!r} has no "
                    f"subagent_{sub}.md in the registry"
                )
            if not self.__agents[sub].purpose:
                raise AgentLoadError(
                    f"{caller.source_path}: sub-agent {sub!r} has no '## Purpose' "
                    f"section, required to render {_SUBAGENTS_PLACEHOLDER}"
                )

        rows: list[str] = []
        for sub in order:
            agent = self.__agents[sub]
            if agent.is_critic:
                continue  # spawned by the engine inside its author's loop, never by a caller
            review = f"`{agent.critic}`, automatically" if agent.critic else "none — single pass"
            kind = "standalone" if agent.standalone else "workflow"
            rows.append(f"| `{run_subagent_tool_name(sub)}` | `{sub}` | {review} | {kind} |")
        table = (
            "| Tool | Sub-agent | Review | Kind |\n"
            "| ---- | --------- | ------ | ---- |\n" + "\n".join(rows)
        )

        paras = [
            f"### {self.__agents[sub].display_name} (`{sub}`)\n\n{self.__agents[sub].purpose}"
            for sub in order
        ]
        return _SUBAGENTS_INTRO + "\n\n" + table + "\n\n" + "\n\n".join(paras)

    def render_subagents_section(self, name: str) -> str:
        """Public access to the rendered sub-agent roster for *name*'s allow-list.

        Same content the registry injects at ``{PLACEHOLDER:SUBAGENTS}``, exposed
        so callers (e.g. prompt-review tooling) can render an agent's roster even
        when its own body does not embed the placeholder.

        Raises:
            AgentLoadError: No agent file for *name*, or a listed sub-agent is
                missing or lacks a ``## Purpose`` section.
        """
        if name not in self.__agents:
            raise AgentLoadError(
                f"No agent file for {name!r}. Expected: subagents/subagent_{name}.md "
                f"or subagents/agent_{name}.md"
            )
        return self.__render_subagents_section(self.__agents[name])

    def __finalize(self, agent: SubAgent, autonomous: bool) -> SubAgent:
        """Render *agent* for the requested mode.

        Filters autonomous-disabled tools out of the effective tool set, then
        prepends the shared base snippets (if any) and the global preamble.
        """
        spec = SUBAGENT_SPECS_BY_NAME.get(agent.name)
        effective_tools = agent.tools
        # Schema-bearing sub-agents are auto-granted the terminal return tool so
        # they can return their result against their declared output schema.
        if spec is not None:
            effective_tools = effective_tools | {_RETURN_RESULT_TOOL}
        if autonomous and _AUTONOMOUS_DISABLED:
            effective_tools = frozenset(t for t in effective_tools if t not in _AUTONOMOUS_DISABLED)
        system_prompt = agent.system_prompt
        if _SUBAGENTS_PLACEHOLDER in system_prompt:
            system_prompt = system_prompt.replace(
                _SUBAGENTS_PLACEHOLDER, self.__render_subagents_section(agent)
            )
        # Order of precedence: global preamble (security + performance) first,
        # then any shared base contract, then (for a schema-bearing agent) the
        # short Input Parameters pointer note, then the agent's own body (which
        # may specialize the base). Bases are validated to exist at load time.
        note = [_INPUT_PARAMETERS_NOTE] if spec is not None else []
        parts = [
            self.__preamble,
            *(self.__bases[b] for b in agent.bases),
            *note,
            system_prompt,
        ]
        system_prompt = "\n\n".join(parts)
        return replace(agent, tools=effective_tools, system_prompt=system_prompt)

    def get(self, name: str, autonomous: bool = False) -> SubAgent:
        """Return the subagent for ``name``, rendered for the requested mode.

        Args:
            name: Subagent name (e.g. ``'narrative_author'``).
            autonomous: When ``True``, tools whose ``ToolSpec.autonomous_mode``
                is ``unavailable`` are excluded from the agent's tool set.

        Returns:
            SubAgent: The matching subagent definition.

        Raises:
            AgentLoadError: No agent file found for this name.
        """
        if name not in self.__agents:
            raise AgentLoadError(
                f"No agent file for {name!r}. Expected: subagents/subagent_{name}.md "
                f"or subagents/agent_{name}.md"
            )
        return self.__finalize(self.__agents[name], autonomous)

    def allowed_subagents(self, name: str) -> frozenset[str]:
        """Return the set of sub-agent names *name* is permitted to spawn.

        Read straight from the agent's frontmatter ``subagents:`` allow-list (no
        prompt rendering). Empty when the agent declares none — the default, so
        no agent can spawn sub-agents unless it explicitly opts in. The engine
        consults this to gate every ``run_subagent`` /
        ``run_author_critic_iteration`` call, for *whichever* agent makes it.

        Raises:
            AgentLoadError: No agent file found for this name.
        """
        if name not in self.__agents:
            raise AgentLoadError(
                f"No agent file for {name!r}. Expected: subagents/subagent_{name}.md "
                f"or subagents/agent_{name}.md"
            )
        return self.__agents[name].subagents

    def run_subagent_specs(self, caller: str) -> list[ToolSpec]:
        """Build the ``run_subagent_<name>`` tools *caller* may invoke.

        One tool per entry in *caller*'s ``subagents:`` allow-list that is both
        **invocable** (not a ``role: critic`` — those are spawned by the engine
        inside their author's loop) and **schema-bearing** (has a
        :class:`SubAgentSpec`). Each declares that sub-agent's own
        ``input_schema`` inline, so the caller sees a concrete, typed task shape
        per sub-agent instead of one opaque ``task_input`` object it has to
        reconstruct from prose.

        A sub-agent that declares a ``critic:`` gets the *loop* contract: its
        tool takes an optional ``max_rounds`` and its declared output is the
        agent's own output schema plus a ``review`` block (see
        :func:`_review_output_schema`), because one call runs the whole
        author→critic loop.

        Returns them in allow-list order; an empty list when *caller* declares
        no sub-agents (the default for every agent that isn't an entry agent).

        Raises:
            AgentLoadError: No agent file for *caller*, or an allow-list entry
                has no agent file of its own.
        """
        if caller not in self.__agents:
            raise AgentLoadError(
                f"No agent file for {caller!r}. Expected: subagents/subagent_{caller}.md "
                f"or subagents/agent_{caller}.md"
            )
        specs: list[ToolSpec] = []
        for name in self.__agents[caller].subagent_order:
            agent = self.__agents.get(name)
            if agent is None:
                raise AgentLoadError(
                    f"{self.__agents[caller].source_path}: subagents entry {name!r} has "
                    f"no subagent_{name}.md in the registry"
                )
            spec = SUBAGENT_SPECS_BY_NAME.get(name)
            if spec is None or agent.is_critic:
                continue
            specs.append(
                build_run_subagent_spec(
                    subagent_name=name,
                    display_name=agent.display_name,
                    description=spec.description,
                    input_schema=spec.input_schema,
                    output_schema=(
                        _review_output_schema(spec.output_schema, agent.critic)
                        if agent.critic
                        else spec.output_schema
                    ),
                    critic_name=agent.critic,
                )
            )
        return specs

    def return_result_specs(self, name: str) -> list[ToolSpec]:
        """Build the ``return_result`` tool *name* sees, bound to its output schema.

        A one-element list (so it drops straight into
        :func:`~kodo.tools.tools_for_agent`'s replacement map), or empty for an
        agent with no :class:`SubAgentSpec` — the entry agents, which never
        return a result to anyone.
        """
        spec = SUBAGENT_SPECS_BY_NAME.get(name)
        if spec is None:
            return []
        return [build_return_result_spec(spec.output_schema)]

    def spec_for(self, name: str) -> SubAgentSpec | None:
        """Return the :class:`SubAgentSpec` for *name*, or ``None`` if it has none.

        Entry agents (guide/problem_solver) have no spec; everything else does.
        The engine uses the spec's ``output_schema`` to validate the agent's
        ``return_result`` payload.
        """
        return SUBAGENT_SPECS_BY_NAME.get(name)

    def all_agents(self) -> list[SubAgent]:
        """Return all loaded subagents (interactive-mode render) in name order."""
        return [
            self.__finalize(agent, False)
            for agent in sorted(self.__agents.values(), key=lambda a: a.name)
        ]
