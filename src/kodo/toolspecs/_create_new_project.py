"""``create_new_project`` tool spec — scaffold a brand-new project folder.

Dispatch lives in :mod:`kodo.tools`, which delegates to the engine
(``EngineServices.create_project``/``bootstrap_project``): the engine slugifies
the requested name into a filesystem-safe directory name, creates that
directory under the workspace root (auto-suffixing ``-2``/``-3``… on
collision), lays out the standard ``specs/``, ``src/``, ``test/`` and
``.kodo/`` (with ``kodo.md``) structure via ``ProjectLayout.init`` and an
initial git checkpoint mirror, and asks the VS Code extension to add the new
directory to the open workspace so the agent can work inside it right away.

The input shape deliberately has no ``path`` property: the agent never picks
the on-disk location, only a human-readable ``name`` (or nothing at all, in
the no-workspace bootstrap fork below) — an absolute filesystem path is
supplied only by the engine itself or by a real user action (the native
"Create Project" folder-picker dialog, wired straight to
``EngineServices.create_project`` outside this tool). Keeping the LLM-facing
schema name-only closes off a path-injection surface: nothing the model
writes can ever place a project at an arbitrary location.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
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
        "appended so an existing project is never touched. You cannot choose the "
        "exact on-disk location yourself — there is no 'path' input; if the user "
        "named a concrete folder, mention it to them but the directory is still "
        "placed by 'name' under the workspace root (or, with no workspace yet, "
        "by the bootstrap fork below). If no project/workspace exists yet in "
        "this session, you may call this with no 'name' at all: in an "
        "interactive session the user is asked to pick (or create) a folder via "
        "a dialog; in an autonomous session a name is invented automatically "
        "and the project is created under ~/kodo-projects/ without asking "
        "anyone. Inside the new directory it lays out the standard `specs/`, "
        "`src/`, `test/` and `.kodo/` (with `kodo.md`) structure and an initial "
        "git checkpoint mirror, then adds the directory to the open VS Code "
        "workspace. Returns the absolute 'path' of the created project and its "
        "workspace 'name'. After calling this you can immediately read and "
        "write files inside the returned path (call `get_root_paths` to see it "
        "listed as a workspace root)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "name": {
                "type": "string",
                "description": (
                    "Human-readable name of the project to create (e.g. "
                    "'My Todo App'). Used both as the workspace-folder label and "
                    "as the basis for the on-disk directory name. May be omitted "
                    "only when no project/workspace exists yet in this session."
                ),
            },
        },
        "required": ["intent"],
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
    security_impact=SecurityImpact.LOW,
    input_visibility={"intent": "always", "name": "always"},
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
