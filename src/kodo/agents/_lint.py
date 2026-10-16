"""Deterministic lint for agent prompts — doc/AGENT_BUILDER.md §5.2.

Pure functions over one agent's prompt file: no registry, no disk, no model.
What the lint needs to know about the rest of the world (the tool catalog, the
agents that loaded, the ones that did not) arrives in a :class:`LintContext`,
so the same call lints an installed agent, a built-in, or a draft that has
not been written yet.

Every finding is a **warning** (D8). Whether an agent *loads* is the registry's
question, answered by its own strict rules; the lint only reports where a
prompt departs from the prompt-writing rules (CLAUDE.md "Prompts"), so a user
can fix it or ignore it.

The rules
=========

======================  ========================================================
``L1 tool-not-granted``  A backticked identifier names a tool the agent is not
                         granted.
``L2 subagent-not-listed``  ``run_subagent_<n>`` where ``<n>`` is not in
                         ``subagents:``.
``L3 unknown-agent``     A backticked ``kodo_<n>`` that is not a loaded agent,
                         or a backticked user agent name that failed to load.
``L4 purpose``           The first paragraph after the title is not one
                         sentence.
``L5 sections``          A ``##`` section required for the agent's kind is
                         missing (D26).
``L6 vague-qualifier``   A word from the CLAUDE.md list, outside code spans,
                         fenced blocks, block quotes and double-quoted text
                         (D29).
``L7 schema-restated``   A fenced block that looks like a JSON Schema.
======================  ========================================================

Line numbers count from the top of the **file**, frontmatter included, so an
editor can jump straight to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._builtin import ENGINE_DRIVEN_AGENTS
from ._loader import AgentLoadError, SubAgent, split_frontmatter

__all__ = [
    "LINT_RULES",
    "RULE_PURPOSE",
    "RULE_SCHEMA_RESTATED",
    "RULE_SECTIONS",
    "RULE_SUBAGENT_NOT_LISTED",
    "RULE_TOOL_NOT_GRANTED",
    "RULE_UNKNOWN_AGENT",
    "RULE_VAGUE_QUALIFIER",
    "SEVERITY_WARNING",
    "LintContext",
    "LintFinding",
    "lint_prompt",
]

RULE_TOOL_NOT_GRANTED = "L1 tool-not-granted"
RULE_SUBAGENT_NOT_LISTED = "L2 subagent-not-listed"
RULE_UNKNOWN_AGENT = "L3 unknown-agent"
RULE_PURPOSE = "L4 purpose"
RULE_SECTIONS = "L5 sections"
RULE_VAGUE_QUALIFIER = "L6 vague-qualifier"
RULE_SCHEMA_RESTATED = "L7 schema-restated"

#: Every rule ID, in order.
LINT_RULES: tuple[str, ...] = (
    RULE_TOOL_NOT_GRANTED,
    RULE_SUBAGENT_NOT_LISTED,
    RULE_UNKNOWN_AGENT,
    RULE_PURPOSE,
    RULE_SECTIONS,
    RULE_VAGUE_QUALIFIER,
    RULE_SCHEMA_RESTATED,
)

#: The one severity a lint finding has (D8).
SEVERITY_WARNING = "warning"

_INPUTS = "Inputs"
_WORKFLOW = "Workflow"
_REPORTING = "Reporting"
_AVOID = "What to Avoid"

# L5, per kind (D26). Engine-driven agents are exempt and have no row.
_REQUIRED_AUTHOR: tuple[str, ...] = (_INPUTS, _WORKFLOW, _REPORTING, _AVOID)
_REQUIRED_CRITIC: tuple[str, ...] = (_INPUTS, _REPORTING, _AVOID)
_REQUIRED_TOP_LEVEL: tuple[str, ...] = (_WORKFLOW, _AVOID)

# L6: the CLAUDE.md list. Multi-word entries match across any run of spaces.
_VAGUE_RE = re.compile(
    r"\b(almost|possibly|maybe|appropriate|reasonable|as\s+needed|may|could|should\s+consider)\b",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_QUOTED_RE = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUN_SUBAGENT_RE = re.compile(r"(?<!\w)run_subagent_(\w+)", re.ASCII)
_HEADING_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SCHEMA_TYPE_RE = re.compile(r"\"type\"\s*:\s*\"object\"")

# A sentence ends at ``.``/``!``/``?`` (optionally closed by ``*``, ``_``, a
# quote or a bracket) followed by whitespace and something that can start the
# next sentence. Lowercase after the stop does not end one, which keeps
# "e.g. this" and "i.e. that" whole.
_SENTENCE_END_RE = re.compile(r"[.!?][*_\"')\]]*\s+(?=[A-Z0-9*_\"'(\[`])")

_BUILTIN_PREFIX = "kodo_"
_RUN_SUBAGENT_PREFIX = "run_subagent_"


@dataclass(frozen=True)
class LintFinding:
    """One place a prompt departs from the prompt-writing rules.

    Attributes:
        rule: The rule's ID, one of :data:`LINT_RULES`.
        severity: Always :data:`SEVERITY_WARNING` (D8).
        message: What is wrong and how to fix it, in one sentence.
        line: The 1-based line in the prompt file, or ``None`` for a finding
            about something absent (a missing section).
    """

    rule: str
    severity: str
    message: str
    line: int | None = None

    def as_payload(self) -> dict[str, object]:
        """The finding as JSON-ready data, the shape every report and tool returns.

        Returns:
            dict[str, object]: ``{rule, severity, line, message}``, ``line``
            being ``null`` for a finding about something absent.
        """
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
        }


@dataclass(frozen=True)
class LintContext:
    """What the lint needs to know beyond the one prompt it is reading.

    Attributes:
        tool_names: Every tool name in the ``ToolSpec`` catalog.
        agent_names: Every agent that loaded — built-in and user.
        broken_names: Every user agent present on disk that failed to load.
        engine_driven: The sub-agents the engine calls by name, exempt from L5.
    """

    tool_names: frozenset[str]
    agent_names: frozenset[str]
    broken_names: frozenset[str] = frozenset()
    engine_driven: frozenset[str] = ENGINE_DRIVEN_AGENTS


def lint_prompt(
    agent: SubAgent,
    text: str,
    granted: frozenset[str],
    context: LintContext,
    *,
    authors: frozenset[str] = frozenset(),
) -> tuple[LintFinding, ...]:
    """Lint one agent's prompt file.

    Args:
        agent: The parsed agent — its frontmatter decides which tools and
            sub-agents the body may name, and which sections it needs.
        text: The prompt file's whole contents, frontmatter included.
        granted: The tools the agent effectively holds: its ``tools:`` plus
            anything the registry grants on top (``return_result`` for a
            sub-agent with a spec).
        context: The tool catalog and the agent set.
        authors: For a critic, the authors whose ``critic:`` names it. Its
            prompt may name their ``run_subagent_<author>`` tool, because that
            is the call it runs inside, just as any agent may name its own.

    Returns:
        tuple[LintFinding, ...]: Every finding, in rule order, then line order.
        Empty for a file with no frontmatter — the loader has already refused
        it, and there is no body to lint.
    """
    try:
        frontmatter, _ = split_frontmatter(text)
    except AgentLoadError:
        return ()
    # Blank the frontmatter out rather than cutting it off, so an offset into
    # what is left is an offset into the file and line numbers come out right.
    body = re.sub(r"[^\n]", " ", frontmatter) + text[len(frontmatter) :]
    unfenced, fences = _split_fences(body)
    spans = [(m.group(1).strip(), _line(body, m.start())) for m in _CODE_SPAN_RE.finditer(unfenced)]
    findings = [
        *_tool_not_granted(spans, granted, context),
        *_subagent_not_listed(agent, authors, unfenced, body),
        *_unknown_agent(spans, context),
        *_purpose(unfenced, body),
        *_sections(agent, unfenced, context),
        *_vague(unfenced, body),
        *_schema_restated(fences),
    ]
    return tuple(findings)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _line(text: str, offset: int) -> int:
    """The 1-based line *offset* falls on."""
    return text.count("\n", 0, offset) + 1


def _blank(match: re.Match[str]) -> str:
    """Replace a match with spaces, keeping its newlines and its length."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _split_fences(body: str) -> tuple[str, list[tuple[int, str]]]:
    """Blank out every fenced block; return the rest and the blocks.

    Returns:
        tuple[str, list[tuple[int, str]]]: *body* with every fenced line
        (fences included) blanked, and each block as (opening line, content).
    """
    lines = body.split("\n")
    fences: list[tuple[int, str]] = []
    opened: int | None = None
    content: list[str] = []
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            if opened is None:
                opened, content = index, []
            else:
                fences.append((opened + 1, "\n".join(content)))
                opened = None
            lines[index] = " " * len(line)
            continue
        if opened is not None:
            content.append(line)
            lines[index] = " " * len(line)
    if opened is not None:
        fences.append((opened + 1, "\n".join(content)))
    return "\n".join(lines), fences


