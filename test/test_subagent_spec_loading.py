"""Guard rails for building the sub-agent catalog from its JSON definitions.

``test_subagentspecs.py`` asserts what the specs *say* (schemas well-formed,
roles resolvable, registry wiring). This file asserts the machinery that
produces them: that every ``<name>.json`` in :mod:`kodo.agents.subagents.specs` loads,
that a malformed one fails loudly instead of silently yielding a half-built
contract, and that :data:`ALL_SUBAGENTS`'s order is derived from the declared
``produces``/``consumes`` graph rather than from a hand-kept list or from
whatever order the filesystem happened to return.

The distinction that matters here: a typo in a spec file must be a startup
error. The whole reason the catalog is data is that a contract can now be
written by someone who is not editing this package, and the cost of accepting a
bad one quietly is an agent whose tool is missing a field nobody notices until a
model misuses it four stages into a run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodo.agents.subagents.specs import (
    ALL_SUBAGENTS,
    SPEC_SUFFIX,
    SPECS_DIR,
    SpecLoadError,
    SpecOrderError,
    SubAgentSpec,
    load_spec,
    load_specs,
    pipeline_order,
)
from kodo.agents.subagents.specs._shapes import author_output, critic_output, pipeline_input

_SPEC_FILES = sorted(SPECS_DIR.glob(f"*{SPEC_SUFFIX}"))


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    """Write *payload* as ``<name>.json`` in *tmp_path* and return the path."""
    path = tmp_path / f"{name}{SPEC_SUFFIX}"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _minimal(name: str = "demo", **overrides: object) -> dict[str, object]:
    """A valid spec body, for tests that break exactly one thing."""
    payload: dict[str, object] = {
        "name": name,
        "input_schema": {"shape": "raw", "schema": {"type": "object", "properties": {}}},
        "output_schema": {"shape": "critic_output"},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The shipped catalog
# ---------------------------------------------------------------------------


def test_catalog_is_built_from_the_shipped_json_files() -> None:
    """Every spec file becomes exactly one catalog entry, and vice versa."""
    assert _SPEC_FILES, "no spec JSON files found — did they ship?"
    assert {p.stem for p in _SPEC_FILES} == {s.name for s in ALL_SUBAGENTS}
    assert len(ALL_SUBAGENTS) == len(_SPEC_FILES)


@pytest.mark.parametrize("path", _SPEC_FILES, ids=lambda p: p.stem)
def test_every_shipped_spec_loads(path: Path) -> None:
    """Each file parses into a spec whose name matches its filename stem."""
    spec = load_spec(path)
    assert isinstance(spec, SubAgentSpec)
    assert spec.name == path.stem
    # Both schemas must survive as real object schemas — a shape that expanded
    # to nothing would otherwise reach a model as an empty tool signature.
    for schema in (spec.input_schema, spec.output_schema):
        assert schema.get("type") == "object"
        assert isinstance(schema.get("properties"), dict)


@pytest.mark.parametrize("path", _SPEC_FILES, ids=lambda p: p.stem)
def test_shipped_specs_use_only_known_keys(path: Path) -> None:
    """The files on disk stay within the documented format.

    Loading already rejects unknown keys; this asserts the shipped files do not
    rely on some looser historical spelling, so the format documented in
    ``_loader`` is the format actually in use.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) <= {
        "name",
        "notes",
        "input_schema",
        "output_schema",
        "produces",
        "component_paths",
        "consumes",
    }


def test_every_shipped_spec_carries_its_rationale() -> None:
    """``notes`` replaced the spec modules' docstrings, so none may be empty."""
    missing = [s.name for s in ALL_SUBAGENTS if not s.notes.strip()]
    assert not missing, f"specs with no 'notes' rationale: {missing}"


