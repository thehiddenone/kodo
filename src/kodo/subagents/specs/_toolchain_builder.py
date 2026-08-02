"""SubAgentSpec for ``toolchain_builder`` (standalone solo; writes files, no artifacts)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["TOOLCHAIN_BUILDER"]


TOOLCHAIN_BUILDER: SubAgentSpec = SubAgentSpec(
    name="toolchain_builder",
    input_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "description": "What to set up or change.",
            },
            "project_path": {
                "type": "string",
                "description": (
                    "Path to the project root directory. Relative to the current "
                    "workspace, or absolute if the project lives outside the workspace."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["bootstrap", "convert"],
                "description": (
                    "Expected job: bootstrap = fresh project; convert = bring an "
                    "existing project into the Kodo build model. A hint only - the "
                    "agent detects the real state on disk and reports it as mode_used."
                ),
            },
            "language": {
                "type": "string",
                "description": (
                    "The project's language/ecosystem when the caller knows it "
                    "(e.g. 'python', 'go', 'typescript'). Optional; the agent "
                    "detects it otherwise and verifies whatever is supplied."
                ),
            },
            "project_code": {
                "type": "string",
                "description": "PROJECTCODE for context (optional).",
            },
        },
        "required": ["instructions", "project_path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "scripts_created": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filesystem paths to the build scripts written (scripts/build.sh).",
            },
            "development_md_path": {
                "type": "string",
                "description": "Filesystem path to the DEVELOPMENT.md written.",
            },
            "dependencies_md_path": {
                "type": ["string", "null"],
                "description": (
                    "Filesystem path to the DEPENDENCIES.md written, or null when the "
                    "project has no dependencies to manage."
                ),
            },
            "ecosystem": {
                "type": "string",
                "description": (
                    "The language/ecosystem the toolchain was set up for, as detected "
                    "on disk (e.g. 'python', 'typescript', 'rust', 'c++', 'go')."
                ),
            },
            "toolchain": {
                "type": ["string", "null"],
                "description": (
                    "One line naming the tools the scripts drive: package manager, "
                    "build, format, static-analysis, and test tools."
                ),
            },
            "mode_used": {
                "type": ["string", "null"],
                "description": (
                    "The job actually performed, decided from the state on disk: "
                    "'bootstrap' or 'convert'. Differs from the requested mode when "
                    "the caller's expectation was wrong."
                ),
            },
            "manifest_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Paths to the manifest/config files created or reused "
                    "(pyproject.toml, package.json, Cargo.toml, CMakeLists.txt, ...)."
                ),
            },
            "verification": {
                "type": ["string", "null"],
                "description": (
                    "What was run to verify the scripts and the outcome, including any "
                    "step that could not be verified and why."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One line: what was set up.",
            },
        },
        "required": ["scripts_created", "development_md_path", "ecosystem", "summary"],
    },
)
