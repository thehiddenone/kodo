"""Behavior tests for kodo.llms.anthropic._cache.

Tests verify the structure of prompt-caching blocks produced by
build_system_blocks and build_message_params without touching any network.
"""

from __future__ import annotations

from kodo.llms._interface import Message
from kodo.llms.anthropic._cache import build_message_params, build_system_blocks

# ---------------------------------------------------------------------------
# build_system_blocks
# ---------------------------------------------------------------------------


def test_build_system_blocks_returns_one_block() -> None:
    """
    Given a system prompt string,
    when build_system_blocks is called,
    then a list with exactly one block is returned.
    """
    result = build_system_blocks("You are a helpful assistant.")
    assert len(result) == 1


def test_build_system_blocks_text_matches_input() -> None:
    """
    Given a system prompt string,
    when build_system_blocks is called,
    then the block's text field equals the input.
    """
    prompt = "You are the Narrative Author."
    result = build_system_blocks(prompt)
    assert result[0]["text"] == prompt


def test_build_system_blocks_cache_control_present_by_default() -> None:
    """
    Given a system prompt,
    when build_system_blocks is called with default cache=True,
    then the block contains a cache_control field.
    """
    result = build_system_blocks("system prompt")
    assert "cache_control" in result[0]
    assert result[0]["cache_control"] == {"type": "ephemeral"}


def test_build_system_blocks_no_cache_control_when_cache_false() -> None:
    """
    Given a system prompt,
    when build_system_blocks is called with cache=False,
    then the block has no cache_control field.
    """
    result = build_system_blocks("system prompt", cache=False)
    assert "cache_control" not in result[0]


def test_build_system_blocks_type_is_text() -> None:
    """
    Given any system prompt,
    when build_system_blocks is called,
    then the block type is 'text'.
    """
    result = build_system_blocks("x")
    assert result[0]["type"] == "text"


# ---------------------------------------------------------------------------
# build_message_params — string content
# ---------------------------------------------------------------------------


def test_build_message_params_empty_messages() -> None:
    """
    Given an empty message list,
    when build_message_params is called,
    then an empty list is returned.
    """
    result = build_message_params([], [])
    assert result == []


def test_build_message_params_string_content_becomes_text_block() -> None:
    """
    Given a message with string content,
    when build_message_params is called,
    then the content is wrapped in a text block.
    """
    messages = [Message(role="user", content="Hello")]
    result = build_message_params(messages, [])
    assert len(result) == 1
    assert result[0]["role"] == "user"
    content = result[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Hello"


def test_build_message_params_no_cache_control_without_breakpoint() -> None:
    """
    Given a message not listed in cache_breakpoints,
    when build_message_params is called,
    then the block has no cache_control.
    """
    messages = [Message(role="user", content="Hello")]
    result = build_message_params(messages, [])
    assert "cache_control" not in result[0]["content"][0]


def test_build_message_params_cache_control_at_breakpoint_index() -> None:
    """
    Given messages where index 0 is in cache_breakpoints,
    when build_message_params is called,
    then the content block at index 0 has cache_control.
    """
    messages = [Message(role="user", content="Hello")]
    result = build_message_params(messages, [0])
    assert result[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_build_message_params_cache_control_only_at_specified_index() -> None:
    """
    Given two messages with breakpoint only at index 1,
    when build_message_params is called,
    then only index 1 has cache_control.
    """
    messages = [
        Message(role="user", content="First"),
        Message(role="assistant", content="Second"),
    ]
    result = build_message_params(messages, [1])
    assert "cache_control" not in result[0]["content"][0]
    assert result[1]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_build_message_params_role_preserved() -> None:
    """
    Given messages with alternating roles,
    when build_message_params is called,
    then each output entry's role matches the input.
    """
    messages = [
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi"),
    ]
    result = build_message_params(messages, [])
    assert result[0]["role"] == "user"
    assert result[1]["role"] == "assistant"


def test_build_message_params_list_content_is_copied() -> None:
    """
    Given a message with list-of-blocks content,
    when build_message_params is called,
    then the blocks are preserved with the same structure.
    """
    blocks = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    messages = [Message(role="user", content=blocks)]
    result = build_message_params(messages, [])
    assert len(result[0]["content"]) == 2
    assert result[0]["content"][0]["text"] == "A"


def test_build_message_params_cache_on_last_block_of_list_content() -> None:
    """
    Given a message with list content at a breakpoint index,
    when build_message_params is called,
    then cache_control is added to the LAST block only.
    """
    blocks = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    messages = [Message(role="user", content=blocks)]
    result = build_message_params(messages, [0])
    content = result[0]["content"]
    assert "cache_control" not in content[0]
    assert content[1]["cache_control"] == {"type": "ephemeral"}


def test_build_message_params_multiple_breakpoints() -> None:
    """
    Given three messages with breakpoints at indices 0 and 2,
    when build_message_params is called,
    then exactly those two messages have cache_control.
    """
    messages = [
        Message(role="user", content="A"),
        Message(role="assistant", content="B"),
        Message(role="user", content="C"),
    ]
    result = build_message_params(messages, [0, 2])
    assert result[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in result[1]["content"][0]
    assert result[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