def test_notes_never_reach_the_model() -> None:
    """``notes`` is engineer-facing: it must not leak into either schema."""
    for spec in ALL_SUBAGENTS:
        blob = json.dumps([spec.input_schema, spec.output_schema])
        # Compare on a distinctive slice rather than the whole note, so the
        # assertion survives reflowed prose.
        probe = spec.notes.strip()[:60]
        assert probe and probe not in blob, f"{spec.name}: notes leaked into a schema"


def test_loading_is_idempotent_and_order_is_stable() -> None:
    """Re-loading the same directory yields the same catalog in the same order."""
    first = pipeline_order(load_specs(SPECS_DIR))
    second = pipeline_order(load_specs(SPECS_DIR))
    assert [s.name for s in first] == [s.name for s in second]
    assert [s.name for s in first] == [s.name for s in ALL_SUBAGENTS]


def test_shapes_are_shared_not_copied() -> None:
    """A spec naming a shape gets that builder's live envelope, byte for byte.

    This is the property that makes shapes worth having: edit the builder and
    every spec naming it follows. If a spec had been flattened into a literal
    schema, this would drift silently.
    """
    by_name = {s.name: s for s in ALL_SUBAGENTS}
    # ``architect_critic`` names critic_output with no arguments.
    assert by_name["architect_critic"].output_schema == critic_output()
    # ``coder`` names author_output with no extras, and pipeline_input per component.
    assert by_name["coder"].output_schema == author_output()
    assert by_name["coder"].input_schema == pipeline_input(
        input_paths=_input_paths_description(by_name["coder"]),
        require_responsibility=True,
    )


def _input_paths_description(spec: SubAgentSpec) -> str:
    """Recover the prose a spec passed to ``pipeline_input``.

    The builder wraps it in a fixed suffix; strip that back off so the test
    compares the same argument the JSON declared without restating it here.
    """
    properties = spec.input_schema["properties"]
    assert isinstance(properties, dict)
    field = properties["input_paths"]
    assert isinstance(field, dict)
    described = field["description"]
    assert isinstance(described, str)
    return described.split(" Supplied by the engine, not by your caller:")[0]


def test_builders_return_fresh_objects_per_spec() -> None:
    """Two specs naming one shape must not share mutable schema state."""
    critics = [s for s in ALL_SUBAGENTS if s.output_schema == critic_output()]
    assert len(critics) > 1
    first, second = critics[0], critics[1]
    assert first.output_schema is not second.output_schema


# ---------------------------------------------------------------------------
# Rejecting bad definitions
# ---------------------------------------------------------------------------


def test_minimal_spec_loads(tmp_path: Path) -> None:
    """The fixture the negative tests mutate is itself valid."""
    spec = load_spec(_write(tmp_path, "demo", _minimal()))
    assert spec.name == "demo"
    assert spec.produces == {}
    assert spec.consumes == ()
    assert spec.component_paths == ""
    assert spec.notes == ""


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param("not an object", "must hold a JSON object", id="not-an-object"),
        pytest.param(_minimal(unexpected=1), "unknown key", id="unknown-top-level-key"),
        pytest.param(
            {"input_schema": {}, "output_schema": {}}, "missing or empty 'name'", id="no-name"
        ),
        pytest.param(_minimal(notes=["a"]), "'notes' must be a string", id="notes-not-string"),
        pytest.param(
            _minimal(component_paths=3),
            "'component_paths' must be",
            id="component-paths-not-string",
        ),
        pytest.param(
            _minimal(produces=["code"]), "'produces' must be an object", id="produces-not-object"
        ),
        pytest.param(
            _minimal(produces={"code": 7}),
            "must name an output field",
            id="produces-value-not-string",
        ),
        pytest.param(_minimal(consumes={}), "'consumes' must be a list", id="consumes-not-list"),
        pytest.param(
            _minimal(consumes=[{"scope": "self"}]), "missing or empty 'role'", id="consumes-no-role"
        ),
        pytest.param(
            _minimal(consumes=[{"role": "code", "nope": 1}]),
            "unknown key",
            id="consumes-unknown-key",
        ),
        pytest.param(
            _minimal(consumes=[{"role": "code", "required": "yes"}]),
            "'required' must be true or false",
            id="consumes-required-not-bool",
        ),
    ],
)
def test_malformed_spec_is_rejected(tmp_path: Path, payload: object, expected: str) -> None:
    """A structurally bad file raises, naming what is wrong."""
    path = _write(tmp_path, "demo", payload)
    with pytest.raises(SpecLoadError, match=expected):
        load_spec(path)


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / f"demo{SPEC_SUFFIX}"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="not valid JSON"):
        load_spec(path)


