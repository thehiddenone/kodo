"""JSON Schemas for user agent JSON files (doc/AGENT_BUILDER.md §4).

The schemas are only useful if they agree with the loaders: a schema that
accepts a file the server refuses sends the user to a BROKEN row with no
squiggle, and one that refuses a good file squiggles a working agent. So the
core test here is agreement: one valid file, a table of single mutations, and
for each the schema's verdict must equal what loading it as a user agent gives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from kodo.agents import (
    AGENT_SCHEMAS_DIRNAME,
    BUILTIN_NAME_PREFIX,
    PACKAGED_AGENTS_DIR,
    SUBAGENT_SPEC_SCHEMA_FILE,
    TOP_AGENT_KEYS,
    TOP_AGENT_SCHEMA_FILE,
    TopAgentLoadError,
    load_top_agent,
    subagent_spec_schema,
    top_agent_schema,
    write_agent_schemas,
)
from kodo.agents.subagents import (
    ALL_SCOPES,
    SPEC_CONSUMES_KEYS,
    SPEC_KEYS,
    SPEC_SHAPE_ARGS,
    SpecLoadError,
    load_spec,
)
from kodo.agents.subagents.specs import SPECS_DIR

_TOP = Draft7Validator(top_agent_schema())
_SPEC = Draft7Validator(subagent_spec_schema())


def _as_user(raw: dict[str, object]) -> dict[str, object]:
    """A built-in file renamed the way a copy renames it (``kodo_x`` -> ``x``)."""
    return {**raw, "name": str(raw["name"]).removeprefix(BUILTIN_NAME_PREFIX)}


def _schema_ok(validator: Draft7Validator, raw: object) -> bool:
    return not list(validator.iter_errors(raw))


# ---------------------------------------------------------------------------
# Coverage of the loaders' key sets
# ---------------------------------------------------------------------------


def test_both_schemas_are_valid_draft7() -> None:
    Draft7Validator.check_schema(top_agent_schema())
    Draft7Validator.check_schema(subagent_spec_schema())


def test_top_agent_schema_lists_exactly_the_loader_keys() -> None:
    schema = top_agent_schema()
    assert set(schema["properties"]) == TOP_AGENT_KEYS  # type: ignore[arg-type]
    assert schema["additionalProperties"] is False


def test_spec_schema_lists_exactly_the_loader_keys_and_shape_arguments() -> None:
    schema = subagent_spec_schema()
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == SPEC_KEYS
    assert set(properties["consumes"]["items"]["properties"]) == SPEC_CONSUMES_KEYS
    for field in ("input_schema", "output_schema"):
        branches = {
            branch["properties"]["shape"]["const"]: set(branch["properties"]) - {"shape"}
            for branch in properties[field]["oneOf"]
        }
        assert branches == {shape: set(args) for shape, args in SPEC_SHAPE_ARGS.items()}


# ---------------------------------------------------------------------------
# Real files
# ---------------------------------------------------------------------------


_BUILTIN_SPECS = sorted(SPECS_DIR.glob("*.json"))
_BUILTIN_CONFIGS = sorted(PACKAGED_AGENTS_DIR.glob("*.json"))


@pytest.mark.parametrize("path", _BUILTIN_SPECS, ids=lambda p: p.stem)
def test_every_builtin_spec_validates_once_renamed(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    errors = [error.message for error in _SPEC.iter_errors(_as_user(raw))]
    assert errors == []


@pytest.mark.parametrize("path", _BUILTIN_CONFIGS, ids=lambda p: p.stem)
def test_builtin_config_validates_once_renamed_unless_it_holds_a_builtin_only_key(
    path: Path,
) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    builtin_only = raw.get("default") is True or raw.get("interactive") is False
    assert _schema_ok(_TOP, _as_user(raw)) is not builtin_only


def test_a_builtin_name_is_refused_by_both_schemas() -> None:
    config = json.loads((PACKAGED_AGENTS_DIR / "kodo_guide.json").read_text(encoding="utf-8"))
    spec = json.loads((SPECS_DIR / "kodo_coder.json").read_text(encoding="utf-8"))
    assert not _schema_ok(_TOP, config)
    assert not _schema_ok(_SPEC, spec)
    assert not _schema_ok(_TOP, {**_as_user(config), "name": "subagents"})


# ---------------------------------------------------------------------------
# Agreement with the loaders
# ---------------------------------------------------------------------------


_GOOD_CONFIG: dict[str, object] = {
    "name": "acme",
    "description": "Does one thing.",
}

_CONFIG_CASES: dict[str, dict[str, object]] = {
    "minimal": {},
    "every key": {
        "notes": "n",
        "label": "Acme",
        "rank": 5,
        "selectable": False,
        "default": False,
        "interactive": True,
    },
    "unknown key": {"selectible": True},
    "empty description": {"description": ""},
    "missing description": {"description": None},
    "rank is a bool": {"rank": True},
    "rank is a float": {"rank": 1.5},
    "label is a number": {"label": 3},
    "selectable is a string": {"selectable": "yes"},
    "claims default": {"default": True},
    "not interactive": {"interactive": False, "selectable": False},
}


def _user_config_loads(raw: dict[str, object], tmp_path: Path) -> bool:
    """Whether *raw* loads **as a user agent**: the loader plus the registry's refusals."""
    path = tmp_path / f"{raw.get('name')}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    try:
        top = load_top_agent(path)
    except TopAgentLoadError:
        return False
    return not top.default and top.interactive


