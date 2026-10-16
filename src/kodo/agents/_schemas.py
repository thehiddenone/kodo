"""JSON Schemas for the two JSON files a user agent bundle holds (AGENT_BUILDER §4).

A user editing ``~/.kodo/agents/<name>/<name>.json`` (a top-level agent's
config) or ``~/.kodo/agents/subagents/<name>/<name>.json`` (a sub-agent's spec)
gets completion and inline errors from VS Code through these schemas. Neither
file can name its schema with ``$schema``: both loaders refuse unknown keys. So
the server writes the schemas to ``~/.kodo/etc/agent_schemas/`` at startup
(:func:`write_agent_schemas`) and kodo-vsix maps them onto those globs in the
user's ``json.schemas`` setting.

Generated, not hand-written, so they cannot drift from the loaders: the
property lists are built by iterating the loaders' own key sets
(:data:`~kodo.agents.TOP_AGENT_KEYS`, :data:`~kodo.agents.subagents.SPEC_KEYS`,
:data:`~kodo.agents.subagents.SPEC_SHAPE_ARGS`,
:data:`~kodo.agents.subagents.SPEC_CONSUMES_KEYS`). A key added to a loader
without a schema entry here raises ``KeyError`` the first time a schema is
built, which the tests do.

The schemas describe what loads **as a user agent**, which is stricter than
the bare loaders in three places the registry enforces: a user config may not
declare ``"default": true`` or ``"interactive": false``, a user name may not
take the reserved ``kodo_`` prefix or be ``subagents``, and a ``consumes``
scope must be a known one. Artifact roles stay open (the registry accepts a
role a user spec invents), so they are offered as completion ``examples`` only.

What a schema cannot check is left to the load check and the lint: ``name``
equal to the filename stem, a ``produces`` field that the output schema
declares, and every cross-agent rule.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ._topagent import TOP_AGENT_KEYS
from ._userstore import BUILTIN_NAME_PREFIX, SHARED_SUBAGENTS_DIRNAME
from .subagents import (
    ALL_ROLES,
    ALL_SCOPES,
    PRODUCES_REMAINDER,
    SPEC_CONSUMES_KEYS,
    SPEC_KEYS,
    SPEC_SHAPE_ARGS,
)

__all__ = [
    "AGENT_SCHEMAS_DIRNAME",
    "SUBAGENT_SPEC_SCHEMA_FILE",
    "TOP_AGENT_SCHEMA_FILE",
    "subagent_spec_schema",
    "top_agent_schema",
    "write_agent_schemas",
]

#: Directory under ``~/.kodo/etc/`` the schemas are written to. Not under
#: ``~/.kodo/agents/``: the agent store reads every directory there as a bundle.
AGENT_SCHEMAS_DIRNAME = "agent_schemas"

#: File name of the schema for a top-level agent's ``<name>.json``.
TOP_AGENT_SCHEMA_FILE = "top_agent.schema.json"

#: File name of the schema for a sub-agent's spec ``<name>.json``.
SUBAGENT_SPEC_SCHEMA_FILE = "subagent_spec.schema.json"

_DRAFT = "http://json-schema.org/draft-07/schema#"


def _user_name(what: str) -> dict[str, object]:
    """The schema of a user agent's ``name``: non-empty, not reserved."""
    return {
        "type": "string",
        "minLength": 1,
        "description": (
            f"The {what}'s name. Must equal this file's name without `.json`, and the "
            f"name of the directory holding it."
        ),
        "not": {
            "anyOf": [
                {"pattern": f"^{BUILTIN_NAME_PREFIX}"},
                {"const": SHARED_SUBAGENTS_DIRNAME},
            ]
        },
        "errorMessage": (
            f"A user agent's name must not start with `{BUILTIN_NAME_PREFIX}` or be "
            f"`{SHARED_SUBAGENTS_DIRNAME}`."
        ),
    }


def _notes() -> dict[str, object]:
    return {
        "type": "string",
        "description": (
            "Why this file looks the way it does, for whoever edits it next. Never shown "
            "to a model or a user."
        ),
    }


