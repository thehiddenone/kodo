"""The ToolSpec dataclass shared by every tool specification in this package."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ToolSpec"]


@dataclass(frozen=True)
class ToolSpec:
    """Specification for a tool the model may invoke.

    Attributes:
        name: Tool name (e.g. ``'publish_artifact'``) — the internal name agents
            and the harness use in tool calls.
        external_name: User-facing name (e.g. ``'Publish Artifact'``) — used to
            label this tool wherever it is shown to a human, including the
            ``## Tools`` section headings rendered by
            :class:`~kodo.subagents._registry.AgentRegistry`.
        user_description: Short (4-5 word) human-friendly summary of what this
            tool does, for UI display (e.g. tool-call events).
        description: Description sent to the LLM as part of the tool
            definition — what the model reads to decide whether and how to
            call this tool.
        input_schema: JSON Schema dict for the tool's input parameters.
        when_to_use: Bullet points of situations that call for this tool,
            rendered into the ``## Tools`` section of every agent prompt that
            is granted this tool. Each bullet is phrased generically — it
            describes a situation, not which agent is in it — since the same
            text may be rendered into multiple agents' prompts.
        autonomous_mode: How this tool behaves when the user is away, or
            ``None`` if it behaves identically in both modes. Two values
            matter to the engine: ``"unavailable"`` (the tool is withheld
            entirely — excluded from the agent's tool list and its rendered
            ``## Tools`` section) and ``"auto-accepted"`` (the tool stays
            available but the engine synthesizes the user's response). The
            full string (e.g. ``"unavailable — ..."``) is rendered verbatim
            into the ``## Tools`` section.
    """

    name: str
    external_name: str
    user_description: str
    description: str
    input_schema: dict[str, object]
    when_to_use: tuple[str, ...]
    autonomous_mode: str | None = None