def test_name_must_match_filename(tmp_path: Path) -> None:
    """The stem is the identity the registry cross-references — enforce it."""
    path = _write(tmp_path, "demo", _minimal(name="something_else"))
    with pytest.raises(SpecLoadError, match="filename stem"):
        load_spec(path)


@pytest.mark.parametrize("key", ["input_schema", "output_schema"])
def test_both_schemas_are_required(tmp_path: Path, key: str) -> None:
    payload = _minimal()
    del payload[key]
    with pytest.raises(SpecLoadError, match=f"missing required key '{key}'"):
        load_spec(_write(tmp_path, "demo", payload))


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        pytest.param({"shape": "nonexistent"}, "unknown shape", id="unknown-shape"),
        pytest.param({"schema": {}}, "must declare a 'shape'", id="no-shape"),
        pytest.param("a string", "must be an object naming a 'shape'", id="not-an-object"),
        pytest.param({"shape": "raw"}, "needs a 'schema' object", id="raw-without-schema"),
        pytest.param(
            {"shape": "critic_output", "input_paths": "x"},
            "has no argument",
            id="wrong-arg-for-shape",
        ),
        pytest.param(
            {"shape": "pipeline_input"}, "'input_paths' is required", id="missing-shape-arg"
        ),
        pytest.param(
            {"shape": "pipeline_input", "input_paths": "x", "require_responsibility": "true"},
            "must be true or false",
            id="bool-arg-as-string",
        ),
        pytest.param(
            {"shape": "pipeline_input", "input_paths": "x", "extra_required": "instructions"},
            "must be a list of strings",
            id="list-arg-as-string",
        ),
        pytest.param(
            {"shape": "author_output", "extra_properties": []},
            "must be an object",
            id="object-arg-as-list",
        ),
    ],
)
def test_bad_schema_declaration_is_rejected(tmp_path: Path, schema: object, expected: str) -> None:
    """A typo in a shape or its arguments fails at load, not at first spawn."""
    path = _write(tmp_path, "demo", _minimal(input_schema=schema))
    with pytest.raises(SpecLoadError, match=expected):
        load_spec(path)


def test_misspelled_shape_argument_is_not_silently_ignored(tmp_path: Path) -> None:
    """The failure this check exists for: a dropped flag nobody notices.

    ``require_responsability`` would otherwise leave the agent without its
    ``responsibility_code`` field while everything still loaded.
    """
    schema = {"shape": "pipeline_input", "input_paths": "x", "require_responsability": True}
    with pytest.raises(SpecLoadError, match="has no argument 'require_responsability'"):
        load_spec(_write(tmp_path, "demo", _minimal(input_schema=schema)))


def test_raw_schema_is_copied_not_aliased(tmp_path: Path) -> None:
    """Two specs built from equal raw schemas hold independent dicts."""
    body = {"type": "object", "properties": {"q": {"type": "string"}}}
    first = load_spec(
        _write(tmp_path, "one", _minimal("one", input_schema={"shape": "raw", "schema": body}))
    )
    second = load_spec(
        _write(tmp_path, "two", _minimal("two", input_schema={"shape": "raw", "schema": body}))
    )
    assert first.input_schema == second.input_schema
    assert first.input_schema is not second.input_schema


