"""``toolchain_deps`` tool — add/remove/update a single project dependency.

This tool does not touch manifests or lockfiles itself. It spawns the
``toolchain_depsmgr`` sub-agent (via the engine's dedicated, ungated
``run_dependency_manager`` service — holding this tool *is* the authorization, so
the sub-agent never needs to sit in any caller's ``run_subagent`` allow-list) and
maps that sub-agent's structured result back onto the tool's output schema.

The one outcome that needs translation is a missing ``DEPENDENCIES.md``: the
sub-agent reports ``status: "dependencies_md_missing"`` and changes nothing, and
this tool turns that into a ``message`` telling the caller how to get one
generated (run the toolchain-setup sub-agent) before retrying — exactly the
error-forwarding the schemas exist to support.

The caller's mandatory ``project_root_path`` is required here too (before the
sub-agent is even spawned) and forwarded verbatim into its task input — the
sub-agent operates only inside that root and never discovers it on its own.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["ToolchainDepsTool"]

_log = logging.getLogger(__name__)

# Returned (with success=False) when the sub-agent reports no DEPENDENCIES.md.
# A step-by-step sub-prompt so the calling agent can recover on its own.
_REMEDIATION = (
    "No DEPENDENCIES.md exists at the project root, so dependency management is "
    "not set up yet — nothing was changed. To enable it:\n"
    "1. Run the toolchain-setup sub-agent for this project's language via "
    '`run_subagent` (e.g. `toolchain_python` for Python). Pass `mode: "bootstrap"` '
    'for a fresh project or `mode: "convert"` for an existing one.\n'
    "2. It generates DEPENDENCIES.md (the dependency contract: the manager, the "
    "dependency kinds, and the add/remove/update commands) alongside the build "
    "scripts.\n"
    "3. Once DEPENDENCIES.md exists, retry `toolchain_deps`."
)


class ToolchainDepsTool(Tool):
    """Delegate one dependency operation to the ``toolchain_depsmgr`` sub-agent."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        project_root_path = str(tool_input.get("project_root_path", "")).strip()
        action = str(tool_input.get("action", "")).strip()
        name = str(tool_input.get("name", "")).strip()
        if not project_root_path or not action or not name:
            return json.dumps(
                {
                    "success": False,
                    "status": "failed",
                    "message": (
                        "toolchain_deps requires `project_root_path`, `action`, and `name`."
                    ),
                }
            )

        task_input = self.__build_task_input(tool_input, project_root_path, action, name)
        result = await self.context.services.run_dependency_manager(task_input)

        status = str(result.get("status", "")).strip()
        summary = str(result.get("summary", "")).strip()
        commands = result.get("commands_run")
        files = result.get("files_changed")

        if status == "dependencies_md_missing":
            message = _REMEDIATION
            if summary:
                message = f"{summary}\n\n{message}"
            return json.dumps(
                {"success": False, "status": "dependencies_md_missing", "message": message}
            )

        if status == "completed":
            payload: dict[str, object] = {
                "success": True,
                "status": "completed",
                "message": summary or f"{action} {name}: done.",
            }
            if isinstance(commands, list):
                payload["commands_run"] = commands
            if isinstance(files, list):
                payload["files_changed"] = files
            return json.dumps(payload)

        # status == "failed", or the sub-agent returned nothing usable.
        message = summary or "Dependency operation could not be completed."
        out: dict[str, object] = {"success": False, "status": "failed", "message": message}
        if isinstance(commands, list):
            out["commands_run"] = commands
        if isinstance(files, list):
            out["files_changed"] = files
        return json.dumps(out)

    @staticmethod
    def __build_task_input(
        tool_input: dict[str, object], project_root_path: str, action: str, name: str
    ) -> dict[str, object]:
        """Render the caller's request into the sub-agent's ``input_schema`` task."""
        version = str(tool_input.get("version", "")).strip()
        kind = str(tool_input.get("kind", "")).strip() or "runtime"
        extra = str(tool_input.get("extra", "")).strip()

        verb = {"add": "Add", "remove": "Remove", "update": "Update"}.get(action, action)
        parts = [f"{verb} the `{kind}` dependency `{name}`"]
        if version:
            parts.append(f"at version `{version}`")
        if extra:
            parts.append(f"in the optional extras group `{extra}`")
        instructions = (
            " ".join(parts) + f", by following the project's DEPENDENCIES.md at "
            f"`{project_root_path}`. If DEPENDENCIES.md is missing, report status "
            "`dependencies_md_missing` and change nothing."
        )

        task: dict[str, object] = {
            "instructions": instructions,
            "project_root_path": project_root_path,
            "action": action,
            "name": name,
            "kind": kind,
        }
        if version:
            task["version"] = version
        if extra:
            task["extra"] = extra
        return task
