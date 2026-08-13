"""Message/tool conversion for Google's Gemini OpenAI-compatible Chat Completions endpoint.

Gemini's OpenAI-compatibility layer (https://ai.google.dev/gemini-api/docs/openai)
speaks Chat Completions, not the Responses API :mod:`kodo.llms.openai._convert`/
:mod:`kodo.llms.meta._convert` target -- the only existing Chat-Completions
precedent in this codebase is the *local* :mod:`kodo.llms.llamacpp._llama`
plugin, so this module is adapted from that one's message-shape helpers
(``_flatten_content``/``_expand_assistant``/``_expand_user``/``_expand_message``/
``_build_oai_messages``/``build_openai_tools``) rather than copied from a cloud
vendor's Responses-API converter. Deliberately a separate copy, not an import
of ``llamacpp/_llama.py`` -- see :mod:`kodo.llms._cloud_registry`'s module
docstring on vendor packages being self-contained.

One deliberate difference from llama.cpp's version: a persisted ``thinking``
block is dropped outright rather than re-wrapped in ``<think>...</think>`` --
that re-wrap exists so a local model's own reasoning convention gets replayed
as real context, but Gemini has no analogous convention for ingesting a
foreign reasoning block back, matching how :mod:`kodo.llms.openai._convert`/
:mod:`kodo.llms.meta._convert` drop it for their own (Responses-API) shape.

There is no cache-breakpoint logic here: Gemini's OpenAI-compatible endpoint
has no ``cache_control`` marker mechanism, so ``LLMPlugin.stream_query``'s
``cache_breakpoints`` argument is simply accepted and ignored by the caller,
per that method's documented contract for providers without explicit caching.
"""

from __future__ import annotations

import json

from kodo.llms._interface import Message, ToolSpec
from kodo.llms._sanitize import strip_kodo_callouts
from kodo.toolspecs import tool_description

__all__ = ["build_chat_messages", "build_openai_tools"]


def _flatten_content(content: object) -> str:
    """Reduce nested Anthropic-style content blocks to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return " ".join(parts)
    return str(content)


def _expand_assistant(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, object]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "thinking":
            # Gemini has no item/field that accepts a foreign persisted
            # reasoning block back -- dropped, same as openai/meta's
            # Responses-API converters drop it for their own shape.
            continue
        elif block_type == "text":
            # One-way notifications to the user; never replayed as context.
            text_parts.append(strip_kodo_callouts(str(block.get("text", ""))))
        elif block_type == "tool_use":
            call: dict[str, object] = {
                "id": str(block.get("id", "")),
                "type": "function",
                "function": {
                    "name": str(block.get("name", "")),
                    "arguments": json.dumps(block.get("input", {})),
                },
            }
            # Gemini's thinking models attach a per-call `thought_signature`
            # (persisted onto this block by kodo.runtime._engine._turns'
            # `_tool_use_block`, read back off the matching ToolCallEvent the
            # google plugin yielded) that MUST be replayed verbatim on any
            # later request that includes this call, in the same
            # extra_content.google.thought_signature shape it arrived in --
            # otherwise Gemini rejects the whole request with HTTP 400
            # ("Function call is missing a thought_signature"). Omitted
            # entirely when absent (e.g. a non-thinking call, or a tool_use
            # block that originated from a different vendor).
            signature = block.get("thought_signature")
            if isinstance(signature, str):
                call["extra_content"] = {"google": {"thought_signature": signature}}
            tool_calls.append(call)
    msg: dict[str, object] = {
        "role": "assistant",
        "content": " ".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return [msg]


def _expand_user(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    text_parts: list[str] = []
    for block in blocks:
        if block.get("type") == "tool_result":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": _flatten_content(block.get("content", "")),
                }
            )
        elif block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
    if text_parts:
        result.append({"role": "user", "content": " ".join(text_parts)})
    return result


def _expand_message(msg: Message) -> list[dict[str, object]]:
    if isinstance(msg.content, str):
        content = strip_kodo_callouts(msg.content) if msg.role == "assistant" else msg.content
        return [{"role": msg.role, "content": content}]
    blocks = msg.content
    if msg.role == "assistant":
        return _expand_assistant(blocks)
    if msg.role == "user":
        return _expand_user(blocks)
    text = " ".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
    return [{"role": msg.role, "content": text}]


def build_chat_messages(system: str, messages: list[Message]) -> list[dict[str, object]]:
    """Convert kodo's conversation history into a Chat Completions ``messages`` list.

    Args:
        system: System prompt text, sent as the first ``system`` message.
        messages: Conversation history in chronological order.

    Returns:
        list[dict[str, object]]: The ``messages=`` parameter for
        ``client.chat.completions.create``.
    """
    result: list[dict[str, object]] = [{"role": "system", "content": system}]
    for msg in messages:
        result.extend(_expand_message(msg))
    return result


def build_openai_tools(tools: list[ToolSpec]) -> list[dict[str, object]]:
    """Render *tools* into the Chat Completions ``tools=[...]`` shape.

    Args:
        tools: Tool specifications the model may invoke.

    Returns:
        list[dict[str, object]]: Tool definitions for the ``tools=`` parameter.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": tool_description(t),
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]
