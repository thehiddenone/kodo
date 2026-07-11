"""Sub-agent specifications — the typed input/output contract of each sub-agent.

This package contains **only** :class:`~kodo.subagents.SubAgentSpec` catalog
entries — one module per agent, named ``_<name>.py``, each exporting a single
module-level ``SubAgentSpec`` constant (e.g. ``_coder.py`` exports ``CODER``).
It mirrors the :mod:`kodo.toolspecs` one-literal-per-file convention; the only
shared code is :mod:`._shapes`, declarative schema *builders* (no runtime logic).

Every sub-agent **except** the user-facing entry agents (``guide``,
``problem_solver``) has a spec here. The registry cross-references a spec to its
``subagent_<name>.md`` by ``name`` and fails fast if either side is missing.

When adding a sub-agent, add a new ``_<name>.py`` module and list its constant in
:data:`ALL_SUBAGENTS`.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec
from ._architect import ARCHITECT
from ._architect_critic import ARCHITECT_CRITIC
from ._code_critic import CODE_CRITIC
from ._coder import CODER
from ._compactor import COMPACTOR
from ._developer import DEVELOPER
from ._e2e_test_code_critic import E2E_TEST_CODE_CRITIC
from ._e2e_test_coder import E2E_TEST_CODER
from ._e2e_test_design_critic import E2E_TEST_DESIGN_CRITIC
from ._e2e_test_designer import E2E_TEST_DESIGNER
from ._functional_design_critic import FUNCTIONAL_DESIGN_CRITIC
from ._functional_designer import FUNCTIONAL_DESIGNER
from ._investigator import INVESTIGATOR
from ._narrative_author import NARRATIVE_AUTHOR
from ._planner import PLANNER
from ._requirements_author import REQUIREMENTS_AUTHOR
from ._requirements_critic import REQUIREMENTS_CRITIC
from ._test_coder import TEST_CODER
from ._test_design_critic import TEST_DESIGN_CRITIC
from ._test_designer import TEST_DESIGNER
from ._toolchain_cpp import TOOLCHAIN_CPP
from ._toolchain_depsmgr import TOOLCHAIN_DEPSMGR
from ._toolchain_python import TOOLCHAIN_PYTHON
from ._web_search_agent import WEB_SEARCH_AGENT

__all__ = [
    "ALL_SUBAGENTS",
    "ARCHITECT",
    "ARCHITECT_CRITIC",
    "CODER",
    "CODE_CRITIC",
    "COMPACTOR",
    "DEVELOPER",
    "E2E_TEST_CODER",
    "E2E_TEST_CODE_CRITIC",
    "E2E_TEST_DESIGNER",
    "E2E_TEST_DESIGN_CRITIC",
    "FUNCTIONAL_DESIGNER",
    "FUNCTIONAL_DESIGN_CRITIC",
    "INVESTIGATOR",
    "NARRATIVE_AUTHOR",
    "PLANNER",
    "REQUIREMENTS_AUTHOR",
    "REQUIREMENTS_CRITIC",
    "TEST_CODER",
    "TEST_DESIGNER",
    "TEST_DESIGN_CRITIC",
    "TOOLCHAIN_CPP",
    "TOOLCHAIN_DEPSMGR",
    "TOOLCHAIN_PYTHON",
    "WEB_SEARCH_AGENT",
    "SubAgentSpec",
]

# Every sub-agent spec in the catalog. Consumed by kodo.subagents._registry to
# render each agent's `## Your Task Contract` and the caller's `## Subagents`
# roster, and to validate spec<->subagent_<name>.md correspondence.
ALL_SUBAGENTS: tuple[SubAgentSpec, ...] = (
    NARRATIVE_AUTHOR,
    ARCHITECT,
    ARCHITECT_CRITIC,
    REQUIREMENTS_AUTHOR,
    REQUIREMENTS_CRITIC,
    FUNCTIONAL_DESIGNER,
    FUNCTIONAL_DESIGN_CRITIC,
    TEST_DESIGNER,
    TEST_DESIGN_CRITIC,
    TEST_CODER,
    CODER,
    CODE_CRITIC,
    E2E_TEST_DESIGNER,
    E2E_TEST_DESIGN_CRITIC,
    E2E_TEST_CODER,
    E2E_TEST_CODE_CRITIC,
    INVESTIGATOR,
    PLANNER,
    DEVELOPER,
    TOOLCHAIN_PYTHON,
    TOOLCHAIN_CPP,
    TOOLCHAIN_DEPSMGR,
    COMPACTOR,
    WEB_SEARCH_AGENT,
)
