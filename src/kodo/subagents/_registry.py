"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.
Two mandatory preambles — the **security** preamble (``preamble_security.md``)
and the **performance** preamble (``preamble_performance.md``) — are prepended,
in that order, to every subagent's system prompt. Because the system prompt is
rebuilt fresh on every turn, both preambles are always present regardless of
context compaction (compaction only rewrites the conversation history, never the
system prompt).

Each subagent's ``## Tools`` section is rendered lazily by replacing the
``{PLACEHOLDER:TOOLS}`` token with one block per tool listed in its frontmatter
``tools:``. Every part of that block — the heading (``external_name``), the
``Autonomous mode`` line, and the ``When to use`` bullets — comes from that
tool's :class:`~kodo.toolspecs.ToolSpec`; no separate prompt file is involved.
``when_to_use`` bullets are written generically (they describe a situation, not
which agent is in it), since the same spec may be rendered into multiple
agents' prompts.

Rendering is performed lazily by :meth:`AgentRegistry.get` so it can honour the
current ``autonomous`` flag: tools whose :class:`~kodo.toolspecs.ToolSpec`
marks ``autonomous_mode`` as ``unavailable`` are excluded — from both the
rendered ``## Tools`` section and the returned :attr:`SubAgent.tools` set —
when ``autonomous=True``.

A caller agent (one with a ``subagents:`` allow-list) may also embed a
``{PLACEHOLDER:SUBAGENTS}`` token. It is replaced with a **sub-agent roster**:
a short paragraph distinguishing **workflow** sub-agents (ordered pipeline,
depend on upstream artifacts) from **standalone** ones (on-demand specialists
with no upstream dependency), then a table of the invocable sub-agents
(author/critic pairs collapsed into one ``run_author_critic_iteration`` row,
solos as ``run_subagent`` rows, with a ``Kind`` column marking workflow vs
standalone) followed by each listed sub-agent's caller-agnostic ``## Purpose``
paragraph — in the caller's allow-list order. The roster is built from the
*callee* agents' frontmatter (``solo``/``critic``/``standalone``) and
``## Purpose`` body, so the description lives once with each sub-agent and is
reused by every caller. See :meth:`AgentRegistry.render_subagents_section`.

Raises :class:`~._loader.AgentLoadError` on duplicate names, missing entries,
or a tool with no matching :class:`~kodo.toolspecs.ToolSpec`.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from kodo.toolspecs import ALL_TOOLS, ToolSpec, augment_output_schema

from ._loader import AgentLoadError, SubAgent, load_agent
from ._subagentspec import SubAgentSpec
from .specs import ALL_SUBAGENTS

_SECURITY_PREAMBLE_FILENAME = "preamble_security.md"
_PERFORMANCE_PREAMBLE_FILENAME = "preamble_performance.md"
_TOOLS_PLACEHOLDER = "{PLACEHOLDER:TOOLS}"
_SUBAGENTS_PLACEHOLDER = "{PLACEHOLDER:SUBAGENTS}"

# The terminal tool every schema-bearing sub-agent is auto-granted (so it can
# return its result against its declared output schema). Granted in the registry
# rather than per-frontmatter so it can never drift from a spec's existence.
_RETURN_RESULT_TOOL = "return_result"

# Intro paragraph that precedes the roster table. Drawn from the callees'
# ``standalone`` frontmatter, it tells the caller how to read the ``Kind``
# column: **workflow** agents advance an ordered pipeline and depend on upstream
# artifacts; **standalone** agents are on-demand specialists with no such
# dependency.
_SUBAGENTS_INTRO = (
    "The sub-agents below come in two kinds, marked in the **Kind** column. "
    "**Workflow** sub-agents advance a pre-determined pipeline: each one consumes "
    "the artifacts produced by the stage before it, so they run in a fixed order "
    "and depend on upstream output. **Standalone** sub-agents are specialists you "
    "invoke whenever the need arises; they sit outside the pipeline and do not "
    "depend on the outcome of any other agent."
)

# Every tool spec, keyed by tool name (names are unique in the catalog).
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}

