"""Subagent registry — ``name -> SubAgent`` lookup.

Loads all ``.md`` files from the subagents package directory at construction time.

Shared prompt text
==================

There is **one** way to reuse prompt text across agents, and it is textual
inclusion: a ``{SHARED:<name>}`` token anywhere in an agent's body is replaced
with the contents of ``shared_<name>.md`` from this package. That is the whole
rule. It replaced three separate mechanisms that all did the same thing by
different means — ``bases:`` frontmatter (prepended), *preambles* (appended,
two of them gated on a tool grant and a frontmatter flag), and a Python
constant substituted into a bespoke placeholder — none of which an agent author
could see the effect of without reading the registry.

Consequences worth knowing:

- **Placement is the agent's**, so an author decides where each block reads
  best rather than accepting a fixed prepend/append slot. The convention the
  agents follow is: shared *contracts* (``escalation``, ``dependencies``) sit
  where the body refers to them, ``task_input`` sits right after the opening
  identity paragraph, and the rule blocks close the file in the order
  ``editing`` (if any) → ``callouts`` (if any) → ``working_rules`` →
  ``security``.
- **Inclusion is the declaration.** ``SubAgent.bases`` and a ``callouts:``
  frontmatter flag both disappeared: an agent that wants the callout rules
  writes ``{SHARED:callouts}``, and that *is* the opt-in. No registry-side gate
  decides for it.
- **Nothing is auto-appended**, so an agent that forgets ``{SHARED:security}``
  would ship without injection resistance. Three checks make that impossible:
  :meth:`AgentRegistry.__validate_shared` raises at construction if a prompt is
  missing a block in :data:`_REQUIRED_SHARED` or (when it holds a
  ``modifies_files`` tool) ``editing``; ``test_agents.py`` scans every
  ``agent_*.md`` / ``subagent_*.md`` for the same thing so it fails at build
  time rather than at runtime; and an unknown ``{SHARED:name}`` is itself a
  load error, so a typo cannot silently render nothing.
- Substitution is a **single pass**. Shared files may not contain tokens of
  their own (checked at load), which makes one pass provably sufficient and
  rules out include cycles.

The rule blocks close a prompt rather than opening it, so every prompt starts
on the agent's identity and role and ends with the rules that bind them —
``security`` last, being both highest-precedence and the block a long prompt
can least afford to have skimmed. Because the system prompt is rebuilt fresh on
every turn, all of it is present regardless of context compaction (compaction
only rewrites the conversation history, never the system prompt).

Two shared blocks are deliberately *not* included by every agent, because
sending them everywhere was counterproductive rather than merely wasteful:
``editing`` names ``edit_file``/``create_file`` and reaches only agents granted
such a tool (roughly half of them never write a file, and telling a critic how
to keep a diff minimal contradicts the "use only your granted tools" rule in
``security``); ``callouts`` reaches only the two entry agents, since a
sub-agent's text is buried in a collapsed subsession block whose open/close
callouts the *client* draws.

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
  sub-agent's ``input_schema`` inline and carrying that sub-agent's own
  ``## Purpose`` body as its description. A sub-agent that declares a
  ``critic:`` gets the loop contract (an optional ``max_rounds``; a ``review``
  block in its output), because one such call runs the entire author→critic
  loop.
- :meth:`AgentRegistry.return_result_specs` mints the ``return_result`` an agent
  sees, with ``result`` bound to that agent's own ``output_schema``.

Both are fed to :func:`kodo.tools.tools_for_agent` as replacements for the
catalog's canonical ``run_subagent`` / ``return_result`` entries; see
:func:`kodo.runtime.agent_tool_specs`, which is the single place that assembles
them.

**There is no sub-agent roster.** A caller used to embed a
``{PLACEHOLDER:SUBAGENTS}`` token that expanded into an intro, a
tool/agent/review/kind table, and every listed sub-agent's ``## Purpose``
paragraph. That was a prompt-side description of tools — precisely what
doc/TOOLS.md §7 forbids everywhere else — and it duplicated a second, terser
description each sub-agent also carried on its ``SubAgentSpec``. Both collapsed
into the generated tool: ``## Purpose`` *is* the ``run_subagent_<name>``
description now, joined by the sentence saying whether the sub-agent is a
pipeline stage or an on-demand specialist (from ``standalone:``) and, for an
author, the review-loop contract (from ``critic:``). ``SubAgentSpec`` no longer
has a ``description`` field at all. A **critic** is not invocable and gets no
tool of its own; what a caller needs to know about it lives in its author's
description, and its own ``## Purpose`` stays in its own prompt.

A schema-bearing agent's own system prompt never contains its input or output
schema either — no ``## Your Task Contract`` block, no JSON dump. It only gets
``{SHARED:task_input}``, a short fixed pointer to where its real task lands: the
first user turn, rendered per call (with real values, per-field descriptions,
and the `return_result` reminder) by
:func:`kodo.runtime._engine._subagents._render_task_input`, never here.
Including that block is what *opts an agent into* the note, which fixed a
standing inaccuracy: it used to be injected into every schema-bearing agent,
including ``compactor``, which the engine seeds with a bare transcript message
rather than a rendered task turn. An agent with no ``SubAgentSpec`` including
it is a load-time error.

Installed skills
================

One further substitution shares the same philosophy: ``{SKILLS}`` expands to
the catalog of skills installed under ``~/.kodo/skills`` (doc/SKILLS.md). It
differs from ``{SHARED:…}`` in exactly one way — its content comes from the
live filesystem instead of a file in this package, so it is expanded per
:meth:`AgentRegistry.get` rather than once at construction, and a skill the
user drops in mid-session is advertised on the next turn.

The **tool grant is the opt-in**: an agent gets skills by declaring
``use_skill`` in its ``tools:`` frontmatter, and :meth:`AgentRegistry.
__validate_skills` then *requires* the ``{SKILLS}`` token in its body (and
rejects the token without the grant). So there is still no engine-side "which
agents get skills" rule to keep in sync — an agent declares it, the same way it
declares everything else it can do.

Raises :class:`~._loader.AgentLoadError` on duplicate names, missing entries, a
tool with no matching :class:`~kodo.toolspecs.ToolSpec`, a ``critic:`` that does
not resolve to an agent declaring ``role: critic``, an unknown/missing/nested
``{SHARED:…}``, a sub-agent with no ``## Purpose``, or ``{SHARED:task_input}``
in an agent with no spec, or a ``use_skill``/``{SKILLS}`` half-declaration.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from kodo.project import kodo_skills_dir
from kodo.skills import SkillStore, render_catalog
from kodo.toolspecs import (
    ALL_TOOLS,
    GET_FINDINGS,
    RETURN_RESULT,
    RUN_SUBAGENT,
    USE_SKILL,
    ToolSpec,
    build_return_result_spec,
    build_run_subagent_spec,
)

from ._loader import AgentLoadError, SubAgent, load_agent
from ._subagentspec import SubAgentSpec
from .specs import ALL_SUBAGENTS

# ``shared_<name>.md`` in this package ⇄ ``{SHARED:<name>}`` in an agent body.
# The lowercase-only name pattern is deliberate: it matches the filenames, so a
# stray ``{SHARED:Security}`` fails the unknown-name check loudly instead of
# being silently normalized into something that happens to work.
SHARED_FILE_PREFIX = "shared_"
_SHARED_TOKEN_RE = re.compile(r"\{SHARED:([a-z0-9_]+)\}")


def shared_token(name: str) -> str:
    """Return the token that includes ``shared_<name>.md`` — ``{SHARED:<name>}``.

    One place builds this string, so the tests and the error messages cannot
    drift from what :data:`_SHARED_TOKEN_RE` actually matches.
    """
    return f"{{SHARED:{name}}}"


# Shared blocks every agent's prompt must include, checked at construction.
# Nothing is auto-appended any more (inclusion is the agent's own declaration),
# so without this an agent could ship with no injection resistance and no
# working rules, and the only symptom would be bad model behavior. Kept minimal
# on purpose: a block belongs here only if *no* agent could correctly omit it.
_REQUIRED_SHARED: tuple[str, ...] = ("working_rules", "security")

# The shared block an agent must include when it can change files on disk, and
# the tools that make it so — read off the live specs rather than named here.
# The pairing is what keeps ``ToolSpec.modifies_files`` honest: grant a
# file-touching tool without the discipline and construction fails.
_EDITING_SHARED = "editing"
_FILE_MODIFYING_TOOLS: frozenset[str] = frozenset(t.name for t in ALL_TOOLS if t.modifies_files)

# The shared block that tells a sub-agent where its real task lands. Valid only
# in an agent the engine seeds from a structured ``task_input`` (i.e. one with a
# ``SubAgentSpec``); see the module docstring.
_TASK_INPUT_SHARED = "task_input"

# The findings protocol (doc/FINDINGS.md §7), split in two because the halves
# have opposite obligations: an author reads the backlog and fixes it, a critic
# reads it and is the only one that may close anything. ``get_findings`` is the
# grant that *is* the opt-in, and the pairing is validated in both directions —
# a grant with neither block leaves an agent holding a tool it was never told to
# use, and a block without the grant tells it to call a tool it does not have.
_FINDINGS_SHARED: tuple[str, ...] = ("findings_author", "findings_critic")
_GET_FINDINGS_TOOL = GET_FINDINGS.name

# The terminal tool every schema-bearing sub-agent is auto-granted (so it can
# return its result against its declared output schema). Granted in the registry
# rather than per-frontmatter so it can never drift from a spec's existence.
# Replaced per-agent by ``return_result_specs`` before it reaches the LLM.
_RETURN_RESULT_TOOL = RETURN_RESULT.name

# ``{SKILLS}`` ⇄ the installed-skills catalog (doc/SKILLS.md §3). A second
# substitution alongside ``{SHARED:<name>}``, deliberately shaped like it —
# placement is the agent author's, and nothing is auto-appended — but expanded
# from the *live* ``~/.kodo/skills`` directory rather than a file in this
# package, so a skill installed mid-session shows up on the next turn. It takes
# no name, since there is only ever one catalog.
SKILLS_TOKEN = "{SKILLS}"

# The tool grant that *is* the opt-in: an agent gets skills by declaring
# ``use_skill``. ``__validate_skills`` binds the two together in both
# directions, so the grant and the catalog can never appear without each other
# (a tool with nothing to name, or a catalog the agent cannot act on).
_USE_SKILL_TOOL = USE_SKILL.name

# The catalog name an agent declares in its ``tools:`` frontmatter to opt into
# spawning sub-agents. It is never offered as-is: ``run_subagent_specs`` expands
# it into one variant per sub-agent the agent may invoke.
_RUN_SUBAGENT_TOOL = RUN_SUBAGENT.name

# Every tool spec, keyed by tool name (names are unique in the catalog).
_SPECS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in ALL_TOOLS}

# Every sub-agent's typed interface, keyed by agent name. An agent that has an
# entry here is "schema-bearing": it is auto-granted ``return_result`` and may
# include ``{SHARED:task_input}``. Its schemas reach a *caller* as real JSON
# Schema on the generated ``run_subagent_<name>`` tool, and reach the agent
# *itself* as the real values under ``## Input Parameters`` at the bottom of its
# first message (see ``kodo.runtime._engine._subagents._render_task_input``) —
# no schema is ever shown in a system prompt. Entry agents (guide/problem_solver
# /judge) have no spec and are left untouched.
SUBAGENT_SPECS_BY_NAME: dict[str, SubAgentSpec] = {s.name: s for s in ALL_SUBAGENTS}


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
            caller can attribute the findings).

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
                    "The file's status after the last round: 'accepted', "
                    "'pending_acceptance', 'needs_revision', or 'pending_review'."
                ),
            },
            "outcome": {
                "type": "string",
                "description": (
                    "Why the loop stopped: 'accepted' (the critic accepted and any "
                    "user sign-off landed), 'escalated' (the author returned a "
                    "blocker in `reason` it cannot resolve, so no critic ran and "
                    "no further round was spent — resolve it and re-run), "
                    "'max_rounds' (the budget ran out with findings still "
                    "outstanding), 'not_converging' (a round closed nothing and "
                    "found nothing, so further rounds were judged wasteful), or "
                    "'not_reviewed' (the author reported no file to review). "
                    "Anything but 'accepted' needs your decision."
                ),
            },
            "rounds": {
                "type": "integer",
                "description": "How many author→critic rounds ran.",
            },
            "outstanding": {
                "type": "integer",
                "description": (
                    "How many findings are still outstanding against the file; 0 when "
                    "accepted. The findings themselves live in the author/critic "
                    "backlog, which is theirs, not yours — you act on the count and "
                    "the outcome."
                ),
            },
        },
        "required": ["status", "outcome", "rounds", "outstanding"],
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

    Every agent returned by :meth:`get` has every ``{SHARED:<name>}`` token in
    its body replaced with ``shared_<name>.md``'s contents, and its tool set
    filtered for the requested mode.

    Args:
        agents_dir: Directory containing the ``shared_*.md`` blocks, the
            ``subagent_*.md`` files, and the ``agent_*.md`` entry-agent files
            (``guide``, ``problem_solver``, ``judge``).

    Raises:
        AgentLoadError: a shared file is empty or itself contains a
            ``{SHARED:…}`` token, an agent references a tool with no matching
            :class:`~kodo.toolspecs.ToolSpec`, an agent includes an unknown
            shared block, omits a required one, holds a file-modifying tool
            without ``{SHARED:editing}``, or includes ``{SHARED:task_input}``
            without being schema-bearing.
    """

    __slots__ = ("__agents", "__shared", "__skills")

    def __init__(self, agents_dir: Path, skills_dir: Path | None = None) -> None:
        # The skills root is injectable purely so tests (and the validator's
        # isolated home) can point at a temp directory; every production caller
        # leaves it None and gets ``~/.kodo/skills``. The store itself holds no
        # cache — it re-scans on each read — so binding it once here still sees
        # skills installed after the server started.
        self.__skills = SkillStore(skills_dir if skills_dir is not None else kodo_skills_dir())
        # Shared blocks (``shared_<name>.md``), keyed by ``<name>``. Never
        # globbed as agents (those globs are ``subagent_*.md`` / ``agent_*.md``),
        # so a shared file can never register as a spawnable agent.
        self.__shared: dict[str, str] = {}
        for path in sorted(agents_dir.glob(f"{SHARED_FILE_PREFIX}*.md")):
            name = path.stem[len(SHARED_FILE_PREFIX) :]
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise AgentLoadError(f"{path}: shared file is empty")
            # Substitution is a single pass, which is only provably sufficient
            # while shared files carry no tokens themselves. Rejecting them here
            # is also what rules out include cycles, without a visited-set walk.
            nested = _SHARED_TOKEN_RE.search(text)
            if nested:
                raise AgentLoadError(
                    f"{path}: shared files must not include other shared files "
                    f"(found {nested.group(0)}); inclusion is a single pass"
                )
            self.__shared[name] = text
        self.__agents: dict[str, SubAgent] = {}
        # Sub-agents (``subagent_*.md``) and the user-facing entry agents
        # (``agent_*.md`` — ``guide``, ``problem_solver``, ``judge``) share one
        # registry, looked up by name regardless of which prefix they use.
        agent_paths = sorted(agents_dir.glob("subagent_*.md")) + sorted(
            agents_dir.glob("agent_*.md")
        )
        for path in agent_paths:
            agent = load_agent(path)
            # Validate every declared tool resolves now, at load time, so a bad
            # frontmatter reference fails fast rather than at first dispatch.
            self.__validate_tools(agent.tools, path)
            self.__validate_shared(agent)
            self.__validate_skills(agent)
            self.__agents[agent.name] = agent
        # Every sub-agent some caller may spawn — the union of all
        # ``subagents:`` allow-lists. Exactly these get a generated
        # ``run_subagent_<name>`` tool, so exactly these need the ``## Purpose``
        # that becomes its description. Derived rather than assumed: the
        # engine-driven agents (``compactor``, ``web_search``,
        # ``toolchain_depsmgr``) sit in no allow-list and correctly have none.
        invocable = {name for agent in self.__agents.values() for name in agent.subagent_order}
        # Second pass — every agent is loaded now, so cross-agent references can
        # be validated. Fail-fast at construction, same as the checks above: a
        # declared ``critic:`` must resolve to a real agent that actually
        # declares ``role: critic`` (otherwise the engine would spawn something
        # that never records a verdict).
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
            # An invocable sub-agent's ``## Purpose`` is its generated tool's
            # description, so a missing one would ship a tool the caller cannot
            # route to. Critics are exempt (the engine spawns them inside their
            # author's loop, so they get no tool), as is any agent no caller
            # lists — the engine-driven ones describe themselves to nobody.
            if agent.name in invocable and not agent.is_critic and not agent.purpose:
                raise AgentLoadError(
                    f"{agent.source_path}: no '## Purpose' section — it is the "
                    f"description of this sub-agent's run_subagent_{agent.name} tool"
                )
            for sub in agent.subagent_order:
                if sub not in self.__agents:
                    raise AgentLoadError(
                        f"{agent.source_path}: subagents entry {sub!r} has no "
                        f"subagent_{sub}.md in the registry"
                    )

    @staticmethod
    def __validate_tools(agent_tools: frozenset[str], path: Path) -> None:
        """Fail fast when an agent's frontmatter names an unknown tool.

        Run at load time (not first render) so a typo in ``tools:`` surfaces when
        the registry is built rather than mid-session.
        """
        for name in sorted(agent_tools):
            if name not in _SPECS_BY_NAME:
                raise AgentLoadError(f"{path}: tool {name!r} has no ToolSpec in kodo.toolspecs")

    def __validate_shared(self, agent: SubAgent) -> None:
        """Check *agent*'s ``{SHARED:…}`` inclusions at construction time.

        Nothing is auto-appended to a prompt any more — inclusion is the agent's
        own declaration — so these checks are the only thing standing
        between a forgotten token and an agent running with no injection
        resistance. ``test_agents.py`` re-runs the same rules over every agent
        file so the failure normally lands at build time; this is the last-resort
        copy for anything that reaches a running server.

        Raises:
            AgentLoadError: an unknown block name, a missing required block, a
                file-modifying tool without the editing discipline, a
                ``get_findings`` grant without exactly one half of the findings
                protocol (or either half without the grant), or a task-input note
                in an agent that is never seeded with one.
        """
        included = {m.group(1) for m in _SHARED_TOKEN_RE.finditer(agent.system_prompt)}

        unknown = sorted(included - self.__shared.keys())
        if unknown:
            raise AgentLoadError(
                f"{agent.source_path}: unknown shared block(s) {unknown} — expected a "
                f"{SHARED_FILE_PREFIX}<name>.md for each; known: {sorted(self.__shared)}"
            )

        missing = [name for name in _REQUIRED_SHARED if name not in included]
        if missing:
            raise AgentLoadError(
                f"{agent.source_path}: every agent prompt must include "
                f"{', '.join(shared_token(n) for n in missing)}"
            )

        # The invariant that keeps ``ToolSpec.modifies_files`` honest: an agent
        # that can change files on disk states the discipline for doing so.
        if agent.tools & _FILE_MODIFYING_TOOLS and _EDITING_SHARED not in included:
            granted = sorted(agent.tools & _FILE_MODIFYING_TOOLS)
            raise AgentLoadError(
                f"{agent.source_path}: grants file-modifying tool(s) {granted}, so its "
                f"prompt must include {shared_token(_EDITING_SHARED)}"
            )

        # The same pairing rule for the findings protocol: exactly one half of
        # it, iff the agent can actually read the backlog.
        findings_blocks = sorted(included.intersection(_FINDINGS_SHARED))
        if _GET_FINDINGS_TOOL in agent.tools and len(findings_blocks) != 1:
            raise AgentLoadError(
                f"{agent.source_path}: grants {_GET_FINDINGS_TOOL}, so its prompt must "
                f"include exactly one of "
                f"{', '.join(shared_token(n) for n in _FINDINGS_SHARED)} — found "
                f"{findings_blocks or 'none'}"
            )
        if findings_blocks and _GET_FINDINGS_TOOL not in agent.tools:
            raise AgentLoadError(
                f"{agent.source_path}: includes {shared_token(findings_blocks[0])} but does "
                f"not grant {_GET_FINDINGS_TOOL}, which that block tells it to call"
            )

        # Guard the direction that is always a bug: an agent with no
        # SubAgentSpec is never seeded from a structured ``task_input``, so the
        # note would promise it a first message that never arrives.
        #
        # The other direction is a legitimate choice, not an omission, which is
        # why ``task_input`` is not in ``_REQUIRED_SHARED``. A schema-bearing
        # agent the engine seeds some *other* way must not carry it either —
        # ``compactor`` is handed a bare "Conversation transcript to compact: …"
        # user message by ``_generate_compaction_summary``, never a rendered
        # ``# Task`` + ``## Input Parameters`` turn — and it documents its real
        # input itself. Requiring it everywhere would force that agent to state
        # something false about its own input.
        if _TASK_INPUT_SHARED in included and agent.name not in SUBAGENT_SPECS_BY_NAME:
            raise AgentLoadError(
                f"{agent.source_path}: {shared_token(_TASK_INPUT_SHARED)} is only valid "
                f"in an agent that declares a SubAgentSpec — this one has none, so it "
                f"is never seeded from a structured task_input"
            )

    @staticmethod
    def __validate_skills(agent: SubAgent) -> None:
        """Bind the ``use_skill`` grant and the ``{SKILLS}`` catalog together.

        Skills reach an agent through two halves that are useless apart: the
        catalog tells the model which skills exist, the tool loads one. Either
        half alone is a silent misconfiguration — a grant with no catalog gives
        the model a tool it can only call with a guessed name, and a catalog
        with no grant advertises skills the agent has no way to open — and
        neither would fail visibly at runtime, so both are load-time errors
        here. Checking it in the registry (rather than only in
        ``test_agents.py``) is what makes the tool grant a *sufficient*
        declaration for an agent author: declare it, and forgetting the token
        is caught for you.

        Raises:
            AgentLoadError: the agent declares one half without the other.
        """
        has_token = SKILLS_TOKEN in agent.system_prompt
        has_tool = _USE_SKILL_TOOL in agent.tools
        if has_tool and not has_token:
            raise AgentLoadError(
                f"{agent.source_path}: grants {_USE_SKILL_TOOL!r}, so its prompt must "
                f"include {SKILLS_TOKEN} where the installed-skills catalog should go"
            )
        if has_token and not has_tool:
            raise AgentLoadError(
                f"{agent.source_path}: includes {SKILLS_TOKEN} without granting "
                f"{_USE_SKILL_TOOL!r} — the agent would be shown skills it cannot load"
            )

    def __finalize(self, agent: SubAgent, autonomous: bool) -> SubAgent:
        """Render *agent* for the requested mode.

        Filters autonomous-disabled tools out of the effective tool set and
        expands every ``{SHARED:<name>}`` token in the body. One pass suffices:
        shared files are rejected at construction if they contain tokens of
        their own, and every name is known to resolve (also checked there), so
        the substitution here cannot fail.
        """
        spec = SUBAGENT_SPECS_BY_NAME.get(agent.name)
        effective_tools = agent.tools
        # Schema-bearing sub-agents are auto-granted the terminal return tool so
        # they can return their result against their declared output schema.
        if spec is not None:
            effective_tools = effective_tools | {_RETURN_RESULT_TOOL}
        if autonomous and _AUTONOMOUS_DISABLED:
            effective_tools = frozenset(t for t in effective_tools if t not in _AUTONOMOUS_DISABLED)
        system_prompt = _SHARED_TOKEN_RE.sub(
            lambda m: self.__shared[m.group(1)], agent.system_prompt
        )
        # Guarded on the token so the ~30 agents without skills never pay for a
        # directory scan. The ones that do pay it once per turn, which is the
        # point: the catalog reflects what is installed *now*, not what was
        # installed when the server booted.
        if SKILLS_TOKEN in system_prompt:
            system_prompt = system_prompt.replace(
                SKILLS_TOKEN, render_catalog(self.__skills.usable())
            )
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

        The **description is the callee's own ``## Purpose`` body**, which is
        why that section is caller-agnostic and third-person: it is written for
        whoever is deciding whether to delegate. It is the only description
        there is — the roster that used to carry a second copy is gone, and
        :class:`SubAgentSpec` no longer has a ``description`` field.
        :func:`~kodo.toolspecs.build_run_subagent_spec` appends the two facts
        the roster's table columns used to carry: whether the sub-agent is a
        pipeline stage or an on-demand specialist (``standalone:``) and, for an
        author, the review-loop contract (``critic:``).

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
                    description=agent.purpose,
                    input_schema=spec.input_schema,
                    output_schema=(
                        _review_output_schema(spec.output_schema, agent.critic)
                        if agent.critic
                        else spec.output_schema
                    ),
                    critic_name=agent.critic,
                    standalone=agent.standalone,
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