@pytest.mark.parametrize("case", sorted(_CONFIG_CASES))
def test_top_agent_schema_agrees_with_loading_as_a_user_agent(case: str, tmp_path: Path) -> None:
    raw = {**_GOOD_CONFIG, **_CONFIG_CASES[case]}
    raw = {key: value for key, value in raw.items() if value is not None}
    assert _schema_ok(_TOP, raw) is _user_config_loads(raw, tmp_path)


_GOOD_SPEC: dict[str, object] = {
    "name": "acme_writer",
    "input_schema": {"shape": "pipeline_input", "input_paths": "The brief."},
    "output_schema": {"shape": "author_output"},
}

_SPEC_CASES: dict[str, dict[str, object]] = {
    "minimal": {},
    "every key": {
        "notes": "n",
        "produces": {"code": "paths"},
        "component_paths": "",
        "consumes": [{"role": "narrative", "scope": "global", "required": False}],
    },
    "invented role": {"consumes": [{"role": "brief"}], "produces": {"brief": "paths"}},
    "raw shape": {"output_schema": {"shape": "raw", "schema": {"type": "object"}}},
    "critic shape": {"output_schema": {"shape": "critic_output"}},
    "every pipeline_input argument": {
        "input_schema": {
            "shape": "pipeline_input",
            "input_paths": "x",
            "require_input_paths": False,
            "require_responsibility": True,
            "extra_properties": {"topic": {"type": "string"}},
            "extra_required": ["topic"],
        }
    },
    "unknown key": {"descripton": "x"},
    "missing output_schema": {"output_schema": None},
    "unknown shape": {"output_schema": {"shape": "writer_output"}},
    "shape argument it does not take": {"output_schema": {"shape": "critic_output", "x": 1}},
    "raw without schema": {"output_schema": {"shape": "raw"}},
    "pipeline_input without input_paths": {"input_schema": {"shape": "pipeline_input"}},
    "require_input_paths is a string": {
        "input_schema": {"shape": "pipeline_input", "input_paths": "x", "require_input_paths": "no"}
    },
    "extra_required is not a list": {
        "input_schema": {"shape": "pipeline_input", "input_paths": "x", "extra_required": "topic"}
    },
    "produces value is not a string": {"produces": {"code": 1}},
    "consumes is an object": {"consumes": {"role": "code"}},
    "consumes entry unknown key": {"consumes": [{"role": "code", "scop": "self"}]},
    "consumes entry without role": {"consumes": [{"scope": "self"}]},
    "consumes required is a string": {"consumes": [{"role": "code", "required": "no"}]},
    "unknown scope": {"consumes": [{"role": "code", "scope": "nearby"}]},
    "component_paths is a list": {"component_paths": []},
}


def _user_spec_loads(raw: dict[str, object], tmp_path: Path) -> bool:
    """Whether *raw* loads as a user spec: the loader plus the registry's scope check."""
    path = tmp_path / f"{raw.get('name')}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    try:
        spec = load_spec(path)
    except SpecLoadError:
        return False
    return all(need.scope in ALL_SCOPES for need in spec.consumes)


@pytest.mark.parametrize("case", sorted(_SPEC_CASES))
def test_spec_schema_agrees_with_loading_as_a_user_spec(case: str, tmp_path: Path) -> None:
    raw = {**_GOOD_SPEC, **_SPEC_CASES[case]}
    raw = {key: value for key, value in raw.items() if value is not None}
    assert _schema_ok(_SPEC, raw) is _user_spec_loads(raw, tmp_path)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_write_creates_both_files_holding_the_schemas(tmp_path: Path) -> None:
    written = write_agent_schemas(tmp_path)

    directory = tmp_path / AGENT_SCHEMAS_DIRNAME
    assert set(written) == {
        directory / TOP_AGENT_SCHEMA_FILE,
        directory / SUBAGENT_SPEC_SCHEMA_FILE,
    }
    assert json.loads((directory / TOP_AGENT_SCHEMA_FILE).read_text("utf-8")) == top_agent_schema()
    spec_text = (directory / SUBAGENT_SPEC_SCHEMA_FILE).read_text("utf-8")
    assert json.loads(spec_text) == subagent_spec_schema()


def test_write_leaves_current_files_alone_and_repairs_stale_ones(tmp_path: Path) -> None:
    write_agent_schemas(tmp_path)
    stale = tmp_path / AGENT_SCHEMAS_DIRNAME / TOP_AGENT_SCHEMA_FILE
    stale.write_text("{}", encoding="utf-8")

    assert write_agent_schemas(tmp_path) == (stale,)
    assert write_agent_schemas(tmp_path) == ()
    assert json.loads(stale.read_text("utf-8")) == top_agent_schema()
    assert [p.name for p in stale.parent.iterdir() if p.name.startswith(".")] == []
