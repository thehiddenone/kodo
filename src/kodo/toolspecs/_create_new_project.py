"""``create_new_project`` tool spec — scaffold a brand-new project folder.

Dispatch lives in :mod:`kodo.tools`, which delegates to the engine
(``EngineServices.create_project``): the engine slugifies the requested name
into a filesystem-safe directory name, creates that directory under the session
workspace root (auto-suffixing ``-2``/``-3``… on collision), lays out the
standard ``specs/``, ``src/``, ``test/`` and ``.kodo/`` (with ``kodo.md``)
structure via ``ProjectLayout.init`` and an initial git checkpoint mirror, and
asks the VS Code extension to add the new directory to the open workspace so
the agent can work inside it right away.
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["CREATE_NEW_PROJECT"]


CREATE_NEW_PROJECT: ToolSpec = ToolSpec(
    name="create_new_project",
    external_name="Create New Project",
    user_description="Create a new project folder",
    description=(
        "Create a brand-new, empty project and add it to the workspace. Give a "
        "human-readable project 'name'; the tool derives a filesystem-safe "
        "directory name from it (lowercased, spaces and unsafe characters turned "
        "into dashes) and creates that directory under the workspace root — if a "
        "directory of that name already exists, a numeric suffix (-2, -3, …) is "
        "appended so an existing project is never touched. Pass an absolute "
        "'path' instead to lay the project out in that exact directory "
        "(supersedes 'name' — only use this when the user has specified a "
        "concrete location). Either 'name' or 'path' must be given. Inside the "
        "new directory it lays out the standard `specs/`, `src/`, `test/` and "
        "`.kodo/` (with `kodo.md`) structure and an initial git checkpoint "
        "mirror, then adds the directory to the open VS Code workspace. Returns "
        "the absolute 'path' of the created project and "
        "its workspace 'name'. After calling this you can immediately read and "
        "write files inside the returned path (call `get_root_paths` to see it "
        "listed as a workspace root)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Human-readable name of the project to create (e.g. "
                    "'My Todo App'). Used both as the workspace-folder label and, "
                    "when 'path' is omitted, as the basis for the on-disk "
                    "directory name."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Absolute directory to lay the project out in, superseding "
                    "'name' for the on-disk location. The directory need not "
                    "exist yet. Only set this when a concrete location was "
                    "specified; otherwise omit it and use 'name'."
                ),
            },
        },
        "required": [],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the newly created project directory.",
            },
            "name": {
                "type": "string",
                "description": "Workspace-folder label the new project was added under.",
            },
        },
        "required": ["path", "name"],
    },
    security_impact=SecurityImpact.MODERATE,
    input_visibility={"name": "always", "path": "always"},
    output_visibility={"path": "always", "name": "always"},
    when_to_use=(
        "When the work calls for a brand-new, self-contained project rather than "
        "changes to an existing one — e.g. the user asks to build a new "
        "application or library from scratch and there is no suitable project "
        "directory yet.",
        "To obtain a fresh, checkpoint-tracked directory that is already part of "
        "the workspace, so subsequent file edits and commands have somewhere to "
        "live.",
    ),
)