def test_load_specs_reads_a_whole_directory(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", _minimal("alpha"))
    _write(tmp_path, "beta", _minimal("beta"))
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    assert [s.name for s in load_specs(tmp_path)] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Derived order
# ---------------------------------------------------------------------------


def _spec(
    name: str,
    produces: dict[str, str] | None = None,
    consumes: tuple[tuple[str, str], ...] = (),
) -> SubAgentSpec:
    from kodo.agents.subagents import Need  # local import: only the order tests need it

    return SubAgentSpec(
        name=name,
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        produces=produces or {},
        consumes=tuple(Need(role=r, scope=s) for r, s in consumes),
    )


def test_order_puts_producers_before_consumers() -> None:
    """The property the whole derivation exists for."""
    names = [s.name for s in ALL_SUBAGENTS]
    producers = {role: s.name for s in ALL_SUBAGENTS for role in s.produces}
    for spec in ALL_SUBAGENTS:
        for need in spec.consumes:
            if need.role in spec.produces:
                continue  # revising its own prior product
            producer = producers.get(need.role)
            if producer is None:
                continue
            assert names.index(producer) < names.index(spec.name), (
                f"{spec.name} consumes {need.role!r} but sorts before its producer {producer}"
            )


def test_order_is_independent_of_input_order() -> None:
    """Shuffling the input cannot change the derived order."""
    forward = pipeline_order(ALL_SUBAGENTS)
    backward = pipeline_order(tuple(reversed(ALL_SUBAGENTS)))
    assert [s.name for s in forward] == [s.name for s in backward]


def test_critics_follow_the_agent_they_review() -> None:
    """``under_review`` is a real edge, so a critic never precedes its author."""
    author = _spec("author", produces={"doc": "paths"})
    critic = _spec("critic", consumes=(("doc", "under_review"),))
    assert [s.name for s in pipeline_order((critic, author))] == ["author", "critic"]


def test_self_consumption_is_not_a_dependency() -> None:
    """An agent revising its own prior work product must still be orderable."""
    reviser = _spec("reviser", produces={"doc": "paths"}, consumes=(("doc", "global"),))
    downstream = _spec("downstream", consumes=(("doc", "global"),))
    assert [s.name for s in pipeline_order((downstream, reviser))] == ["reviser", "downstream"]


def test_agents_outside_the_artifact_graph_sort_last() -> None:
    """No produces and no consumes means no place in the pipeline."""
    standalone = _spec("aaa_standalone")
    producer = _spec("zzz_producer", produces={"doc": "paths"})
    consumer = _spec("zzz_consumer", consumes=(("doc", "global"),))
    ordered = [s.name for s in pipeline_order((standalone, consumer, producer))]
    assert ordered == ["zzz_producer", "zzz_consumer", "aaa_standalone"]


def test_shipped_standalone_agents_are_last() -> None:
    """The real catalog keeps its non-pipeline agents at the tail."""
    graphless = {s.name for s in ALL_SUBAGENTS if not s.produces and not s.consumes}
    names = [s.name for s in ALL_SUBAGENTS]
    assert graphless, "expected some non-pipeline agents"
    assert set(names[-len(graphless) :]) == graphless
    assert names[-len(graphless) :] == sorted(graphless)


def test_a_cycle_is_reported_not_hung() -> None:
    """Mutually-consuming specs raise, rather than recursing forever."""
    left = _spec("left", produces={"a": "paths"}, consumes=(("b", "global"),))
    right = _spec("right", produces={"b": "paths"}, consumes=(("a", "global"),))
    with pytest.raises(SpecOrderError, match="cycle"):
        pipeline_order((left, right))


def test_unproduced_role_does_not_break_ordering() -> None:
    """A dangling need is the registry's error to report, not a crash here."""
    orphan = _spec("orphan", consumes=(("nobody_makes_this", "global"),))
    assert [s.name for s in pipeline_order((orphan,))] == ["orphan"]
