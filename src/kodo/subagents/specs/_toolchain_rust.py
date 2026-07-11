"""SubAgentSpec for ``toolchain_rust`` (standalone solo; writes files, no artifacts)."""

from __future__ import annotations

from .._subagentspec import SubAgentSpec

__all__ = ["TOOLCHAIN_RUST"]


TOOLCHAIN_RUST: SubAgentSpec = SubAgentSpec(
    name="toolchain_rust",
    description=(
        "Sets up the five standard build scripts + DEVELOPMENT.md (and DEPENDENCIES.md "
        "when the project has dependencies) for a Rust project, using Cargo."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "type": "string",
                "description": "What to set up or change.",
            },
            "mode": {
                "type": "string",
                "enum": ["bootstrap", "convert"],
                "description": (
                    "bootstrap = fresh project; convert = bring an existing project "
                    "into the Kodo build model."
                ),
            },
            "project_code": {
                "type": "string",
                "description": "PROJECTCODE for context (optional).",
            },
        },
        "required": ["instructions", "mode"],
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
            "cargo_toml_path": {
                "type": ["string", "null"],
                "description": "Path to the root Cargo.toml created or reused, or null if none.",
            },
            "summary": {
                "type": "string",
                "description": "One line: what was set up.",
            },
        },
        "required": ["scripts_created", "development_md_path", "summary"],
    },
)
