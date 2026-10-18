"""Behavioral tests for the change element's listing rule (doc/SESSION_LLM_BINDING.md §4).

A parameter is listed when it changed, unless it went from the old LLM's
default to the new LLM's default (D8); the first prompt lists every
non-default parameter (D10).
"""

from __future__ import annotations

from kodo.runtime import ParamsSnapshot, ParamValue, diff_params


def _snapshot(identity: str, **params: ParamValue) -> ParamsSnapshot:
    return ParamsSnapshot(llm_identity=identity, llm_label=identity.upper(), params=params)


def _thinking(value: str, default: str) -> ParamValue:
    return ParamValue("Thinking", value, default)


def test_switching_llm_with_thinking_moving_default_to_default_lists_only_the_llm() -> None:
    old = _snapshot("a", thinking=_thinking("low", "low"))
    new = _snapshot("b", thinking=_thinking("high", "high"))

    rows = diff_params(old, new)

    assert rows == [{"param": "llm", "label": "LLM", "from": "A", "to": "B"}]


def test_a_non_default_old_value_is_listed_even_when_the_new_one_is_default() -> None:
    old = _snapshot("a", thinking=_thinking("unlimited", "high"))
    new = _snapshot("b", thinking=_thinking("high", "high"))

    rows = diff_params(old, new)

    assert [r["param"] for r in rows] == ["llm", "thinking"]
    assert rows[1]["from"] == "unlimited" and rows[1]["to"] == "high"


def test_a_change_to_a_non_default_value_is_listed() -> None:
    old = _snapshot("a", mode=ParamValue("Mode", "interactive", "interactive"))
    new = _snapshot("a", mode=ParamValue("Mode", "autonomous", "interactive"))

    assert [r["param"] for r in diff_params(old, new)] == ["mode"]


def test_nothing_changed_lists_nothing() -> None:
    snapshot = _snapshot("a", mode=ParamValue("Mode", "autonomous", "interactive"))

    assert diff_params(snapshot, snapshot) == []


def test_an_unchanged_non_default_value_is_not_listed() -> None:
    old = _snapshot("a", thinking=_thinking("unlimited", "high"))
    new = _snapshot("b", thinking=_thinking("unlimited", "high"))

    assert [r["param"] for r in diff_params(old, new)] == ["llm"]


def test_a_param_only_the_new_side_has_counts_as_unset_on_the_old_side() -> None:
    old = _snapshot("cloud")
    new = _snapshot(
        "local",
        **{
            "flag:--ctx-size": ParamValue("Context size", "65536", "131072"),
            "flag:--jinja": ParamValue("Jinja", "on", "on"),
        },
    )

    rows = diff_params(old, new)

    assert [r["param"] for r in rows] == ["llm", "flag:--ctx-size"]
    assert rows[1]["from"] is None


def test_a_param_only_the_old_side_had_is_listed_when_it_was_non_default() -> None:
    old = _snapshot("a", **{"sampling:temperature": ParamValue("Temperature", "0.8", None)})
    new = _snapshot("b")

    rows = diff_params(old, new)

    assert rows[-1] == {
        "param": "sampling:temperature",
        "label": "Temperature",
        "from": "0.8",
        "to": None,
    }


def test_extras_reach_the_row_as_from_and_to_keys() -> None:
    old = _snapshot("a", thinking=ParamValue("Thinking", "max", "high", {"base_llm": "anthropic"}))
    new = _snapshot("b", thinking=ParamValue("Thinking", "high", "medium", {"base_llm": "openai"}))

    row = diff_params(old, new)[1]

    assert row["from_base_llm"] == "anthropic"
    assert row["to_base_llm"] == "openai"


def test_first_prompt_lists_the_llm_and_every_non_default_param() -> None:
    new = _snapshot(
        "a",
        mode=ParamValue("Mode", "interactive", "interactive"),
        thinking=_thinking("unlimited", "high"),
        command_control=ParamValue("Tool Control", "permissive", "smart"),
    )

    rows = diff_params(None, new)

    assert [r["param"] for r in rows] == ["llm", "thinking", "command_control"]
    assert rows[0] == {"param": "llm", "label": "LLM", "from": None, "to": "A"}
    assert all(r["from"] is None for r in rows)


def test_snapshot_json_round_trip() -> None:
    snapshot = _snapshot(
        "local:q",
        thinking=ParamValue("Thinking", "high", "high", {"base_llm": "qwen"}),
        **{"sampling:top_k": ParamValue("Top K", "20", None)},
    )

    assert ParamsSnapshot.from_json(snapshot.to_json()) == snapshot
    assert ParamsSnapshot.from_json("nope") is None