def _top_agent_properties() -> dict[str, dict[str, object]]:
    """One property schema per key a top-level agent's config may hold."""
    return {
        "name": _user_name("agent"),
        "notes": _notes(),
        "label": {
            "type": "string",
            "description": (
                "The name shown in the agent picker. Defaults to the prompt's `display_name`."
            ),
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "description": "One sentence shown under the label: what choosing this agent does.",
        },
        "rank": {
            "type": "integer",
            "default": 0,
            "description": "Sort key in the agent picker, ascending. Ties sort by name.",
        },
        "selectable": {
            "type": "boolean",
            "default": True,
            "description": "Whether the agent picker offers this agent.",
        },
        "default": {
            "const": False,
            "description": (
                "Only a built-in agent can be the default. A user agent that declares "
                "`true` does not load; pick a default in Settings instead."
            ),
        },
        "interactive": {
            "const": True,
            "description": (
                "Only a built-in agent can be non-interactive. A user agent that "
                "declares `false` does not load."
            ),
        },
    }


def top_agent_schema() -> dict[str, object]:
    """The JSON Schema of a user top-level agent's ``<name>.json``.

    Returns:
        dict[str, object]: A draft-07 schema with one property per key in
        :data:`~kodo.agents.TOP_AGENT_KEYS` and ``additionalProperties: false``.

    Raises:
        KeyError: The loader accepts a key this module has no schema for.
    """
    described = _top_agent_properties()
    return {
        "$schema": _DRAFT,
        "title": "Kōdo top-level agent config",
        "description": (
            "How a top-level agent is offered in the picker. Sits beside the agent's "
            "`agent_<name>.md` prompt."
        ),
        "type": "object",
        "properties": {key: described[key] for key in sorted(TOP_AGENT_KEYS)},
        "required": ["name", "description"],
        "additionalProperties": False,
    }


def _shape_arg_schemas() -> dict[str, dict[str, object]]:
    """One schema per argument any spec shape accepts."""
    return {
        "input_paths": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Prose describing which files this agent reads. It becomes the "
                "description of the engine-supplied `input_paths` field."
            ),
        },
        "require_input_paths": {
            "type": "boolean",
            "default": True,
            "description": "Whether `input_paths` is listed as required.",
        },
        "require_responsibility": {
            "type": "boolean",
            "default": False,
            "description": (
                "True for an agent that runs once per component: declares and requires "
                "`responsibility_code`."
            ),
        },
        "extra_properties": {
            "type": "object",
            "additionalProperties": {"type": "object"},
            "description": "Extra properties merged into the shape's schema, as JSON Schemas.",
        },
        "extra_required": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra required property names.",
        },
        "schema": {
            "type": "object",
            "description": "The literal JSON Schema, used as written.",
        },
    }


#: Arguments a shape cannot omit; mirrors the loader's ``_req_str``/``raw`` checks.
_REQUIRED_SHAPE_ARGS: dict[str, tuple[str, ...]] = {
    "pipeline_input": ("input_paths",),
    "raw": ("schema",),
}

_SHAPE_DESCRIPTIONS: dict[str, str] = {
    "pipeline_input": "The task a file-backed pipeline sub-agent receives.",
    "author_output": "An author's result: the paths it wrote, or an escalation.",
    "critic_output": "A critic's result: the reviewed path and its findings.",
    "raw": "A literal JSON Schema under `schema`, for a contract of the agent's own.",
}


def _shape_schema(field: str) -> dict[str, object]:
    """The schema of ``input_schema``/``output_schema``: one branch per shape."""
    arg_schemas = _shape_arg_schemas()
    branches: list[dict[str, object]] = []
    for shape in sorted(SPEC_SHAPE_ARGS):
        properties: dict[str, object] = {
            "shape": {"const": shape, "description": _SHAPE_DESCRIPTIONS[shape]}
        }
        for arg in sorted(SPEC_SHAPE_ARGS[shape]):
            properties[arg] = arg_schemas[arg]
        branches.append(
            {
                "type": "object",
                "properties": properties,
                "required": ["shape", *_REQUIRED_SHAPE_ARGS.get(shape, ())],
                "additionalProperties": False,
            }
        )
    return {
        "description": (
            f"The {field.replace('_', ' ')}, written as a shape: the name of a "
            "shared schema builder and its arguments."
        ),
        "type": "object",
        "required": ["shape"],
        "properties": {"shape": {"enum": sorted(SPEC_SHAPE_ARGS)}},
        "oneOf": branches,
    }


