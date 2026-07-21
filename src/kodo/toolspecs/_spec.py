"""The ToolSpec dataclass shared by every tool specification in this package."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "OUTPUT_VISIBILITY_DEFAULT",
    "VISIBILITY_ALWAYS",
    "VISIBILITY_HIDDEN",
    "VISIBILITY_VALUES",
    "VISIBILITY_VISIBLE",
    "SecurityImpact",
    "ToolSpec",
]


class SecurityImpact(IntEnum):
    """How much damage a tool could do if misused — a 7-level threat scale.

    Ordered ``NONE`` (0, read-only / inert) through ``CRITICAL`` (6, arbitrary
    side effects). The numeric value is the threat level; :attr:`label` is the
    user-friendly name shown in the UI and rendered into agent prompts. The
    security layer (not yet wired) will consume these levels to decide which
    calls need a permission gate.
    """

    NONE = 0
    MINIMAL = 1
    LOW = 2
    MODERATE = 3
    HIGH = 4
    SEVERE = 5
    CRITICAL = 6

    @property
    def label(self) -> str:
        """Title-cased, user-friendly name for this threat level."""
        return self.name.capitalize()


# Per-property visibility values for ``input_visibility`` / ``output_visibility``.
VISIBILITY_ALWAYS = "always"  # shown in full, never cropped
VISIBILITY_VISIBLE = "visible"  # shown, but cropped if large (3 lines / 200 chars)
VISIBILITY_HIDDEN = "hidden"  # never shown to the customer
VISIBILITY_VALUES: frozenset[str] = frozenset(
    {VISIBILITY_ALWAYS, VISIBILITY_VISIBLE, VISIBILITY_HIDDEN}
)

# A property absent from a visibility map defaults to hidden (most private).
OUTPUT_VISIBILITY_DEFAULT = VISIBILITY_HIDDEN


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
        output_schema: JSON Schema dict for the tool's successful result. It is
            an ``object`` schema; the engine augments it in-flight with an
            engine-owned ``schema_compliance`` boolean (see
            :mod:`kodo.toolspecs._compliance`) before showing it to agents — so
            specs must NOT declare ``schema_compliance`` themselves. Tools may
            also return an ``{"error": "..."}`` object on failure; that envelope
            is universal and is not modelled per-spec.
        security_impact: The :class:`SecurityImpact` threat level for this tool.
        input_visibility: Map of input property name → visibility
            (``always`` / ``visible`` / ``hidden``); see the ``VISIBILITY_*``
            constants. Properties absent from the map default to ``hidden``.
        output_visibility: Map of output property name → visibility, same values
            and default as :attr:`input_visibility`.
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
        requires_project: Whether this tool needs a bound project/workspace
            to run. :class:`~kodo.tools.ToolDispatcher` rejects a call to such
            a tool before dispatch (with a message to call
            ``create_new_project`` first) when no project is bound and the
            call doesn't carry ``temporary: true`` — see
            :mod:`kodo.toolspecs._workspace`.
    """

    name: str
    external_name: str
    user_description: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    security_impact: SecurityImpact
    input_visibility: dict[str, str]
    output_visibility: dict[str, str]
    when_to_use: tuple[str, ...]
    autonomous_mode: str | None = None
    requires_project: bool = False
