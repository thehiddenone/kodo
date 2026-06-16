"""Tool specifications for every tool Kōdo's LLM-driven agents may call.

This package contains **only** :class:`ToolSpec` (the dataclass) and
``ToolSpec`` catalog entries — one module per tool, named ``_<tool_name>.py``,
each exporting a single module-level ``ToolSpec`` constant (e.g.
``_create_file.py`` exports ``CREATE_FILE``). When adding a new tool, add a
new ``_<tool_name>.py`` module here rather than extending an existing one.

No dispatch or implementation logic lives here. Orchestrator tool dispatch is
in :mod:`kodo.runtime._tool_surface`; leaf sub-agent tool dispatch is in
:mod:`kodo.runtime._subagent_dispatch`.
"""

from __future__ import annotations

from ._ask_user import ASK_USER
from ._ask_user_orchestrator import ORCHESTRATOR_ASK_USER
from ._copy_file import COPY_FILE
from ._create_file import CREATE_FILE
from ._delete_file import DELETE_FILE
from ._disable_autonomous_mode import DISABLE_AUTONOMOUS_MODE
from ._edit_file import EDIT_FILE
from ._escalate_blocker import ESCALATE_BLOCKER
from ._finalize_project import FINALIZE_PROJECT
from ._list_artifacts import LIST_ARTIFACTS
from ._move_file import MOVE_FILE
from ._post_update import POST_UPDATE
from ._publish_artifact import PUBLISH_ARTIFACT
from ._query_frontier import QUERY_FRONTIER
from ._read_artifact import READ_ARTIFACT
from ._report_artifact_completed import REPORT_ARTIFACT_COMPLETED
from ._request_user_review_artifact import REQUEST_USER_REVIEW_ARTIFACT
from ._rollback import ROLLBACK
from ._run_author_critic_iteration import RUN_AUTHOR_CRITIC_ITERATION
from ._run_command import RUN_COMMAND
from ._run_subagent import RUN_SUBAGENT
from ._spec import ToolSpec
from ._toolchain_build import TOOLCHAIN_BUILD
from ._toolchain_deps import TOOLCHAIN_DEPS
from ._toolchain_test import TOOLCHAIN_TEST

__all__ = [
    "ALL_TOOLS",
    "ASK_USER",
    "COPY_FILE",
    "CREATE_FILE",
    "DELETE_FILE",
    "DISABLE_AUTONOMOUS_MODE",
    "EDIT_FILE",
    "ESCALATE_BLOCKER",
    "FINALIZE_PROJECT",
    "LEAF_TOOLS_BY_NAME",
    "LIST_ARTIFACTS",
    "MOVE_FILE",
    "ORCHESTRATOR_ASK_USER",
    "POST_UPDATE",
    "PUBLISH_ARTIFACT",
    "QUERY_FRONTIER",
    "READ_ARTIFACT",
    "REPORT_ARTIFACT_COMPLETED",
    "REQUEST_USER_REVIEW_ARTIFACT",
    "ROLLBACK",
    "RUN_AUTHOR_CRITIC_ITERATION",
    "RUN_COMMAND",
    "RUN_SUBAGENT",
    "TOOLCHAIN_BUILD",
    "TOOLCHAIN_DEPS",
    "TOOLCHAIN_TEST",
    "ToolSpec",
]

# Tool specs a leaf sub-agent may be granted, keyed by name.
LEAF_TOOLS_BY_NAME: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        PUBLISH_ARTIFACT,
        READ_ARTIFACT,
        ESCALATE_BLOCKER,
        ASK_USER,
        REQUEST_USER_REVIEW_ARTIFACT,
        REPORT_ARTIFACT_COMPLETED,
        CREATE_FILE,
        EDIT_FILE,
        DELETE_FILE,
        COPY_FILE,
        MOVE_FILE,
        RUN_COMMAND,
    )
}

# Every tool spec in the catalog, including both `ask_user` variants. Used by
# kodo.subagents._registry to resolve external_name for the rendered
# `## Tools` section without needing to know which specs are leaf vs.
# orchestrator.
ALL_TOOLS: tuple[ToolSpec, ...] = (
    ASK_USER,
    ORCHESTRATOR_ASK_USER,
    COPY_FILE,
    CREATE_FILE,
    DELETE_FILE,
    DISABLE_AUTONOMOUS_MODE,
    EDIT_FILE,
    ESCALATE_BLOCKER,
    FINALIZE_PROJECT,
    LIST_ARTIFACTS,
    MOVE_FILE,
    POST_UPDATE,
    PUBLISH_ARTIFACT,
    QUERY_FRONTIER,
    READ_ARTIFACT,
    REPORT_ARTIFACT_COMPLETED,
    REQUEST_USER_REVIEW_ARTIFACT,
    ROLLBACK,
    RUN_AUTHOR_CRITIC_ITERATION,
    RUN_COMMAND,
    RUN_SUBAGENT,
    TOOLCHAIN_BUILD,
    TOOLCHAIN_DEPS,
    TOOLCHAIN_TEST,
)
