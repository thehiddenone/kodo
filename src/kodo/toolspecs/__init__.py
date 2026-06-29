"""Tool specifications for every tool Kōdo's LLM-driven agents may call.

This package contains **only** :class:`ToolSpec` (the dataclass) and
``ToolSpec`` catalog entries — one module per tool, named ``_<tool_name>.py``,
each exporting a single module-level ``ToolSpec`` constant (e.g.
``_filesystem.py`` exports ``FILESYSTEM``). When adding a new tool, add a
new ``_<tool_name>.py`` module here rather than extending an existing one.

No dispatch or implementation logic lives here. Every tool's dispatch lives in
:mod:`kodo.tools` (one ``_<tool_name>.py`` handler module per tool), routed
through a single :class:`kodo.tools.ToolDispatcher`.
"""

from __future__ import annotations

from ._ask_user import ASK_USER
from ._compliance import (
    SCHEMA_COMPLIANCE_KEY,
    augment_output_schema,
    normalize_output,
    tool_result_succeeded,
)
from ._create_new_project import CREATE_NEW_PROJECT
from ._disable_autonomous_mode import DISABLE_AUTONOMOUS_MODE
from ._edit_file import EDIT_FILE
from ._escalate_blocker import ESCALATE_BLOCKER
from ._filesystem import FILESYSTEM
from ._finalize_project import FINALIZE_PROJECT
from ._find_files import FIND_FILES
from ._find_text_in_files import FIND_TEXT_IN_FILES
from ._get_root_paths import GET_ROOT_PATHS
from ._list_artifacts import LIST_ARTIFACTS
from ._publish_artifact import PUBLISH_ARTIFACT
from ._query_frontier import QUERY_FRONTIER
from ._read_artifact import READ_ARTIFACT
from ._report_artifact_completed import REPORT_ARTIFACT_COMPLETED
from ._request_user_review_artifact import REQUEST_USER_REVIEW_ARTIFACT
from ._return_result import RETURN_RESULT
from ._rollback import ROLLBACK
from ._run_author_critic_iteration import RUN_AUTHOR_CRITIC_ITERATION
from ._run_command import RUN_COMMAND
from ._run_subagent import RUN_SUBAGENT
from ._spec import (
    OUTPUT_VISIBILITY_DEFAULT,
    VISIBILITY_ALWAYS,
    VISIBILITY_HIDDEN,
    VISIBILITY_VALUES,
    VISIBILITY_VISIBLE,
    SecurityImpact,
    ToolSpec,
)
from ._toolchain_build import TOOLCHAIN_BUILD
from ._toolchain_deps import TOOLCHAIN_DEPS
from ._visibility import build_detail_rows, stringify_value

__all__ = [
    "ALL_TOOLS",
    "ASK_USER",
    "CREATE_NEW_PROJECT",
    "DISABLE_AUTONOMOUS_MODE",
    "EDIT_FILE",
    "ESCALATE_BLOCKER",
    "FILESYSTEM",
    "FINALIZE_PROJECT",
    "FIND_FILES",
    "FIND_TEXT_IN_FILES",
    "GET_ROOT_PATHS",
    "LIST_ARTIFACTS",
    "OUTPUT_VISIBILITY_DEFAULT",
    "PUBLISH_ARTIFACT",
    "QUERY_FRONTIER",
    "READ_ARTIFACT",
    "REPORT_ARTIFACT_COMPLETED",
    "REQUEST_USER_REVIEW_ARTIFACT",
    "RETURN_RESULT",
    "ROLLBACK",
    "RUN_AUTHOR_CRITIC_ITERATION",
    "RUN_COMMAND",
    "RUN_SUBAGENT",
    "SCHEMA_COMPLIANCE_KEY",
    "TOOLCHAIN_BUILD",
    "TOOLCHAIN_DEPS",
    "VISIBILITY_ALWAYS",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_VALUES",
    "VISIBILITY_VISIBLE",
    "SecurityImpact",
    "ToolSpec",
    "augment_output_schema",
    "build_detail_rows",
    "normalize_output",
    "stringify_value",
    "tool_result_succeeded",
]

# Every tool spec in the catalog. Used by kodo.subagents._registry to render
# the `## Tools` section of each agent prompt.
ALL_TOOLS: tuple[ToolSpec, ...] = (
    ASK_USER,
    CREATE_NEW_PROJECT,
    DISABLE_AUTONOMOUS_MODE,
    EDIT_FILE,
    ESCALATE_BLOCKER,
    FILESYSTEM,
    FINALIZE_PROJECT,
    GET_ROOT_PATHS,
    FIND_FILES,
    FIND_TEXT_IN_FILES,
    LIST_ARTIFACTS,
    PUBLISH_ARTIFACT,
    QUERY_FRONTIER,
    READ_ARTIFACT,
    REPORT_ARTIFACT_COMPLETED,
    REQUEST_USER_REVIEW_ARTIFACT,
    RETURN_RESULT,
    ROLLBACK,
    RUN_AUTHOR_CRITIC_ITERATION,
    RUN_COMMAND,
    RUN_SUBAGENT,
    TOOLCHAIN_BUILD,
    TOOLCHAIN_DEPS,
)
