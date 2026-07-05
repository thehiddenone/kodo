"""``update_web_search_state`` tool spec — write to the web_search agent's TTL memory.

Exclusive to the ``web_search`` agent (doc/WEB_SEARCH.md) by convention. See
:mod:`kodo.toolspecs._get_web_search_state` for the read side and
:class:`kodo.websearch.WebSearchStateStore` for the storage semantics.
"""

from __future__ import annotations

from kodo.websearch import TIME_MARK

from ._spec import SecurityImpact, ToolSpec

__all__ = ["UPDATE_WEB_SEARCH_STATE"]

UPDATE_WEB_SEARCH_STATE: ToolSpec = ToolSpec(
    name="update_web_search_state",
    external_name="Update Web Search State",
    user_description="Write search-pacing memory",
    description=(
        f"Set one key in your persistent key-value memory. Three cases: (1) a normal "
        f"string `value` stores that note under `key`, refreshing its 12-hour TTL; "
        f'(2) `value` == "" (empty string) deletes `key`; (3) `value` == '
        f'"{TIME_MARK}" (the literal string) records the current time under `key` '
        "instead of a note — reading it back later via get_web_search_state returns "
        "the number of seconds elapsed since this call, not the value you passed. Use "
        "this to remember WHEN something happened (e.g. `key='google_last_query', "
        f'value="{TIME_MARK}"` right before querying Google) so a later '
        "get_web_search_state call tells you how long it's been. Use plain string "
        "values to remember WHAT happened (e.g. `key='google_status', "
        "value='blocked: captcha wall'`). Keep keys short and stable per engine "
        "(e.g. `<engine>_last_query`, `<engine>_status`) so you can look them up "
        "consistently."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The memory key to set, delete, or time-mark.",
            },
            "value": {
                "type": "string",
                "description": (
                    f'A note to store, "" to delete `key`, or the literal "{TIME_MARK}" '
                    "to record the current time under `key`."
                ),
            },
        },
        "required": ["key", "value"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Always 'ok'."},
        },
        "required": ["status"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={"key": "visible", "value": "visible"},
    output_visibility={"status": "visible"},
    when_to_use=(
        "Right before querying a search engine, to time-mark that you're about to "
        "query it — so a later call can tell how recently it was hit.",
        "Immediately after an engine serves a wall/captcha, to record that block so "
        "you don't repeat the same query against it this session.",
        "To delete a stale note that no longer applies (empty-string value).",
    ),
)
