"""SubAgentSpec for ``toolchain_depsmgr`` (standalone solo; dependency manager).

The acting force behind the ``toolchain_deps`` tool: a language-agnostic agent
that executes one add/remove/update dependency operation by following the
project's ``DEPENDENCIES.md`` (the *Dependency Contract*, ``base_dependencies``).
Its ``output_schema`` carries a ``status`` whose ``dependencies_md_missing``
value is how it tells the tool the project has no ``DEPENDENCIES.md`` yet, so the
tool can hand the caller a remediation sub-prompt instead of a bare failure.

``project_root_path`` is a mandatory input: this agent never discovers or
infers which project to operate on (e.g. via ``get_root_paths``) — the caller
always hands it a concrete root, and it is a hard rule (see
``subagent_toolchain_depsmgr.md``) that every file/search/command it issues
stays inside that root.
"""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["TOOLCHAIN_DEPSMGR"]


TOOLCHAIN_DEPSMGR: SubAgentSpec = SubAgentSpec(
    name="toolchain_depsmgr",
    description=(
        "Executes one add/remove/update dependency operation for any toolchain by "
        "following the project's DEPENDENCIES.md."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "description": "Human-readable statement of the operation to perform.",
            },
            "project_root_path": {
                "type": "string",
                "description": (
                    "Absolute path to the project root to operate in — the directory "
                    "that holds (or should hold) DEPENDENCIES.md. The agent MUST NOT "
                    "read, search, or run commands outside this root."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["add", "remove", "update"],
                "description": "Dependency operation to perform.",
            },
            "name": {
                "type": "string",
                "description": "Dependency package/module name.",
            },
            "version": {
                "type": "string",
                "description": "Version or version constraint (empty/omitted = latest).",
            },
            "kind": {
                "type": "string",
                "enum": ["runtime", "dev", "test", "optional", "build"],
                "description": (
                    "Canonical dependency kind (DEPENDENCIES.md vocabulary). "
                    "Defaults to runtime when omitted."
                ),
            },
            "extra": {
                "type": "string",
                "description": "Extras/optional-feature group name (only for kind=optional).",
            },
        },
        "required": ["instructions", "project_root_path", "action", "name"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["completed", "dependencies_md_missing", "failed"],
                "description": (
                    "completed = operation applied and verified; "
                    "dependencies_md_missing = no DEPENDENCIES.md at the project root, "
                    "nothing was changed; failed = DEPENDENCIES.md was present but the "
                    "operation could not be completed."
                ),
            },
            "summary": {
                "type": "string",
                "description": ("One- or two-sentence result: what changed, or why it could not."),
            },
            "commands_run": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The DEPENDENCIES.md commands actually executed, in order.",
            },
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Manifest/lockfile paths the operation modified.",
            },
        },
        "required": ["status", "summary"],
    },
)
