"""Agent markdown file parser — frontmatter + system-prompt body.

Each agent file is a Markdown document with YAML frontmatter:

    ---
    name: narrative_author
    tools:
      - fileio_write_file
    ---
    <system prompt body>

The filename stem must be ``subagent_<name>`` for sub-agents (everything spawned
through a ``run_subagent_<name>`` tool, plus the critics the engine spawns on
their behalf) or ``agent_<name>`` for the user-facing entry agents (``guide``,
``problem_solver``) that drive a session directly rather than being spawned by
one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ROLE_CRITIC", "SubAgent", "AgentLoadError", "load_agent"]

# The one recognized ``role:`` value. See ``SubAgent.role``.
ROLE_CRITIC = "critic"
_ROLES: frozenset[str] = frozenset({"", ROLE_CRITIC})

_FRONT_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

# Captures the body of a ``## Purpose`` section: everything after the heading
# line up to (but not including) the next ``#``/``##`` heading or end of file.
_PURPOSE_RE = re.compile(r"(?ms)^##[ \t]+Purpose[ \t]*$\n?(.*?)(?=^#{1,2}[ \t]|\Z)")


class AgentLoadError(Exception):
    """Raised when a subagent file cannot be parsed or lacks required fields."""


@dataclass(frozen=True)
class SubAgent:
    """A loaded subagent definition.

    Attributes:
        name: Subagent name from frontmatter (e.g. ``'narrative_author'``).
        tools: MCP tool names this subagent may invoke.
        subagents: Names of sub-agents this agent is permitted to spawn (via a
            ``run_subagent_<name>`` tool). Empty by default,
            so an agent can spawn nothing unless its frontmatter opts in. There is
            no built-in "only the guide spawns" assumption — any agent that
            declares both a spawning tool and a ``subagents`` allow-list can drive
            sub-agents, and the engine enforces the allow-list at dispatch time.
        system_prompt: Full system prompt body.
        source_path: Absolute path to the source ``.md`` file.
        capability: Preferred LLM capability tier — ``'max'``, ``'high'``,
            ``'medium'``, or ``'low'``.  Defaults to ``'medium'`` when not set
            in frontmatter.
        display_name: User-friendly name shown in the UI (e.g. in subsession
            takeover dividers). Falls back to a title-cased ``name`` when the
            frontmatter does not set ``display_name``.
        subagent_order: The ``subagents:`` allow-list in declaration order. Same
            membership as :attr:`subagents` (a set, order-free, used for the
            dispatch gate), but order-preserving so a caller's generated
            ``run_subagent_<name>`` tools are built in the order the author
            listed them.
        purpose: Body of this agent's ``## Purpose`` section — a *caller-agnostic*
            description of what the agent does and when to call it. It becomes
            the **description of this agent's ``run_subagent_<name>`` tool**, so
            it is written third-person for whoever is deciding whether to
            delegate, and the registry requires it on every invocable sub-agent.
            Empty only for an entry agent or a critic (neither is invocable).
        role: The agent's structural role, from frontmatter ``role:``. Only
            ``"critic"`` is recognized today; everything else (the default ``""``)
            is an ordinary sub-agent. A critic is **not** invocable by a caller —
            it gets no ``run_subagent_<name>`` tool — and its result is a review
            verdict the engine records in the reviewed document's ``.jsonl``
            evolution log (see :mod:`kodo.guided_state`) rather than a value it
            hands back to whoever spawned it. Declared explicitly rather than
            inferred from "is named as someone's ``critic:``" so the engine never
            bakes in a blanket assumption about which agents behave this way.
        critic: Name of the critic this agent is paired with (frontmatter
            ``critic:``). A non-empty value marks the agent an **author**: its
            ``run_subagent_<name>`` tool runs the whole author→critic loop in one
            call rather than a single pass. Empty for unreviewed agents and for
            critics themselves.
        standalone: ``True`` when this agent is **not** part of the ordered
            pipeline (frontmatter ``standalone: true``) — a specialist invoked on
            demand whenever the need arises, with no upstream dependency on any
            other agent's output. ``False`` (the default) marks a **workflow**
            agent that advances the pre-determined pipeline and consumes the
            artifacts of the stage before it. Stated as a sentence in the
            generated ``run_subagent_<name>`` tool's description, since it is
            what tells a caller whether ordering matters.
        user_review: ``True`` when this agent's work product must be signed off
            by the **user** before it is accepted (frontmatter
            ``user_review: true``). Opt-in and ``False`` by default: an agent
            that does not declare it has its work product accepted the moment
            nothing is outstanding against it, with no gate. Orthogonal to
            ``critic`` — the two compose in every combination:

            =============  ==============  ==================================
            ``critic``     ``user_review``  What one ``run_subagent`` call does
            =============  ==============  ==================================
            set            ``False``        author→critic rounds; auto-accept
            set            ``True``         author→critic rounds, then the gate
            ``""``         ``True``         author→gate rounds (no critic)
            ``""``         ``False``        one pass, no review at all
            =============  ==============  ==================================

            Which artifacts are worth a human's attention is a property of the
            artifact, not of whether someone happened to pair a critic with its
            author — which is what decided it before this flag existed.
        planner: ``True`` when this agent's result **is a plan** (frontmatter
            ``planner: true``). The engine parses such a result and initializes
            the session's plan from it — the ordered ``tasks`` and the
            ``codebase_context`` — so the agent that commissioned the plan then
            tracks it through ``get_plan``/``plan_step_forward`` instead of
            re-describing it every round (doc/PLANNING.md).

            A declaration, never an inference: nothing in the engine knows that
            the agent *named* ``planner`` plans, and a second or third planner
            added later needs no engine change. What it does imply is a
            **contract** on the agent's :class:`~kodo.subagents.SubAgentSpec` —
            its ``output_schema`` must declare ``tasks`` and
            ``codebase_context``, which
            :class:`~kodo.subagents.AgentRegistry` checks at load time so a
            planner whose result the engine could not read fails fast rather
            than at first spawn.

            Orthogonal to ``critic``/``user_review`` and to ``standalone``: the
            plan is initialized from whatever result the declared flow finally
            produces.
    """

    name: str
    tools: frozenset[str]
    system_prompt: str
    source_path: Path
    capability: str = "medium"
    display_name: str = ""
    subagents: frozenset[str] = frozenset()
    subagent_order: tuple[str, ...] = ()
    purpose: str = ""
    role: str = ""
    critic: str = ""
    standalone: bool = False
    user_review: bool = False
    planner: bool = False

    @property
    def is_critic(self) -> bool:
        """Whether this agent is a critic (frontmatter ``role: critic``)."""
        return self.role == ROLE_CRITIC


def load_agent(path: Path) -> SubAgent:
    """Parse a single subagent markdown file.

    Args:
        path: Absolute path to the ``.md`` file.

    Returns:
        SubAgent: Fully populated subagent dataclass.

    Raises:
        AgentLoadError: File is missing frontmatter, a required field, or has an
            empty system-prompt body.
    """
    text = path.read_text(encoding="utf-8")
    fm_dict, body = _parse_frontmatter(text, path)

    name = fm_dict.get("name")
    if not isinstance(name, str) or not name:
        raise AgentLoadError(f"{path}: missing or empty 'name' in frontmatter")

    tools_raw = fm_dict.get("tools", [])
    if isinstance(tools_raw, list):
        tools: frozenset[str] = frozenset(str(t) for t in tools_raw)
    elif isinstance(tools_raw, str):
        tools = frozenset([tools_raw])
    else:
        tools = frozenset()

    subagents_raw = fm_dict.get("subagents", [])
    if isinstance(subagents_raw, list):
        subagent_order: tuple[str, ...] = tuple(str(s) for s in subagents_raw)
    elif isinstance(subagents_raw, str):
        subagent_order = (subagents_raw,)
    else:
        subagent_order = ()
    subagents: frozenset[str] = frozenset(subagent_order)

    expected_stems = (f"subagent_{name}", f"agent_{name}")
    if path.stem not in expected_stems:
        raise AgentLoadError(
            f"{path}: filename stem {path.stem!r} does not match expected "
            f"{expected_stems[0]!r} or {expected_stems[1]!r}"
        )

    if not body:
        raise AgentLoadError(f"{path}: system-prompt body is empty")

    capability_raw = fm_dict.get("capability", "medium")
    capability = str(capability_raw) if isinstance(capability_raw, str) else "medium"
    if capability not in ("max", "high", "medium", "low"):
        capability = "medium"

    display_raw = fm_dict.get("display_name")
    display_name = (
        str(display_raw).strip()
        if isinstance(display_raw, str) and display_raw.strip()
        else _default_display_name(name)
    )

    role = _scalar(fm_dict.get("role")).lower()
    if role not in _ROLES:
        raise AgentLoadError(
            f"{path}: unknown role {role!r} in frontmatter; expected one of "
            f"{sorted(r for r in _ROLES if r)} or no 'role:' at all"
        )
    critic = _scalar(fm_dict.get("critic"))
    if role == ROLE_CRITIC and critic:
        raise AgentLoadError(f"{path}: a 'role: critic' agent cannot itself declare a 'critic:'")
    standalone = _scalar(fm_dict.get("standalone")).lower() in ("true", "yes", "1")
    user_review = _scalar(fm_dict.get("user_review")).lower() in ("true", "yes", "1")
    if role == ROLE_CRITIC and user_review:
        raise AgentLoadError(
            f"{path}: a 'role: critic' agent cannot declare 'user_review:' — a critic "
            f"writes findings, not a work product, so there is nothing for the user to "
            f"sign off on; declare it on the author whose work it reviews"
        )
    planner = _scalar(fm_dict.get("planner")).lower() in ("true", "yes", "1")
    if role == ROLE_CRITIC and planner:
        raise AgentLoadError(
            f"{path}: a 'role: critic' agent cannot declare 'planner:' — a critic returns "
            f"a review verdict, not a plan, and the engine would have no tasks to "
            f"initialize a plan from"
        )
    purpose = _extract_purpose(body)

    return SubAgent(
        name=name,
        tools=tools,
        system_prompt=body,
        source_path=path,
        capability=capability,
        display_name=display_name,
        subagents=subagents,
        subagent_order=subagent_order,
        purpose=purpose,
        role=role,
        critic=critic,
        standalone=standalone,
        user_review=user_review,
        planner=planner,
    )


def _scalar(value: object) -> str:
    """Coerce a frontmatter value to a trimmed scalar string.

    The lightweight frontmatter parser yields scalars as ``str`` and lists as
    ``list[str]``; an empty scalar (``key:`` with nothing after the colon) comes
    back as an empty list. Normalize all of these to a plain string.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return ""


def _extract_purpose(body: str) -> str:
    """Return the body of the ``## Purpose`` section, or ``""`` when absent."""
    m = _PURPOSE_RE.search(body)
    return m.group(1).strip() if m else ""


def _default_display_name(name: str) -> str:
    """Derive a user-friendly name from a snake_case agent name.

    ``narrative_author`` → ``"Narrative Author"``.
    """
    return " ".join(part.capitalize() for part in name.split("_") if part) or name


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    m = _FRONT_RE.match(text)
    if not m:
        raise AgentLoadError(f"{path}: missing --- frontmatter delimiters")

    fm_text = m.group(1)
    body = text[m.end() :].strip()

    result: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] = []

    def _flush() -> None:
        if current_key is not None:
            result[current_key] = list(current_list)

    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            current_list.append(stripped[2:].strip())
        elif ":" in stripped:
            _flush()
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            current_key = key
            current_list = []
            if val:
                result[key] = val
                current_key = None

    _flush()
    return result, body
