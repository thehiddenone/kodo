"""Message/tool conversion for the OpenAI Responses API.

Converts kodo's Anthropic-shaped persisted conversation (:class:`~kodo.llms.
_interface.Message`, whose content blocks use Claude's ``text``/``tool_use``/
``tool_result``/``thinking`` block types) into the Responses API's ``input``
item list, which is flat — a tool call or tool result is its own top-level
item, never nested inside a message's ``content`` the way Claude nests
``tool_use`` blocks.

Unlike :mod:`kodo.llms.anthropic._cache`, there is no cache-breakpoint logic
here: Responses API prompt caching is fully automatic (no ``cache_control``
markers to place), so ``LLMPlugin.stream_query``'s ``cache_breakpoints``
argument is simply accepted and ignored by the caller, per that method's
documented contract for providers without explicit caching.
"""

from __future__ import annotations

import json

from kodo.llms._interface import Message, ToolSpec
from kodo.llms._sanitize import strip_kodo_callouts
from kodo.toolspecs import tool_description

__all__ = ["build_input_items", "build_tool_defs"]


def _flatten_content(content: object) -> str:
    """Reduce a tool_result's ``content`` (str, or Anthropic-style text blocks) to a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(content)


def _expand_assistant(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    content: list[dict[str, object]] = []

    def _flush() -> None:
        if content:
            items.append({"type": "message", "role": "assistant", "content": list(content)})
            content.clear()

    for block in blocks:
        block_type = block.get("type")
        if block_type == "thinking":
            # No Responses API item accepts a foreign persisted reasoning
            # block back (regardless of origin) -- dropped, same as an
            # unsigned thinking block is dropped before replay to Claude.
            continue
        elif block_type == "text":
            text = strip_kodo_callouts(str(block.get("text", "")))
            if text:
                content.append({"type": "input_text", "text": text})
        elif block_type == "tool_use":
            _flush()
            items.append(
                {
                    "type": "function_call",
                    "call_id": str(block.get("id", "")),
                    "name": str(block.get("name", "")),
                    "arguments": json.dumps(block.get("input", {})),
                }
            )
    _flush()
    return items


def _expand_user(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    content: list[dict[str, object]] = []

    def _flush() -> None:
        if content:
            items.append({"type": "message", "role": "user", "content": list(content)})
            content.clear()

    for block in blocks:
        block_type = block.get("type")
        if block_type == "tool_result":
            _flush()
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(block.get("tool_use_id", "")),
                    "output": _flatten_content(block.get("content", "")),
                }
            )
        elif block_type == "text":
            text = str(block.get("text", ""))
            if text:
                content.append({"type": "input_text", "text": text})
    _flush()
    return items


def _expand_message(msg: Message) -> list[dict[str, object]]:
    if isinstance(msg.content, str):
        text = strip_kodo_callouts(msg.content) if msg.role == "assistant" else msg.content
        if not text:
            return []
        return [{"type": "message", "role": msg.role, "content": text}]
    if msg.role == "assistant":
        return _expand_assistant(msg.content)
    return _expand_user(msg.content)


def build_input_items(messages: list[Message]) -> list[dict[str, object]]:
    """Convert kodo's conversation history into a Responses API ``input`` list.

    Args:
        messages: Conversation history in chronological order.

    Returns:
        list[dict[str, object]]: Flat item list for the ``input=`` parameter
        of ``client.responses.stream``/``.create``. A message left with no
        items after dropping ``thinking`` blocks (or an empty string) is
        omitted entirely -- the Responses API rejects an empty ``content``.
    """
    items: list[dict[str, object]] = []
    for msg in messages:
        items.extend(_expand_message(msg))
    return items


def build_tool_defs(tools: list[ToolSpec]) -> list[dict[str, object]]:
    """Render *tools* into the Responses API ``tools=[...]`` shape.

    Flatter than both Anthropic's ``{"name","description","input_schema"}``
    and Chat Completions' nested ``{"type":"function","function":{...}}`` --
    Responses API wants ``name``/``description``/``parameters`` directly on
    the tool dict.

    Args:
        tools: Tool specifications the model may invoke.

    Returns:
        list[dict[str, object]]: Tool definitions for the ``tools=`` parameter.
    """
    return [
        {
            "type": "function",
            "name": t.name,
            "description": tool_description(t),
            "parameters": t.input_schema,
            # kodo's existing input_schemas were built for Claude and aren't
            # guaranteed to satisfy OpenAI's strict-mode requirements (every
            # property required + additionalProperties:false) -- keep parity
            # with the permissive validation Claude/llama.cpp already apply.
            "strict": False,
        }
        for t in tools
    ]
