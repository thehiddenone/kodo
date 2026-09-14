"""Build :class:`~kodo.subagents.SubAgentSpec` objects from their JSON definitions.

Every catalog entry is a ``<name>.json`` file in this package. Nothing about a
sub-agent's contract is hardcoded in Python any more: this module is the one
place that turns such a file into a spec, and :mod:`kodo.subagents.specs` runs
it over the package directory at import time to build ``ALL_SUBAGENTS``.

The file format
===============

::

    {
      "name": "coder",
      "notes": "Why this spec looks the way it does (never shown to a model).",
      "input_schema":  { "shape": "pipeline_input", "input_paths": "…" },
      "output_schema": { "shape": "author_output" },
      "produces": { "code": "paths" },
      "component_paths": "designs",
      "consumes": [ { "role": "test_plan", "scope": "self" } ]
    }

``name`` must equal the filename stem — the same rule ``subagent_<name>.md``
already follows, so a spec, its prompt and its file name cannot drift apart.
Only ``name``, ``input_schema`` and ``output_schema`` are required; every other
key defaults the way the dataclass does.

Schemas: shapes, not copies
---------------------------

A schema is written as a **shape** — the name of one of the declarative
builders in :mod:`._shapes` plus that builder's arguments — rather than as an
expanded JSON Schema. ``{"shape": "author_output"}`` is the whole of most
authors' output declaration, and the ~80 lines of envelope it stands for
(``paths``/``summary`` plus the escalation fields) keep living in one Python
function instead of being copy-pasted into seventeen files. Change the envelope
there and every spec that names the shape follows, which is exactly what the
builders were introduced to guarantee and what expanding them into JSON would
have thrown away.

``{"shape": "raw", "schema": {…}}`` is the escape hatch, for the agents whose
contract is genuinely their own (``planner``, ``investigator``, ``web_search``,
the inline agents): the object under ``schema`` is used verbatim. A
user-authored spec can always reach for it, so naming shapes costs no
expressiveness — it only removes duplication from the specs that do share an
envelope.

Unknown keys are errors, everywhere — at the top level, inside a shape, and
inside a ``consumes`` entry. A misspelled ``require_responsability`` that was
silently ignored would produce an agent whose tool quietly lacks a field, which
is precisely the class of failure that fails-fast checking exists to prevent.
Role and scope *vocabulary* is deliberately not checked here: that is
cross-agent knowledge (is this role produced by anybody?) and stays where it
already lives, in :meth:`AgentRegistry.__validate_artifact_roles`.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from .._artifacts import Need
from .._subagentspec import SubAgentSpec
from ._shapes import author_output, critic_output, pipeline_input

__all__ = ["SPEC_SUFFIX", "SpecLoadError", "load_spec", "load_specs"]

#: Extension of a spec definition file. The glob that finds them.
SPEC_SUFFIX = ".json"

_TOP_LEVEL_KEYS = frozenset(
    {
        "name",
        "notes",
        "input_schema",
        "output_schema",
        "produces",
        "component_paths",
        "consumes",
    }
)
_CONSUMES_KEYS = frozenset({"role", "scope", "required"})

#: Shape name -> the argument names that shape accepts. ``raw`` is not a
#: builder: it carries a literal schema under ``schema``.
_SHAPE_ARGS: dict[str, frozenset[str]] = {
    "pipeline_input": frozenset(
        {
            "input_paths",
            "require_input_paths",
            "require_responsibility",
            "extra_properties",
            "extra_required",
        }
    ),
    "author_output": frozenset({"extra_properties"}),
    "critic_output": frozenset(),
    "raw": frozenset({"schema"}),
}


class SpecLoadError(Exception):
    """Raised when a spec JSON file is malformed, or names something unknown."""


def load_spec(path: Path) -> SubAgentSpec:
    """Build one :class:`SubAgentSpec` from its JSON definition.

    Args:
        path: Absolute path to a ``<name>.json`` spec file.

    Returns:
        SubAgentSpec: The fully-built contract, with both schemas expanded from
        their declared shapes.

    Raises:
        SpecLoadError: The file is not valid JSON, is not a JSON object, holds
            an unknown key, has a ``name`` that disagrees with the filename, or
            declares a schema shape or shape argument that does not exist.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecLoadError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise SpecLoadError(f"{path}: spec file must hold a JSON object, got {type(raw).__name__}")

    _reject_unknown(raw, _TOP_LEVEL_KEYS, f"{path}: unknown key")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise SpecLoadError(f"{path}: missing or empty 'name'")
    if name != path.stem:
        raise SpecLoadError(
            f"{path}: 'name' is {name!r} but the filename stem is {path.stem!r}; "
            f"a spec must live in {name}{SPEC_SUFFIX}"
        )

    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise SpecLoadError(f"{path}: 'notes' must be a string")

    for key in ("input_schema", "output_schema"):
        if key not in raw:
            raise SpecLoadError(f"{path}: missing required key {key!r}")

    component_paths = raw.get("component_paths", "")
    if not isinstance(component_paths, str):
        raise SpecLoadError(f"{path}: 'component_paths' must be a string")

    return SubAgentSpec(
        name=name,
        notes=notes,
        input_schema=_build_schema(raw["input_schema"], path, "input_schema"),
        output_schema=_build_schema(raw["output_schema"], path, "output_schema"),
        produces=_produces(raw.get("produces", {}), path),
        consumes=_consumes(raw.get("consumes", []), path),
        component_paths=component_paths,
    )