# Every sub-agent's typed interface, keyed by agent name. An agent that has an
# entry here is "schema-bearing": it is auto-granted ``return_result``, gets a
# ``## Your Task Contract`` section rendered into its own prompt, and its schemas
# appear in any caller's roster. Entry agents (guide/problem_solver) have no
# spec and are left untouched.
SUBAGENT_SPECS_BY_NAME: dict[str, SubAgentSpec] = {s.name: s for s in ALL_SUBAGENTS}

# Tools withheld entirely in autonomous mode.
_AUTONOMOUS_DISABLED: frozenset[str] = frozenset(
    t.name for t in ALL_TOOLS if t.autonomous_mode and "unavailable" in t.autonomous_mode.lower()
)


class AgentRegistry:
    """Index of all loaded subagents, looked up by name.

    Every agent returned by :meth:`get` has the security and performance
    preambles prepended (in that order) and its ``## Tools`` placeholder
    replaced with descriptions for its allowed tools, filtered for the
    requested mode.

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
            # frontmatter reference fails fast rather than at first render.
            self.__render_tools_section(agent.tools, path)
            # Validate every declared base exists, for the same fail-fast reason.
            for base in agent.bases:
                if base not in self.__bases:
                    raise AgentLoadError(
                        f"{path}: base {base!r} has no base_{base}.md in {agents_dir}"
                    )
            self.__agents[agent.name] = agent
        # Second pass — every agent is loaded now, so cross-agent references in a
        # ``{PLACEHOLDER:SUBAGENTS}`` roster can be validated (each listed
        # sub-agent must exist and carry a ``## Purpose`` section). Fail-fast at
        # construction, same as the tool/base checks above.
        for agent in self.__agents.values():
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
    def __render_tools_section(agent_tools: frozenset[str], path: Path) -> str:
        blocks: list[str] = []
        for name in sorted(agent_tools):
            spec = _SPECS_BY_NAME.get(name)
            if spec is None:
                raise AgentLoadError(f"{path}: tool {name!r} has no ToolSpec in kodo.toolspecs")
            lines = []
            if spec.autonomous_mode:
                lines.append(f"- **Autonomous mode:** {spec.autonomous_mode}")
            lines.append(f"- **Security impact:** {spec.security_impact.label}")
            lines.append("- **When to use:**")
            lines.extend(f"  - {bullet}" for bullet in spec.when_to_use)
            # Output schema is visible to the agent (augmented in-flight with the
            # engine-owned `schema_compliance` flag the agent should consult).
            output_schema = json.dumps(augment_output_schema(spec.output_schema), indent=2)
            lines.append("- **Output schema:**")
            lines.append(f"  ```json\n{output_schema}\n  ```")
            blocks.append(f"### {spec.external_name} (`{name}`)\n\n" + "\n".join(lines))
        return "\n\n".join(blocks)

    @staticmethod
    def __render_contract_section(spec: SubAgentSpec) -> str:
        """Render an agent's own ``## Your Task Contract`` from its spec.

        Shows the structured task the agent receives (``input_schema``) and the
        result it must hand to ``return_result`` (``output_schema``, augmented
        with the engine-owned ``schema_compliance`` field, exactly as a tool's
        output schema is shown).
        """
        input_json = json.dumps(spec.input_schema, indent=2)
        output_json = json.dumps(augment_output_schema(spec.output_schema), indent=2)
        return (
            "## Your Task Contract\n\n"
            "You are invoked with a structured task matching this **input schema**:\n\n"
            f"```json\n{input_json}\n```\n\n"
            "When you finish, call `return_result` exactly once with a `result` object "
            "matching this **output schema**. The engine validates it; a "
            "`schema_compliance: false` in the acknowledgement means it had to repair "
            "your payload (missing fields backfilled, undeclared fields dropped):\n\n"
            f"```json\n{output_json}\n```"
        )

    @staticmethod
    def __render_subagent_schemas(spec: SubAgentSpec) -> str:
        """Render a callee's input/output schema blocks for a caller's roster."""
        input_json = json.dumps(spec.input_schema, indent=2)
        output_json = json.dumps(augment_output_schema(spec.output_schema), indent=2)
        return (
            "**Input schema** (pass as `task_input`):\n\n"
            f"```json\n{input_json}\n```\n\n"
            "**Output schema** (what it returns):\n\n"
            f"```json\n{output_json}\n```"
        )

    def __render_subagents_section(self, caller: SubAgent) -> str:
        """Render the sub-agent roster that fills *caller*'s ``{PLACEHOLDER:SUBAGENTS}``.

        Three parts, in this order:

        1. An **intro paragraph** (:data:`_SUBAGENTS_INTRO`) explaining how to
           read the ``Kind`` column — workflow (ordered, upstream-dependent) vs
           standalone (on-demand specialist).
        2. A **roster table** (modelled on the guide's hand-written *Sub-Agent
           Names* table) with one row per *invocable* sub-agent in the caller's
           ``subagents:`` allow-list order. An agent that declares a ``critic:``
           is an **author** → a ``run_author_critic_iteration`` row naming the
           critic; an agent that declares ``solo: true`` → a ``run_subagent`` row;
           a **pure critic** (neither) is *absorbed* into its author's row and
           gets no row of its own. The ``Kind`` column reads ``standalone`` when
           the callee declares ``standalone: true``, else ``workflow``.
        3. A **purpose paragraph** per sub-agent in the allow-list — authors,
           critics, and solos alike — so an author and its critic read adjacent.
           Each is the caller-agnostic ``## Purpose`` body from that agent's file.

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
            if agent.critic:
                tool, critic_col = "run_author_critic_iteration", f"`{agent.critic}`"
            elif agent.solo:
                tool, critic_col = "run_subagent", "—"
            else:
                continue  # pure critic — shown in its author's row, not its own
            kind = "standalone" if agent.standalone else "workflow"
            rows.append(f"| `{tool}` | `{sub}` | {critic_col} | {kind} |")
        table = (
            "| Tool | `name` / `author_name` | `critic_name` | Kind |\n"
            "| ---- | ---------------------- | ------------- | ---- |\n" + "\n".join(rows)
        )

        paras: list[str] = []
        for sub in order:
            agent = self.__agents[sub]
            para = f"### {agent.display_name} (`{sub}`)\n\n{agent.purpose}"
            spec = SUBAGENT_SPECS_BY_NAME.get(sub)
            if spec is not None:
                para += "\n\n" + self.__render_subagent_schemas(spec)
            paras.append(para)
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

        Filters autonomous-disabled tools from both the effective tool set and
        the rendered ``## Tools`` section, then prepends the shared base snippets
        (if any) and the global preamble.
        """
        spec = SUBAGENT_SPECS_BY_NAME.get(agent.name)
        effective_tools = agent.tools
        # Schema-bearing sub-agents are auto-granted the terminal return tool so
        # they can return their result against their declared output schema.
        if spec is not None:
            effective_tools = effective_tools | {_RETURN_RESULT_TOOL}
        if autonomous and _AUTONOMOUS_DISABLED:
            effective_tools = frozenset(t for t in effective_tools if t not in _AUTONOMOUS_DISABLED)
        tools_section = self.__render_tools_section(effective_tools, agent.source_path)
        system_prompt = agent.system_prompt.replace(_TOOLS_PLACEHOLDER, tools_section)
        if _SUBAGENTS_PLACEHOLDER in system_prompt:
            system_prompt = system_prompt.replace(
                _SUBAGENTS_PLACEHOLDER, self.__render_subagents_section(agent)
            )
        # Order of precedence: global preamble (security + performance) first,
        # then any shared base contract, then the agent's own typed task
        # contract (when it has a spec), then the agent's own body (which may
        # specialize the base). Bases are validated to exist at load time.
        contract = [self.__render_contract_section(spec)] if spec is not None else []
        parts = [
            self.__preamble,
            *(self.__bases[b] for b in agent.bases),
            *contract,
            system_prompt,
        ]
        system_prompt = "\n\n".join(parts)
        return replace(agent, tools=effective_tools, system_prompt=system_prompt)

    def get(self, name: str, autonomous: bool = False) -> SubAgent:
        """Return the subagent for ``name``, rendered for the requested mode.

        Args:
            name: Subagent name (e.g. ``'narrative_author'``).
            autonomous: When ``True``, tools whose ``ToolSpec.autonomous_mode``
                is ``unavailable`` are excluded from the agent's tool set and
                its rendered ``## Tools`` section.

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
