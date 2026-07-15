"""``init_project`` tool spec — add Kodo's git mirror to an existing folder.

Dispatch lives in :mod:`kodo.tools`, which delegates to the engine
(``EngineServices.init_project``): the engine judges whether the given
directory is empty (ignoring dotfiles/dot-directories), lays out
``specs/``/``src/``/``test/`` only when it is, always scaffolds ``.kodo/``
(with ``kodo.md``) and an initial git checkpoint mirror commit, and — unless
the directory is already part of the open workspace — asks the VS Code
extension to add it so the agent can work inside it right away. Unlike
``create_new_project``, the target directory must already exist and its
pre-existing content (if any) is never touched.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["INIT_PROJECT"]


INIT_PROJECT: ToolSpec = ToolSpec(
    name="init_project",
    external_name="Init Project",
    user_description="Add Kodo's git mirror to an existing folder",
    description=(
        "Bring an *existing* directory under Kodo's checkpoint tracking "
        "without disturbing whatever it already contains. Give the absolute "
        "'path' of a directory that already exists on disk — this tool never "
        "creates the directory itself (use `create_new_project` for a "
        "brand-new one). It lists the directory's contents: if it has no "
        "entries, or only entries whose name starts with a dot (e.g. "
        "'.git/', '.gitignore'), the directory is treated as empty and the "
        "standard `specs/`, `src/`, `test/` layout is created, exactly as "
        "`create_new_project` would; if it already holds real content, those "
        "directories are NOT created and nothing existing is touched. Either "
        "way, `.kodo/` (with `kodo.md`) is always created and the checkpoint "
        "git mirror is initialised with its mandatory first commit before "
        "the call returns. If the directory is not already part of the open "
        "workspace it is added, same as `create_new_project`; if it's "
        "already open, the workspace is left as-is. Fails if the directory "
        "does not exist, or if it already has a `.kodo/` (it is already a "
        "Kodo project — there is nothing to initialise). Returns the "
        "absolute 'path', its workspace 'name', and whether the standard "
        "layout was 'scaffolded'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "path": {
                "type": "string",
                "description": (
                    "Absolute path of the existing directory to bring under "
                    "Kodo's tracking. It must already exist on disk; "
                    "init_project never creates it."
                ),
            },
        },
        "required": ["intent", "path"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the augmented project directory.",
            },
            "name": {
                "type": "string",
                "description": "Workspace-folder label the directory is registered under.",
            },
            "scaffolded": {
                "type": "boolean",
                "description": (
                    "True if the directory was judged empty and the standard "
                    "specs/, src/, test/ layout was created; false if it "
                    "already had content and only .kodo/ was added."
                ),
            },
        },
        "required": ["path", "name", "scaffolded"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={"intent": "always", "path": "always"},
    output_visibility={"path": "always", "name": "always", "scaffolded": "always"},
    when_to_use=(
        "When the work calls for bringing an existing, not-yet-tracked "
        "directory under Kodo's checkpoint mirror — e.g. the user points at "
        "a project that already has code and just wants Kodo's git-mirror "
        "checkpointing on top of it.",
        "To obtain a checkpoint-tracked directory that is already part of "
        "the workspace without risking any of its existing files — unlike "
        "`create_new_project`, which is for a brand-new, empty project.",
    ),
)