def _tool_not_granted(
    spans: list[tuple[str, int]], granted: frozenset[str], context: LintContext
) -> list[LintFinding]:
    """L1: a backticked tool name the agent does not hold."""
    return [
        LintFinding(
            RULE_TOOL_NOT_GRANTED,
            SEVERITY_WARNING,
            f"`{span}` is a tool this agent is not granted — add it to tools: or stop naming it",
            line,
        )
        for span, line in spans
        if span in context.tool_names and span not in granted
    ]


def _subagent_not_listed(
    agent: SubAgent, authors: frozenset[str], unfenced: str, body: str
) -> list[LintFinding]:
    """L2: ``run_subagent_<n>`` for a ``<n>`` the agent may not spawn.

    Naming the call the agent itself runs inside is not an instruction to make
    it: an agent's own tool, and a critic's author's, are exempt.
    """
    exempt = agent.subagents | authors | {agent.name}
    return [
        LintFinding(
            RULE_SUBAGENT_NOT_LISTED,
            SEVERITY_WARNING,
            f"names run_subagent_{m.group(1)}, but {m.group(1)!r} is not in subagents:",
            _line(body, m.start()),
        )
        for m in _RUN_SUBAGENT_RE.finditer(unfenced)
        if m.group(1) not in exempt
    ]


