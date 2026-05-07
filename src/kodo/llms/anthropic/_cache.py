"""Prompt-caching breakpoint logic for the Anthropic plugin.

Builds the ``system`` and ``messages`` structures that the Anthropic API
expects when prompt caching is enabled.  Each function returns plain dicts
that satisfy the SDK's TypedDict requirements.
"""

from __future__ import annotations

from kodo.llms._interface import Message

__all__ = ["build_system_blocks", "build_message_params"]

# Cache-control marker used by all breakpoints
_EPHEMERAL: dict[str, object] = {"type": "ephemeral"}


def build_system_blocks(system: str, *, cache: bool = True) -> list[dict[str, object]]:
    """Convert a plain system string into an Anthropic ``system`` block list.

    When ``cache=True`` the block is marked with ``cache_control`` so the
    system prompt is cached on the first call and reused on subsequent calls
    in the same conversation.

    Args:
        system: The full system prompt text.
        cache: Whether to attach a ``cache_control`` breakpoint.

    Returns:
        list[dict[str, object]]: A one-element list of text blocks suitable
        for passing as ``system=`` to the Anthropic client.
    """
    block: dict[str, object] = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = _EPHEMERAL
    return [block]


def build_message_params(
    messages: list[Message],
    cache_breakpoints: list[int],
) -> list[dict[str, object]]:
    """Convert :class:`~kodo.llms._interface.Message` objects to Anthropic params.

    Messages at indices listed in ``cache_breakpoints`` have their last
    content block marked with ``cache_control`` so the prompt cache
    checkpoint is placed after that message.

    Args:
        messages: Conversation history.
        cache_breakpoints: Zero-based indices into ``messages`` to mark
            with ``cache_control``.

    Returns:
        list[dict[str, object]]: Message parameter dicts for the Anthropic
        API ``messages`` argument.
    """
    breakpoint_set = set(cache_breakpoints)
    result: list[dict[str, object]] = []

    for i, msg in enumerate(messages):
        if isinstance(msg.content, str):
            if i in breakpoint_set:
                content: list[dict[str, object]] = [
                    {"type": "text", "text": msg.content, "cache_control": _EPHEMERAL}
                ]
            else:
                content = [{"type": "text", "text": msg.content}]
        else:
            # Already a list of content blocks — copy and optionally mark last
            content = [dict(block) for block in msg.content]
            if i in breakpoint_set and content:
                content[-1]["cache_control"] = _EPHEMERAL

        result.append({"role": msg.role, "content": content})

    return result
