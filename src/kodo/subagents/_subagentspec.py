"""The :class:`SubAgentSpec` dataclass — the typed interface of a sub-agent.

A sub-agent is "a tool with agentic behavior": like a :class:`~kodo.toolspecs.ToolSpec`
it declares an ``input_schema`` (what the caller must supply when delegating) and
an ``output_schema`` (what the sub-agent returns, via the ``return_result`` tool,
when it finishes). Both are JSON-Schema ``object`` dicts.

Per the project decision, a ``SubAgentSpec`` carries **only** the agent's
input/output *contract*; every other piece of agent metadata — tools,
capability, ``display_name``, ``critic``/``standalone``, and the prose
describing the agent (``## Purpose``) — stays in the ``subagent_*.md``
frontmatter/body and is loaded by :func:`~kodo.subagents._loader.load_agent`.

Since 2026-09-05 the contract is more than the two schemas: ``produces`` and
``consumes`` declare, in artifact *roles* (:mod:`._artifacts`), what this agent
writes and what it must be given. They live here rather than in frontmatter
because they *are* the contract — ``consumes`` is what the engine turns into a
concrete ``input_paths`` — where the frontmatter holds behavioural policy
(which critic reviews me, which tools I may call). The registry cross-references a spec
to its :class:`~kodo.subagents._loader.SubAgent` by ``name``.

There was once a ``description`` field here too, a one-line caller-facing
summary. It was deleted because it competed with ``## Purpose`` for the same
job: both described the sub-agent to a caller, and once the prompt-side roster
was removed both wanted the same destination — the generated
``run_subagent_<name>`` tool's description. ``## Purpose`` won (it is the fuller
text, it lives with the prose, and it was already written caller-agnostic), so
the schemas here and the prose there no longer overlap at all.

One spec per file under :mod:`kodo.subagents.specs`, mirroring the
``kodo.toolspecs`` one-literal-per-file convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._artifacts import Need

__all__ = ["RESPONSIBILITY_CODE_KEY", "SubAgentSpec"]


#: The task field naming the component a per-component spawn is scoped to.
#:
#: Only a spec that genuinely runs once per component declares it (pipeline
#: stages 5-7, through ``pipeline_input(require_responsibility=True)``). Every
#: other spec omits the property entirely, and the engine drops the field from
#: any task aimed at such an agent — see
#: :attr:`SubAgentSpec.takes_responsibility_code`.
RESPONSIBILITY_CODE_KEY = "responsibility_code"


@dataclass(frozen=True)
class SubAgentSpec:
    """The typed input/output contract of a single sub-agent.

    Attributes:
        name: Sub-agent name — matches ``SubAgent.name`` and the
            ``subagent_<name>.md`` filename stem.
        input_schema: JSON Schema (an ``object`` schema) describing the
            structured task the caller must supply when delegating. The engine
            validates the delegated ``task_input`` against it before spawning.
        output_schema: JSON Schema describing what the sub-agent returns through
            the ``return_result`` tool. The engine augments it with the
            engine-owned ``schema_compliance`` field (see
            :mod:`kodo.toolspecs._compliance`) before showing it to the agent and
            normalizes the ``return_result`` payload against it — so, like a
            tool's output schema, specs must NOT declare ``schema_compliance``.
            May be a top-level ``oneOf`` for a dual-role agent (``test_coder``).
        produces: ``{artifact role: output field carrying its path(s)}`` — what
            this agent's work product is *for*. An ordinary author maps its one
            role to :data:`~._artifacts.PRODUCES_REMAINDER` (``"paths"``),
            meaning "everything I reported". An agent filling several roles
            names a distinct output field per role, and the role mapped to the
            remainder receives whatever no named field claimed — which is how
            ``functional_designer`` keeps the Design Plan out of the pile of
            Functional Designs. Empty for a critic (it writes findings, not
            artifacts) and for a non-pipeline agent.
        component_paths: Name of an output field holding
            ``{component codename: path}``, for an agent that writes several
            components' artifacts in **one** run. Only ``functional_designer``
            needs it: it produces every component's Functional Design in a
            single whole-product call, so its work product carries no
            ``responsibility_code`` and per-file attribution is the only way
            ``self``/``dependencies`` scopes can narrow it. Empty for a
            per-component agent, whose work product's own code already covers
            every member.
        consumes: The inputs this agent needs, as
            :class:`~._artifacts.Need` values. The engine resolves them against
            the session's work-product ledger and hands over a fully-formed
            ``input_paths``; declaring nothing means the engine builds nothing
            and the caller's own ``input_paths`` stands unchanged.
    """

    name: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    produces: dict[str, str] = field(default_factory=dict)
    consumes: tuple[Need, ...] = ()
    component_paths: str = ""

    @property
    def takes_responsibility_code(self) -> bool:
        """Whether this agent is scoped to one component by its caller.

        True only when the spec's ``input_schema`` declares
        :data:`RESPONSIBILITY_CODE_KEY` — i.e. the agent genuinely runs once per
        component (pipeline stages 5-7). The engine reads this instead of
        trusting the field's presence in a task: a ``responsibility_code`` aimed
        at any other agent is dropped before it can reach the work-product id,
        role resolution or the rendered brief, so a caller cannot split a
        product-level agent's record by supplying one. The prompt-level rule
        that used to be the only guard is now a description of engine
        behaviour, not an obligation a model has to remember.
        """
        properties = self.input_schema.get("properties")
        return isinstance(properties, dict) and RESPONSIBILITY_CODE_KEY in properties
