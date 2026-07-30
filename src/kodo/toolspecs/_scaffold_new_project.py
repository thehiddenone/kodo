"""``scaffold_new_project`` tool spec — set up a directory as a Kodo project.

Merges the former ``create_new_project`` and ``init_project`` tools into one:
the purpose is always "make this directory a Kodo project", whether that
directory is brand new or already exists on disk.

Dispatch lives in :mod:`kodo.tools`, which delegates to the engine
(``EngineServices.create_project``/``init_project``/``bootstrap_project``
— unchanged engine primitives, chosen by which input the agent supplied):

* No ``path`` given: same as the former ``create_new_project``. Give a
  human-readable ``name``; the engine slugifies it into a filesystem-safe
  directory name and creates that directory under the workspace root
  (auto-suffixing ``-2``/``-3``… on collision), laying out the standard
  ``specs/``, ``src/``, ``test/`` and ``.kodo/`` (with ``kodo.md``)
  structure and an initial git checkpoint mirror. With no project/workspace
  bound yet, ``name`` may be omitted too — the no-workspace bootstrap fork
  below runs instead.
* ``path`` given and it exists on disk: same as the former ``init_project``.
  If the directory has no ``.kodo/`` yet, it is brought under Kodo's
  tracking in place — pre-existing content is never touched, only
  ``specs/``/``src/``/``test/`` are added when the directory was otherwise
  empty. If the directory already has a ``.kodo/`` (already a Kodo
  project), this is a no-op success — the tool just reports that
  scaffolding was already in place.
* ``path`` given but it does not exist on disk: an error. The agent never
  gets to choose an on-disk location for a *new* project — only ``name``
  can drive creation (see the "no path" property note below) — so a
  nonexistent ``path`` is invalid input, not a signal to create anything.

The input shape deliberately has no ``path``-drives-creation behavior: the
agent can never pick the on-disk location of a *newly created* project,
only a human-readable ``name`` (or nothing at all, in the no-workspace
bootstrap fork below) — an absolute filesystem path for a new project is
supplied only by the engine itself or by a real user action (the native
"Create Project" folder-picker dialog, wired straight to
``EngineServices.create_project`` outside this tool). Keeping *creation*
name-only closes off a path-injection surface: nothing the model writes can
ever place a *new* project at an arbitrary location. ``path`` is only ever
used to point at something that must already exist on disk.
"""

from __future__ import annotations

from ._intent import INTENT_PROPERTY
from ._spec import SecurityImpact, ToolSpec

__all__ = ["SCAFFOLD_NEW_PROJECT"]


