"""``toolchain_deps`` tool spec.

The caller-facing surface for changing project dependencies. The dispatch handler
(:mod:`kodo.tools._toolchain_deps`) does not touch manifests itself — it spawns
the ``toolchain_depsmgr`` sub-agent, which executes the project's
``DEPENDENCIES.md``. When no ``DEPENDENCIES.md`` exists, the sub-agent reports
``dependencies_md_missing`` and the tool returns a ``status`` of the same name
plus a remediation ``message`` telling the caller how to get one generated (run
the toolchain-setup sub-agent), so the caller can recover rather than seeing a
bare failure.

``project_root_path`` is required (mirrors ``toolchain_build``'s mandatory
``project_path``) and is forwarded verbatim into the sub-agent's task input,
which is itself required there too — the sub-agent never discovers its own
project root, so the caller must always name one.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["TOOLCHAIN_DEPS"]


TOOLCHAIN_DEPS: ToolSpec = ToolSpec(
    name="toolchain_deps",
    external_name="Manage Dependencies",
    user_description="Manage project dependencies",
    description=(
        "Add, remove, or update a single project dependency. The only sanctioned "
        "way to change dependency files — agents do not edit manifests or "
        "lockfiles directly. The work is performed by the dependency-management "
        "sub-agent, which follows the project's `DEPENDENCIES.md`.\n\n"
        "Inputs: `project_root_path` (required — absolute path of the project "
        "root to operate on; the sub-agent never touches anything outside it), "
        "`action` (add | remove | update), `name` (the package), optional "
        "`version` (constraint; omit for latest), optional `kind` (which "
        "dependency category — `runtime` (default), `dev`, `test`, `optional`, or "
        "`build`), and optional `extra` (the extras group, for `kind: optional`).\n\n"
        "Returns `success`, a `status`, and a `message`. A `status` of "
        "`dependencies_md_missing` means the project has no `DEPENDENCIES.md` yet: "
        "nothing was changed, and `message` explains how to get one generated "
        "(run the toolchain-setup sub-agent to bootstrap/convert the project) "
        "before retrying."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_root_path": {
                "type": "string",
                "description": (
                    "Absolute path of the project root to operate on (the directory "
                    "holding, or that should hold, DEPENDENCIES.md)."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["add", "remove", "update"],
                "description": "Dependency operation to perform.",
            },
            "name": {"type": "string", "description": "Dependency package name."},
            "version": {
                "type": "string",
                "description": "Version constraint; omit for the latest/unpinned.",
            },
            "kind": {
                "type": "string",
                "enum": ["runtime", "dev", "test", "optional", "build"],
                "description": (
                    "Dependency category. Defaults to `runtime`. Use `dev` for "
                    "development-only tools, `test` for test-only deps, `optional` "
                    "for an opt-in feature/extra, `build` for build-backend "
                    "requirements."
                ),
            },
            "extra": {
                "type": "string",
                "description": "Extras/optional-feature group name (only for `kind: optional`).",
            },
        },
        "required": ["project_root_path", "action", "name"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "success": {"type": "boolean", "description": "Whether the operation succeeded."},
            "status": {
                "type": "string",
                "enum": ["completed", "dependencies_md_missing", "failed"],
                "description": (
                    "completed = applied and verified; dependencies_md_missing = no "
                    "DEPENDENCIES.md, nothing changed (see `message` for remediation); "
                    "failed = DEPENDENCIES.md present but the operation could not complete."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Human-readable result detail. For dependencies_md_missing, a "
                    "step-by-step remediation telling the caller how to get a "
                    "DEPENDENCIES.md generated before retrying."
                ),
            },
            "commands_run": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The DEPENDENCIES.md commands executed, in order.",
            },
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Manifest/lockfile paths the operation modified.",
            },
        },
        "required": ["success", "status", "message"],
    },
    security_impact=SecurityImpact.HIGH,
    input_visibility={
        "project_root_path": "always",
        "action": "always",
        "name": "always",
        "version": "visible",
        "kind": "visible",
        "extra": "visible",
    },
    output_visibility={
        "success": "always",
        "status": "always",
        "message": "visible",
        "commands_run": "visible",
        "files_changed": "visible",
    },
    when_to_use=(
        "A new library (database driver, HTTP client, message queue "
        "client, parser, etc.) is needed before referencing it in an "
        "implementation.",
        "A dependency is no longer needed and should be removed, or an "
        "existing dependency needs a version bump required by the "
        "implementation.",
    ),
)