def load_specs(directory: Path) -> tuple[SubAgentSpec, ...]:
    """Build every spec in *directory*, in filename order.

    Every ``*.json`` file in the directory is a spec — there is no manifest to
    keep in step, so adding one is adding a file. Callers that want the catalog
    in dependency order pass the result through
    :func:`~kodo.subagents.specs._order.pipeline_order`.

    Two files cannot collide on a name, because a name *is* its filename stem —
    so there is no duplicate check here. Merging several spec directories (phase
    2) is where that question arises, and it belongs to whatever does the
    merging, which is the only thing holding both candidates.

    Args:
        directory: Directory holding the ``<name>.json`` spec files.

    Returns:
        tuple[SubAgentSpec, ...]: One spec per file, sorted by name.

    Raises:
        SpecLoadError: Any file in the directory fails to load.
    """
    return tuple(load_spec(path) for path in sorted(directory.glob(f"*{SPEC_SUFFIX}")))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_schema(node: object, path: Path, field: str) -> dict[str, object]:
    """Expand one ``{"shape": …}`` declaration into a real JSON Schema."""
    where = f"{path}: {field}"
    if not isinstance(node, dict):
        raise SpecLoadError(f"{where} must be an object naming a 'shape'")
    shape = node.get("shape")
    if not isinstance(shape, str) or not shape:
        raise SpecLoadError(f"{where} must declare a 'shape' (one of {sorted(_SHAPE_ARGS)})")
    if shape not in _SHAPE_ARGS:
        raise SpecLoadError(
            f"{where}: unknown shape {shape!r}; known shapes: {sorted(_SHAPE_ARGS)}"
        )
    args = {k: v for k, v in node.items() if k != "shape"}
    _reject_unknown(args, _SHAPE_ARGS[shape], f"{where}: shape {shape!r} has no argument")

    if shape == "raw":
        schema = args.get("schema")
        if not isinstance(schema, dict):
            raise SpecLoadError(f"{where}: shape 'raw' needs a 'schema' object")
        # Copied so a caller can never mutate the loaded spec's schema through
        # the dict it passed in, matching the builders' fresh-dict contract.
        return deepcopy(schema)
    if shape == "critic_output":
        return critic_output()
    if shape == "author_output":
        return author_output(extra_properties=_opt_obj(args, "extra_properties", where))
    return pipeline_input(
        input_paths=_req_str(args, "input_paths", where),
        require_input_paths=_opt_bool(args, "require_input_paths", True, where),
        require_responsibility=_opt_bool(args, "require_responsibility", False, where),
        extra_properties=_opt_obj(args, "extra_properties", where),
        extra_required=_opt_str_list(args, "extra_required", where),
    )


def _produces(value: object, path: Path) -> dict[str, str]:
    """Validate and copy the ``{role: output field}`` mapping."""
    if not isinstance(value, dict):
        raise SpecLoadError(f"{path}: 'produces' must be an object mapping role -> output field")
    produces: dict[str, str] = {}
    for role, field_name in value.items():
        if not isinstance(field_name, str):
            raise SpecLoadError(
                f"{path}: 'produces' entry {role!r} must name an output field as a string"
            )
        produces[str(role)] = field_name
    return produces


def _consumes(value: object, path: Path) -> tuple[Need, ...]:
    """Validate and build the declared :class:`Need` list."""
    if not isinstance(value, list):
        raise SpecLoadError(f"{path}: 'consumes' must be a list of {{role, scope}} objects")
    needs: list[Need] = []
    for index, entry in enumerate(value):
        where = f"{path}: consumes[{index}]"
        if not isinstance(entry, dict):
            raise SpecLoadError(f"{where} must be an object")
        _reject_unknown(entry, _CONSUMES_KEYS, f"{where}: unknown key")
        role = entry.get("role")
        if not isinstance(role, str) or not role:
            raise SpecLoadError(f"{where}: missing or empty 'role'")
        scope = entry.get("scope", "global")
        if not isinstance(scope, str) or not scope:
            raise SpecLoadError(f"{where}: 'scope' must be a non-empty string")
        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise SpecLoadError(f"{where}: 'required' must be true or false")
        needs.append(Need(role=role, scope=scope, required=required))
    return tuple(needs)


def _reject_unknown(mapping: dict[str, object], allowed: frozenset[str], prefix: str) -> None:
    """Raise when *mapping* holds a key outside *allowed*."""
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise SpecLoadError(f"{prefix} {unknown[0]!r}; known keys: {sorted(allowed)}")


def _req_str(args: dict[str, object], key: str, where: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise SpecLoadError(f"{where}: {key!r} is required and must be a non-empty string")
    return value


def _opt_bool(args: dict[str, object], key: str, default: bool, where: str) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise SpecLoadError(f"{where}: {key!r} must be true or false")
    return value


def _opt_obj(args: dict[str, object], key: str, where: str) -> dict[str, object] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SpecLoadError(f"{where}: {key!r} must be an object")
    return deepcopy(value)


def _opt_str_list(args: dict[str, object], key: str, where: str) -> list[str] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpecLoadError(f"{where}: {key!r} must be a list of strings")
    return [str(item) for item in value]
