"""``kodo-headless`` stdout rendering and model identity (doc/HEADLESS.md).

:class:`EventSink` writes one event stream two ways — ``jsonl`` for machines
and ``text`` for people; :class:`ModelSpec` is a value object whose equality
and hashing follow its canonical ``label``.
"""

from __future__ import annotations

import io
import json

import pytest

from kodo.headless import OUTPUT_FORMATS, EventSink, ModelSpec

# ---------------------------------------------------------------------------
# EventSink
# ---------------------------------------------------------------------------


def _text(event_type: str, **fields: object) -> str:
    buffer = io.StringIO()
    EventSink("text", buffer).emit(event_type, **fields)
    return buffer.getvalue()


def test_unknown_output_format_is_refused() -> None:
    assert "xml" not in OUTPUT_FORMATS
    with pytest.raises(ValueError, match="xml"):
        EventSink("xml", io.StringIO())


def test_jsonl_writes_one_object_per_line_with_ts_and_type() -> None:
    buffer = io.StringIO()
    sink = EventSink("jsonl", buffer)

    sink.emit("tool.start", agent="coder", tool="read_file", path=object())
    sink.write_line("KODO-RESULT outcome=completed")

    first, second = buffer.getvalue().splitlines()
    event = json.loads(first)
    assert (event["type"], event["agent"], event["tool"]) == ("tool.start", "coder", "read_file")
    assert isinstance(event["ts"], str) and event["ts"]
    assert isinstance(event["path"], str)  # non-JSON values are stringified, not fatal
    assert second == "KODO-RESULT outcome=completed"


@pytest.mark.parametrize(
    ("event_type", "label"),
    [
        ("text", "assistant"),
        ("text.delta", "assistant"),
        ("thinking", "thinking"),
        ("thinking.delta", "thinking"),
    ],
)
def test_text_renders_spoken_output_with_its_agent(event_type: str, label: str) -> None:
    assert _text(event_type, agent="planner", text="hello") == f"[planner] {label}: hello\n"


def test_text_renders_top_level_output_without_a_scope() -> None:
    assert _text("text", agent=None, text="hi") == "assistant: hi\n"


def test_text_renders_a_tool_call_with_its_document() -> None:
    rendered = _text(
        "tool.call", agent="coder", tool="create_file", tool_call_id="tu_1", document="  body\n"
    )
    assert rendered == "[coder] tool create_file (tu_1):\nbody\n"


def test_text_renders_a_denied_tool() -> None:
    rendered = _text("tool.denied", agent="coder", tool="run_command", reason="git is read-only")
    assert rendered == "[coder] DENIED run_command: git is read-only\n"


def test_text_renders_the_result_as_indented_json() -> None:
    rendered = _text("run.result", outcome="completed", tool_calls=2)
    assert rendered.startswith("result: {\n")
    assert json.loads(rendered.removeprefix("result: ")) == {
        "outcome": "completed",
        "tool_calls": 2,
    }


def test_text_renders_other_events_as_non_empty_key_values() -> None:
    rendered = _text("usage", agent="coder", input=10, model="m", note="", extra=None, tags=[])
    assert rendered == "[coder] usage input=10 model=m\n"
    assert _text("run.start") == "run.start\n"


# ---------------------------------------------------------------------------
# ModelSpec
# ---------------------------------------------------------------------------


def test_model_spec_repr_shows_the_label() -> None:
    assert (
        repr(ModelSpec.parse("anthropic/claude-sonnet-5"))
        == "ModelSpec('anthropic/claude-sonnet-5')"
    )
    assert repr(ModelSpec("m-q4")) == "ModelSpec('m-q4')"


def test_model_spec_equality_and_hash_follow_vendor_and_name() -> None:
    explicit = ModelSpec.parse("local/m-q4")
    bare = ModelSpec.parse("m-q4")
    cloud = ModelSpec("m-q4", "openai")

    assert explicit == bare
    assert hash(explicit) == hash(bare)
    assert explicit != cloud
    assert len({explicit, bare, cloud}) == 2
    assert explicit != "m-q4"


@pytest.mark.parametrize(("name", "vendor"), [(" ", None), ("m", "Bad Vendor"), ("m", "9x")])
def test_model_spec_constructor_refuses_bad_identities(name: str, vendor: str | None) -> None:
    with pytest.raises(ValueError):
        ModelSpec(name, vendor)
