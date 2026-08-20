"""Message/tool conversion for Amazon Bedrock's Converse API.

Bedrock's Converse API (<https://docs.aws.amazon.com/bedrock/latest/userguide/
conversation-inference.html>) is its own normalised wire shape — neither
Anthropic Messages nor OpenAI Chat Completions — so this module is a genuine
third conversion, not an adaptation of :mod:`kodo.llms.openrouter._convert`
like the Chat-Completions-shaped vendors are of each other. It is still a
self-contained per-vendor copy by the rule in
:mod:`kodo.llms._cloud_registry`'s module docstring.

Three Converse-specific constraints drive the shape of the code here, each of
which is a hard ``ValidationException`` rather than a silently degraded
response:

* **No empty content arrays.** Every message must carry at least one content
  block, so messages that convert to nothing are dropped outright.
* **Roles must alternate.** Converse rejects two consecutive messages with the
  same role, which kodo's history can produce (e.g. an assistant text turn
  followed by a separate assistant tool-use turn), so
  :func:`_merge_consecutive` folds runs of one role into a single message.
* **``toolResult`` blocks belong to a user message**, which is already where
  kodo puts them.

A persisted ``thinking`` block is dropped on replay, the same conservative
default every vendor here except Gemini uses. Bedrock *does* define a
``reasoningContent`` block with a ``signature`` for round-tripping, but only
some upstream families produce one and the catalog this vendor draws from is
heterogeneous (doc/LLM_REGISTRY.md §3b) — replaying a signature to a model
that never issued one is the failure mode this avoids.

There is no cache-breakpoint logic here either. Bedrock does expose explicit
``cachePoint`` markers, but support is per-model and sending one to a model
that doesn't support it is a ``ValidationException`` — with no per-model
capability flag available from the catalog API (§3b), kodo cannot know which
is which, so ``LLMPlugin.stream_query``'s ``cache_breakpoints`` argument is
accepted and ignored per that method's documented contract for providers
without usable explicit caching. Same decision, same reason, as OpenRouter.
"""

from __future__ import annotations

from kodo.llms._interface import Message, ToolSpec
from kodo.llms._sanitize import strip_kodo_callouts
from kodo.toolspecs import tool_description

__all__ = ["build_converse_messages", "build_converse_tool_config", "build_system_blocks"]


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
    content: list[dict[str, object]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "thinking":
            # Dropped -- see module docstring.
            continue
        if block_type == "text":
            # One-way notifications to the user; never replayed as context.
            text = strip_kodo_callouts(str(block.get("text", ""))).strip()
            if text:
                content.append({"text": text})
        elif block_type == "tool_use":
            raw_input = block.get("input", {})
            content.append(
                {
                    "toolUse": {
                        "toolUseId": str(block.get("id", "")),
                        "name": str(block.get("name", "")),
                        # Converse takes the arguments as a real JSON value,
                        # not the serialized string Chat Completions uses.
                        "input": raw_input if isinstance(raw_input, dict) else {},
                    }
                }
            )
    return content


def _expand_user(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for block in blocks:
        if block.get("type") == "tool_result":
            content.append(
                {
                    "toolResult": {
                        "toolUseId": str(block.get("tool_use_id", "")),
                        "content": [{"text": _flatten_content(block.get("content", ""))}],
                    }
                }
            )
        elif block.get("type") == "text":
            text = str(block.get("text", "")).strip()
            if text:
                content.append({"text": text})
    return content


def _expand_message(msg: Message) -> tuple[str, list[dict[str, object]]]:
    """Return *msg* as a ``(role, content_blocks)`` pair for Converse."""
    role = "assistant" if msg.role == "assistant" else "user"
    if isinstance(msg.content, str):
        text = msg.content
        if msg.role == "assistant":
            text = strip_kodo_callouts(text)
        text = text.strip()
        return role, ([{"text": text}] if text else [])
    blocks = msg.content
    if msg.role == "assistant":
        return role, _expand_assistant(blocks)
    if msg.role == "user":
        return role, _expand_user(blocks)
    text = " ".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text").strip()
    return role, ([{"text": text}] if text else [])


def _merge_consecutive(turns: list[tuple[str, list[dict[str, object]]]]) -> list[dict[str, object]]:
    """Fold runs of same-role turns into one message each — see module docstring."""
    merged: list[dict[str, object]] = []
    for role, content in turns:
        if not content:
            continue
        if merged and merged[-1]["role"] == role:
            previous = merged[-1]["content"]
            if isinstance(previous, list):
                previous.extend(content)
            continue
        merged.append({"role": role, "content": list(content)})
    return merged


def build_system_blocks(system: str) -> list[dict[str, object]]:
    """Render the system prompt into Converse's separate ``system`` parameter.

    Args:
        system: System prompt text.

    Returns:
        list[dict[str, object]]: The ``system=`` parameter, or ``[]`` when
        there is no prompt (Converse rejects a block with empty text).
    """
    text = system.strip()
    return [{"text": text}] if text else []


def build_converse_messages(messages: list[Message]) -> list[dict[str, object]]:
    """Convert kodo's conversation history into Converse's ``messages`` list.

    Unlike the Chat-Completions vendors, the system prompt is **not** part of
    this list — Converse takes it in its own top-level ``system`` field (see
    :func:`build_system_blocks`).

    Args:
        messages: Conversation history in chronological order.

    Returns:
        list[dict[str, object]]: The ``messages=`` parameter for
        ``bedrock-runtime.converse_stream``, with empty messages dropped and
        same-role runs merged.
    """
    return _merge_consecutive([_expand_message(m) for m in messages])


def build_converse_tool_config(tools: list[ToolSpec]) -> dict[str, object]:
    """Render *tools* into Converse's ``toolConfig`` shape.

    Args:
        tools: Tool specifications the model may invoke.

    Returns:
        dict[str, object]: The ``toolConfig=`` parameter, or ``{}`` when there
        are no tools (Converse rejects an empty ``tools`` array).
    """
    if not tools:
        return {}
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t.name,
                    "description": tool_description(t),
                    "inputSchema": {"json": t.input_schema},
                }
            }
            for t in tools
        ]
    }
