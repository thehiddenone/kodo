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
from ._create_directory import CREATE_DIRECTORY
from ._create_file import CREATE_FILE
from ._create_new_project import CREATE_NEW_PROJECT
from ._disable_autonomous_mode import DISABLE_AUTONOMOUS_MODE
from ._document_feedback import DOCUMENT_FEEDBACK
from ._edit_file import EDIT_FILE
from ._escalate_blocker import ESCALATE_BLOCKER
from ._filesystem import FILESYSTEM
from ._finalize_project import FINALIZE_PROJECT
from ._find_files import FIND_FILES
from ._find_text_in_files import FIND_TEXT_IN_FILES
from ._get_root_paths import GET_ROOT_PATHS
from ._get_web_search_state import GET_WEB_SEARCH_STATE
from ._guided_dev_status import GUIDED_DEV_STATUS
from ._init_project import INIT_PROJECT
from ._intent import INTENT_KEY, INTENT_PROPERTY, requires_intent
from ._query_search_engine import QUERY_SEARCH_ENGINE
from ._read_attachment import READ_ATTACHMENT
from ._read_file import READ_FILE
from ._read_webpage import READ_WEBPAGE
from ._remaining_time import REMAINING_TIME
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
from ._submit_evaluation import SUBMIT_EVALUATION
from ._toolchain_build import TOOLCHAIN_BUILD
from ._toolchain_deps import TOOLCHAIN_DEPS
from ._update_web_search_state import UPDATE_WEB_SEARCH_STATE
from ._visibility import build_detail_rows, stringify_value
from ._wait import WAIT
from ._web_search import WEB_SEARCH
from ._workspace import NO_PROJECT_ERROR

__all__ = [
    "ALL_TOOLS",
    "ASK_USER",
    "CREATE_DIRECTORY",
    "CREATE_FILE",
    "CREATE_NEW_PROJECT",
    "DISABLE_AUTONOMOUS_MODE",
    "DOCUMENT_FEEDBACK",
    "EDIT_FILE",
    "ESCALATE_BLOCKER",
    "FILESYSTEM",
    "FINALIZE_PROJECT",
    "FIND_FILES",
    "FIND_TEXT_IN_FILES",
    "GET_ROOT_PATHS",
    "GET_WEB_SEARCH_STATE",
    "GUIDED_DEV_STATUS",
    "INIT_PROJECT",
    "INTENT_KEY",
    "INTENT_PROPERTY",
    "NO_PROJECT_ERROR",
    "OUTPUT_VISIBILITY_DEFAULT",
    "QUERY_SEARCH_ENGINE",
    "READ_ATTACHMENT",
    "READ_FILE",
    "READ_WEBPAGE",
    "REMAINING_TIME",
    "RETURN_RESULT",
    "ROLLBACK",
    "RUN_AUTHOR_CRITIC_ITERATION",
    "RUN_COMMAND",
    "RUN_SUBAGENT",
    "SCHEMA_COMPLIANCE_KEY",
    "SUBMIT_EVALUATION",
    "TOOLCHAIN_BUILD",
    "TOOLCHAIN_DEPS",
    "UPDATE_WEB_SEARCH_STATE",
    "VISIBILITY_ALWAYS",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_VALUES",
    "VISIBILITY_VISIBLE",
    "WAIT",
    "WEB_SEARCH",
    "SecurityImpact",
    "ToolSpec",
    "augment_output_schema",
    "build_detail_rows",
    "normalize_output",
    "requires_intent",
    "stringify_value",
    "tool_result_succeeded",
]

# Every tool spec in the catalog. Used by kodo.subagents._registry to render
# the `## Tools` section of each agent prompt.
ALL_TOOLS: tuple[ToolSpec, ...] = (
    ASK_USER,
    CREATE_DIRECTORY,
    CREATE_FILE,
    CREATE_NEW_PROJECT,
    DISABLE_AUTONOMOUS_MODE,
    DOCUMENT_FEEDBACK,
    EDIT_FILE,
    ESCALATE_BLOCKER,
    FILESYSTEM,
    FINALIZE_PROJECT,
    GET_ROOT_PATHS,
    FIND_FILES,
    FIND_TEXT_IN_FILES,
    GET_WEB_SEARCH_STATE,
    GUIDED_DEV_STATUS,
    INIT_PROJECT,
    QUERY_SEARCH_ENGINE,
    READ_ATTACHMENT,
    READ_FILE,
    READ_WEBPAGE,
    REMAINING_TIME,
    RETURN_RESULT,
    ROLLBACK,
    RUN_AUTHOR_CRITIC_ITERATION,
    RUN_COMMAND,
    RUN_SUBAGENT,
    SUBMIT_EVALUATION,
    TOOLCHAIN_BUILD,
    TOOLCHAIN_DEPS,
    UPDATE_WEB_SEARCH_STATE,
    WAIT,
    WEB_SEARCH,
)
