"""Message/tool conversion for Kimi's OpenAI-compatible endpoint.

Kimi's API speaks Chat Completions and is OpenAI-SDK-compatible
(https://platform.moonshot.ai/docs/guide/migrating-from-openai-to-kimi) —
same wire shape as :mod:`kodo.llms.deepseek._convert`/
:mod:`kodo.llms.alibaba._convert`, and this module is adapted from the former
(itself adapted from Alibaba's, itself adapted from Google's). Deliberately a
separate copy, not an import of any of them — see
:mod:`kodo.llms._cloud_registry`'s module docstring on vendor packages being
self-contained.

A persisted ``thinking`` block is dropped outright on replay, same as every
other vendor here except Gemini. No Moonshot documentation was found
describing a ``thought_signature``-style mandatory replay requirement for a
prior turn's reasoning (unlike Gemini's own thinking models, see
doc/LLM_REGISTRY.md) — dropping it is the conservative default that matches
4 of the now-6 vendors in this codebase, flagged here as an assumption to
revisit if Moonshot's docs turn up a stricter requirement later.

There is no cache-breakpoint logic here: Kimi's own context caching is
automatic and prefix-based, hitting on unchanged prefixes aligned to 256-token
chunks (https://platform.kimi.ai/docs/guide/use-context-caching-feature-of-kimi-api),
with no ``cache_control`` marker mechanism to opt into — so
``LLMPlugin.stream_query``'s ``cache_breakpoints`` argument is simply accepted
and ignored by the caller, per that method's documented contract for
providers without explicit caching.
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
            # Dropped -- see module docstring on why this differs from
            # Gemini's mandatory-replay `thought_signature` handling.
            continue
        elif block_type == "text":
            # One-way notifications to the user; never replayed as context.
            text_parts.append(strip_kodo_callouts(str(block.get("text", ""))))
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
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