def _unknown_agent(spans: list[tuple[str, int]], context: LintContext) -> list[LintFinding]:
    """L3: a backticked agent name that names no loaded agent."""
    findings: list[LintFinding] = []
    for span, line in spans:
        if not _IDENTIFIER_RE.match(span) or span.startswith(_RUN_SUBAGENT_PREFIX):
            continue
        if span in context.broken_names:
            findings.append(
                LintFinding(
                    RULE_UNKNOWN_AGENT,
                    SEVERITY_WARNING,
                    f"`{span}` names a user agent that does not load",
                    line,
                )
            )
        elif (
            span.startswith(_BUILTIN_PREFIX)
            and span not in context.agent_names
            and span not in context.tool_names
        ):
            findings.append(
                LintFinding(
                    RULE_UNKNOWN_AGENT,
                    SEVERITY_WARNING,
                    f"`{span}` names no agent Kōdo has loaded",
                    line,
                )
            )
    return findings


def _purpose(unfenced: str, body: str) -> list[LintFinding]:
    """L4: the first paragraph after the title is one sentence."""
    paragraphs = [
        (match.start(), match.group(0).strip())
        for match in re.finditer(r"(?s)\S.*?(?=\n[ \t]*\n|\Z)", unfenced)
    ]
    if paragraphs:
        # The title is the first line, whether or not a blank line follows it.
        start, first = paragraphs[0]
        title, _, rest = first.partition("\n")
        if _is_title(title):
            paragraphs = paragraphs[1:]
            if rest.strip():
                offset = start + len(title) + 1
                paragraphs.insert(0, (offset + len(rest) - len(rest.lstrip()), rest.strip()))
    if not paragraphs:
        return [
            LintFinding(
                RULE_PURPOSE,
                SEVERITY_WARNING,
                "the prompt has no purpose sentence after its title",
                None,
            )
        ]
    offset, paragraph = paragraphs[0]
    line = _line(body, offset)
    if paragraph.startswith(("#", "- ", "* ", "{", "|")) or re.match(r"\d+\.\s", paragraph):
        return [
            LintFinding(
                RULE_PURPOSE,
                SEVERITY_WARNING,
                "the first paragraph after the title is not prose — open with one sentence "
                "naming the agent's purpose",
                line,
            )
        ]
    prose = _CODE_SPAN_RE.sub("code", paragraph)
    sentences = len(_SENTENCE_END_RE.findall(prose.rstrip())) + 1
    if sentences > 1:
        return [
            LintFinding(
                RULE_PURPOSE,
                SEVERITY_WARNING,
                f"the first paragraph after the title has {sentences} sentences — it must be "
                f"one sentence naming the agent's purpose",
                line,
            )
        ]
    return []


def _is_title(line: str) -> bool:
    """Whether a line is the title: a ``#`` heading or a bold name alone."""
    text = line.strip()
    return text.startswith("# ") or bool(re.fullmatch(r"\*\*[^*]+\*\*", text))


def _sections(agent: SubAgent, unfenced: str, context: LintContext) -> list[LintFinding]:
    """L5: every ``##`` section the agent's kind requires is present."""
    if agent.name in context.engine_driven:
        return []
    if agent.is_top_level:
        required, kind = _REQUIRED_TOP_LEVEL, "a top-level agent"
    elif agent.is_critic:
        required, kind = _REQUIRED_CRITIC, "a critic"
    else:
        required, kind = _REQUIRED_AUTHOR, "a sub-agent"
    present = {m.group(1) for m in _HEADING_RE.finditer(unfenced)}
    return [
        LintFinding(
            RULE_SECTIONS,
            SEVERITY_WARNING,
            f"no '## {section}' section, which {kind} requires",
            None,
        )
        for section in required
        if section not in present
    ]


def _vague(unfenced: str, body: str) -> list[LintFinding]:
    """L6: a vague qualifier in prose."""
    prose = "\n".join(
        " " * len(line) if line.lstrip().startswith(">") else line for line in unfenced.split("\n")
    )
    prose = _CODE_SPAN_RE.sub(_blank, prose)
    prose = _QUOTED_RE.sub(_blank, prose)
    return [
        LintFinding(
            RULE_VAGUE_QUALIFIER,
            SEVERITY_WARNING,
            f"vague qualifier {' '.join(m.group(1).split())!r} — replace it with a definite "
            f"rule or a measurable threshold",
            _line(body, m.start()),
        )
        for m in _VAGUE_RE.finditer(prose)
    ]


def _schema_restated(fences: list[tuple[int, str]]) -> list[LintFinding]:
    """L7: a fenced block that restates a JSON Schema."""
    return [
        LintFinding(
            RULE_SCHEMA_RESTATED,
            SEVERITY_WARNING,
            "this fenced block looks like a JSON Schema — name the tool and let its schema "
            "speak for itself, so the prompt and the schema cannot drift",
            line,
        )
        for line, content in fences
        if _SCHEMA_TYPE_RE.search(content) and '"properties"' in content
    ]
