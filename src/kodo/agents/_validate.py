"""One validation report per agent — doc/AGENT_BUILDER.md §5.1 and §5.2.

A report joins the two checks an agent goes through:

- the **load check** — today's registry rules, unchanged: the strict loaders,
  the cross-agent pass and the demotion cascade. A user agent that failed them
  is a :class:`~kodo.agents.BrokenAgent` row, and the report carries the row's
  reason, which is otherwise only shown as the row's text;
- the **lint** (:func:`~kodo.agents.lint_prompt`), warnings only, run over the
  prompt file whenever it still parses — so a bundle demoted for its config
  still gets its prompt checked.

Built-ins and user agents are validated alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kodo.toolspecs import ALL_TOOLS

from ._install import KIND_AGENT, KIND_SUBAGENT
from ._lint import LintContext, LintFinding, lint_prompt
from ._loader import AgentLoadError, SubAgent, load_agent
from ._registry import AgentRegistry
from ._userstore import BrokenAgent

__all__ = ["AgentReport", "AgentValidator"]


@dataclass(frozen=True)
class AgentReport:
    """Whether one agent loads, and where its prompt departs from the rules.

    Attributes:
        name: The agent's name.
        kind: :data:`~kodo.agents.KIND_AGENT` or :data:`~kodo.agents.KIND_SUBAGENT`.
        load_error: Why the agent does not load, or ``""`` when it does.
        findings: The lint's warnings, in rule order, then line order.
        path: The prompt file the findings' line numbers refer to, or ``None``
            when no prompt file could be found.
    """

    name: str
    kind: str
    load_error: str = ""
    findings: tuple[LintFinding, ...] = ()
    path: Path | None = None

    @property
    def loads(self) -> bool:
        """Whether the agent passed the load check."""
        return not self.load_error

    def as_payload(self) -> dict[str, object]:
        """The report as JSON-ready data — the ``--validate-agent --json`` and wire shape.

        Returns:
            dict[str, object]: ``{name, kind, load: "ok" | "error", error,
            path, findings: [{rule, severity, line, message}]}``, where ``line``
            is ``null`` for a finding about something absent.
        """
        return {
            "name": self.name,
            "kind": self.kind,
            "load": "ok" if self.loads else "error",
            "error": self.load_error,
            "path": str(self.path) if self.path is not None else "",
            "findings": [
                {
                    "rule": finding.rule,
                    "severity": finding.severity,
                    "line": finding.line,
                    "message": finding.message,
                }
                for finding in self.findings
            ],
        }


class AgentValidator:
    """Build :class:`AgentReport` rows against one registry's current state.

    The lint's view of the world — the tool catalog, the agents that loaded,
    the ones that did not — is taken once, at construction, so reporting on
    every row of a listing costs one prompt read per row and nothing more.
    Build a new validator after the registry reloads.

    Args:
        registry: The registry whose agents are validated.
    """

    __registry: AgentRegistry
    __broken: dict[str, BrokenAgent]
    __context: LintContext

    def __init__(self, registry: AgentRegistry) -> None:
        self.__registry = registry
        self.__broken = {entry.name: entry for entry in registry.broken_agents}
        self.__context = LintContext(
            tool_names=frozenset(tool.name for tool in ALL_TOOLS),
            agent_names=registry.agent_names(),
            broken_names=frozenset(self.__broken),
        )

    @property
    def context(self) -> LintContext:
        """The lint context every report is built with."""
        return self.__context

    def validate(self, name: str) -> AgentReport | None:
        """Report on one agent, loaded or broken.

        Args:
            name: The agent's name — a built-in, a loaded user agent, or a
                user agent that failed to load.

        Returns:
            AgentReport | None: The report, or ``None`` when no agent by that
            name is loaded or broken.
        """
        if name in self.__context.agent_names:
            agent = self.__registry.get(name)
            return AgentReport(
                name=name,
                kind=KIND_AGENT if agent.is_top_level else KIND_SUBAGENT,
                findings=self.__lint(agent, agent.tools, self.__registry.authors_of(name)),
                path=agent.source_path,
            )
        broken = self.__broken.get(name)
        if broken is None:
            return None
        kind = KIND_AGENT if broken.is_top_level else KIND_SUBAGENT
        prompt = self.__prompt_path(broken)
        try:
            parsed = load_agent(prompt) if prompt is not None else None
        except (AgentLoadError, OSError):
            parsed = None
        if parsed is None:
            return AgentReport(name=name, kind=kind, load_error=broken.error, path=prompt)
        return AgentReport(
            name=name,
            kind=kind,
            load_error=broken.error,
            findings=self.__lint(parsed, parsed.tools, frozenset()),
            path=prompt,
        )

    def __lint(
        self, agent: SubAgent, granted: frozenset[str], authors: frozenset[str]
    ) -> tuple[LintFinding, ...]:
        """Lint *agent*'s prompt file as it is on disk."""
        try:
            text = agent.source_path.read_text(encoding="utf-8")
        except OSError:
            return ()
        return lint_prompt(agent, text, granted, self.__context, authors=authors)

    @staticmethod
    def __prompt_path(broken: BrokenAgent) -> Path | None:
        """The prompt file of a broken row, whose path is its file or its directory."""
        if broken.path.is_file():
            return broken.path
        stem = "agent_" if broken.is_top_level else "subagent_"
        candidate = broken.path / f"{stem}{broken.name}.md"
        return candidate if candidate.is_file() else None