def _consumes_entry_schemas() -> dict[str, dict[str, object]]:
    """One schema per key a ``consumes`` entry may hold."""
    return {
        "role": {
            "type": "string",
            "minLength": 1,
            "examples": sorted(ALL_ROLES),
            "description": "The artifact role this agent needs.",
        },
        "scope": {
            "enum": sorted(ALL_SCOPES),
            "default": "global",
            "description": "Which work products filling that role are handed over.",
        },
        "required": {
            "type": "boolean",
            "default": True,
            "description": "Whether the agent cannot run without it.",
        },
    }


def _spec_properties() -> dict[str, dict[str, object]]:
    """One property schema per key a sub-agent spec may hold."""
    entry = _consumes_entry_schemas()
    return {
        "name": _user_name("sub-agent"),
        "notes": _notes(),
        "input_schema": _shape_schema("input_schema"),
        "output_schema": _shape_schema("output_schema"),
        "produces": {
            "type": "object",
            "propertyNames": {"minLength": 1, "examples": sorted(ALL_ROLES)},
            "additionalProperties": {"type": "string", "examples": [PRODUCES_REMAINDER]},
            "description": (
                "Artifact role -> the output field carrying its path(s); "
                f"`{PRODUCES_REMAINDER}` means everything the agent reported."
            ),
        },
        "component_paths": {
            "type": "string",
            "description": (
                "The output field holding {component codename: path}, for an agent "
                "that writes several components' artifacts in one run."
            ),
        },
        "consumes": {
            "type": "array",
            "description": (
                "The inputs this agent needs, resolved by the engine into `input_paths`."
            ),
            "items": {
                "type": "object",
                "properties": {key: entry[key] for key in sorted(SPEC_CONSUMES_KEYS)},
                "required": ["role"],
                "additionalProperties": False,
            },
        },
    }


def subagent_spec_schema() -> dict[str, object]:
    """The JSON Schema of a user sub-agent's spec ``<name>.json``.

    Returns:
        dict[str, object]: A draft-07 schema with one property per key in
        :data:`~kodo.agents.subagents.SPEC_KEYS` and ``additionalProperties:
        false``.

    Raises:
        KeyError: The loader accepts a key or shape argument this module has
            no schema for.
    """
    described = _spec_properties()
    return {
        "$schema": _DRAFT,
        "title": "Kōdo sub-agent spec",
        "description": (
            "A sub-agent's typed contract: its input and output schemas and the "
            "artifacts it produces and consumes. Sits beside `subagent_<name>.md`."
        ),
        "type": "object",
        "properties": {key: described[key] for key in sorted(SPEC_KEYS)},
        "required": ["name", "input_schema", "output_schema"],
        "additionalProperties": False,
    }


def write_agent_schemas(etc_dir: Path) -> tuple[Path, ...]:
    """Write both schemas under ``<etc_dir>/agent_schemas/``, where they differ.

    A file whose content already matches is left untouched, so a server start
    with an unchanged Kōdo writes nothing. Each write goes through a hidden
    temp file and ``os.replace``, so a reader never sees half a schema.

    Args:
        etc_dir: The ``~/.kodo/etc`` directory.

    Returns:
        tuple[Path, ...]: The files written this call (empty when both were
        current).

    Raises:
        OSError: A directory or file cannot be written.
    """
    directory = etc_dir / AGENT_SCHEMAS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, schema in (
        (TOP_AGENT_SCHEMA_FILE, top_agent_schema()),
        (SUBAGENT_SPEC_SCHEMA_FILE, subagent_spec_schema()),
    ):
        path = directory / filename
        text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == text:
            continue
        _atomic_write(path, text)
        written.append(path)
    return tuple(written)


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* through a hidden temp file and ``os.replace``."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