SCAFFOLD_NEW_PROJECT: ToolSpec = ToolSpec(
    name="scaffold_new_project",
    external_name="Scaffold New Project",
    user_description="Set up a directory as a Kodo project",
    description=(
        "Set up a directory as a Kodo project — either a brand-new directory "
        "or an existing one — and add it to the workspace. Which of those "
        "happens is decided by whether you pass 'path':\n\n"
        "1. No 'path' (create a brand-new project): give a human-readable "
        "'name'; the tool derives a filesystem-safe directory name from it "
        "(lowercased, spaces and unsafe characters turned into dashes) and "
        "creates that directory under the workspace root — if a directory "
        "of that name already exists, a numeric suffix (-2, -3, …) is "
        "appended so an existing project is never touched. You cannot "
        "choose the exact on-disk location yourself for a new project — if "
        "the user named a concrete folder, mention it to them but the "
        "directory is still placed by 'name' under the workspace root (or, "
        "with no workspace yet, by the bootstrap fork below). If no "
        "project/workspace exists yet in this session, you may call this "
        "with no 'name' at all either: in an interactive session the user "
        "is asked to pick (or create) a folder via a dialog; in an "
        "autonomous session a name is invented automatically and the "
        "project is created under ~/kodo-projects/ without asking anyone. "
        "Inside the new directory it lays out the standard `specs/`, "
        "`src/`, `test/` and `.kodo/` (with `kodo.md`) structure and an "
        "initial git checkpoint mirror, then adds the directory to the "
        "open VS Code workspace.\n\n"
        "2. 'path' given and it already exists on disk (bring an existing "
        "directory under Kodo's tracking): give the absolute 'path' of a "
        "directory that already exists — this tool never creates the "
        "directory itself in this case (omit 'path' for a brand-new one, "
        "per case 1). If it has no `.kodo/` yet, it is scaffolded in place "
        "without disturbing whatever it already contains: the directory's "
        "contents are listed, and if it has no entries (or only entries "
        "whose name starts with a dot, e.g. '.git/', '.gitignore') it's "
        "treated as empty and the standard `specs/`, `src/`, `test/` "
        "layout is created exactly as in case 1; if it already holds real "
        "content, those directories are NOT created and nothing existing "
        "is touched. Either way `.kodo/` (with `kodo.md`) is created and "
        "the checkpoint git mirror is initialised with its mandatory first "
        "commit before the call returns, and the directory is added to the "
        "open workspace unless it's already part of it. If the directory "
        "already has a `.kodo/` — it's already a Kodo project — this call "
        "just succeeds as a no-op (check the 'already_scaffolded' output) "
        "instead of erroring.\n\n"
        "3. 'path' given but it does NOT exist on disk: fails with an "
        "error. 'path' only ever points at something already there; it is "
        "never a location to create a new project at.\n\n"
        "After calling this you can immediately read and write files "
        "inside the returned path (call `get_root_paths` to see it listed "
        "as a workspace root).\n\n"
        "When to use: any time a directory needs to become (or already is) "
        "a checkpoint-tracked Kodo project — a brand-new, self-contained "
        "project built from scratch (omit 'path'), or an existing directory "
        "that already holds code and just needs Kodo's git-mirror "
        "checkpointing on top of it (pass 'path'). Safe to call again on a "
        "directory you already scaffolded — it just confirms scaffolding "
        "is in place rather than erroring."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "intent": INTENT_PROPERTY,
            "name": {
                "type": "string",
                "description": (
                    "Human-readable name of the project to create (e.g. "
                    "'My Todo App'). Used only when 'path' is omitted: as "
                    "both the workspace-folder label and the basis for the "
                    "on-disk directory name of a brand-new project. May be "
                    "omitted only when no project/workspace exists yet in "
                    "this session. Ignored when 'path' is given."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Absolute path of an existing directory to bring under "
                    "Kodo's tracking (or confirm is already tracked). It "
                    "must already exist on disk — this tool never creates a "
                    "directory at an agent-chosen path; omit 'path' "
                    "entirely to create a brand-new project (see 'name')."
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
                "description": "Absolute path of the project directory.",
            },
            "name": {
                "type": "string",
                "description": "Workspace-folder label the project is registered under.",
            },
            "scaffolded": {
                "type": "boolean",
                "description": (
                    "True if the standard specs/, src/, test/ layout was "
                    "freshly created just now (always true when a brand-new "
                    "project was created, or when an existing empty "
                    "directory got the layout added). False if the "
                    "directory already had real content (only .kodo/ was "
                    "added) or was already a fully scaffolded Kodo project."
                ),
            },
            "already_scaffolded": {
                "type": "boolean",
                "description": (
                    "True if 'path' pointed at a directory that already had "
                    "a .kodo/ — already a Kodo project — so this call was a "
                    "no-op success and nothing was changed on disk."
                ),
            },
        },
        "required": ["path", "name", "scaffolded", "already_scaffolded"],
    },
    security_impact=SecurityImpact.LOW,
    input_visibility={"intent": "always", "name": "always", "path": "always"},
    output_visibility={
        "path": "always",
        "name": "always",
        "scaffolded": "always",
        "already_scaffolded": "always",
    },
)
