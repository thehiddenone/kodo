"""The artifact-role vocabulary and the per-agent input/output declarations.

Every pipeline sub-agent declares what it **produces** and what it
**consumes**, in terms of a small closed vocabulary of artifact *roles*
(``architecture``, ``requirements``, …) rather than file paths. The engine
resolves a consumer's declared needs against the session's work-product ledger
(:mod:`kodo.workproducts`) and hands it a fully-formed ``input_paths`` — so no
model ever has to guess where an upstream document lives.

The alternative, and what this replaced, was letting the calling model write
``input_paths`` freehand and hoping the labels meant something. They did not:
the engine's own critic spawn hardcoded a single ``{"target": path}`` for every
critic regardless of how many inputs its contract declared, and a
``requirements_critic`` that was promised the architecture and given only the
document under review reconstructed the architecture's path from the worked
example in its own schema description — then read the resulting nonexistent
file ~1133 times (session ``1788543589``; doc/FINDINGS.md, doc/STUCK_DETECTION.md
§2.11).

**Roles are not files.** A role says *what a document is for*, and one role may
be filled by several files (a component's whole implementation) while one agent
may fill several roles (``narrative_author`` writes both the Narrative and the
Tech Stack). That is why :attr:`~kodo.subagents.SubAgentSpec.produces` maps a
role to the *output field* carrying its paths, rather than being a single name:
see :data:`PRODUCES_REMAINDER`.

**Scopes** answer "which one?" when a role has been filled more than once —
per component, or per project. They are what lets a fixed vocabulary serve an
agent that reads dozens of files without ever naming one.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ALL_ROLES",
    "ALL_SCOPES",
    "PRODUCES_REMAINDER",
    "ROLE_ARCHITECTURE",
    "ROLE_CODE",
    "ROLE_DESIGN_PLAN",
    "ROLE_E2E_TEST_CODE",
    "ROLE_E2E_TEST_PLAN",
    "ROLE_FUNCTIONAL_DESIGN",
    "ROLE_NARRATIVE",
    "ROLE_REQUIREMENTS",
    "ROLE_TECH_STACK",
    "ROLE_TEST_CODE",
    "ROLE_TEST_PLAN",
    "SCOPE_ALL",
    "SCOPE_DEPENDENCIES",
    "SCOPE_GLOBAL",
    "SCOPE_SELF",
    "SCOPE_UNDER_REVIEW",
    "Need",
]

# --- roles ----------------------------------------------------------------

ROLE_NARRATIVE = "narrative"
ROLE_TECH_STACK = "tech_stack"
ROLE_ARCHITECTURE = "architecture"
ROLE_REQUIREMENTS = "requirements"
ROLE_DESIGN_PLAN = "design_plan"
ROLE_FUNCTIONAL_DESIGN = "functional_design"
ROLE_TEST_PLAN = "test_plan"
ROLE_TEST_CODE = "test_code"
ROLE_CODE = "code"
ROLE_E2E_TEST_PLAN = "e2e_test_plan"
ROLE_E2E_TEST_CODE = "e2e_test_code"

#: Every role a spec may name. Closed on purpose: the registry refuses to load
#: an agent naming one that is not here, so a typo fails at startup rather than
#: silently resolving to nothing mid-run.
ALL_ROLES: frozenset[str] = frozenset(
    {
        ROLE_NARRATIVE,
        ROLE_TECH_STACK,
        ROLE_ARCHITECTURE,
        ROLE_REQUIREMENTS,
        ROLE_DESIGN_PLAN,
        ROLE_FUNCTIONAL_DESIGN,
        ROLE_TEST_PLAN,
        ROLE_TEST_CODE,
        ROLE_CODE,
        ROLE_E2E_TEST_PLAN,
        ROLE_E2E_TEST_CODE,
    }
)

# --- scopes ---------------------------------------------------------------

#: The single current work product filling this role for the project.
SCOPE_GLOBAL = "global"
#: That role, narrowed to this spawn's ``responsibility_code`` — only
#: meaningful for a role produced by a per-component stage, whose work product
#: carries one.
SCOPE_SELF = "self"
#: Every work product filling this role. Bounded by the component count, and
#: what a product-level stage genuinely wants: the end-to-end stages design and
#: test the whole system, so every component's design is the right input, not a
#: fallback for missing attribution.
SCOPE_ALL = "all"
#: The work product this critic round is reviewing. Supplied by the engine
#: from the round itself, never looked up.
SCOPE_UNDER_REVIEW = "under_review"
#: The designs of components this one consumes or is consumed by, in **both**
#: directions — an interface has two sides. Resolved from the component graph
#: the architect returns alongside its document and the engine persists per
#: project; a project whose architect predates the graph resolves it to nothing,
#: which is why ``coder`` declares it ``required=False``.
SCOPE_DEPENDENCIES = "dependencies"

ALL_SCOPES: frozenset[str] = frozenset(
    {SCOPE_GLOBAL, SCOPE_SELF, SCOPE_ALL, SCOPE_UNDER_REVIEW, SCOPE_DEPENDENCIES}
)

#: The ``produces`` field name meaning "every reported path not already claimed
#: by another role's named field". An ordinary author maps its one role to this;
#: ``functional_designer`` maps ``design_plan`` to its own output field and
#: ``functional_design`` to this, so the plan does not also arrive as a design.
PRODUCES_REMAINDER = "paths"


@dataclass(frozen=True)
class Need:
    """One input an agent declares it needs, as a role plus a scope.

    Attributes:
        role: A member of :data:`ALL_ROLES`.
        scope: A member of :data:`ALL_SCOPES` — which of that role's work
            products this agent wants.
        required: When ``True`` the agent genuinely cannot do its job without
            this input. An unresolvable required need is a loud, named failure
            rather than an agent left to guess (which is the failure this whole
            mechanism exists to remove). Optional needs resolve to nothing and
            are simply absent from ``input_paths``.
    """

    role: str
    scope: str = SCOPE_GLOBAL
    required: bool = True
