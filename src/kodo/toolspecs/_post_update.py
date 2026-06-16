"""``post_update`` tool spec — placeholder, dispatch not yet implemented."""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["POST_UPDATE"]


POST_UPDATE: ToolSpec = ToolSpec(
    name="post_update",
    external_name="Post Progress Update",
    user_description="Post a progress update",
    description=(
        "Send a non-blocking progress update to the UI. Describes state "
        "transitions and decisions — never artifact content (no requirement "
        "text, design excerpts, or code)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Progress update text."},
        },
        "required": ["message"],
    },
    when_to_use=(
        "A stage starts or completes for a codename, or a product-level stage starts or completes.",
        "An escalation is triaged, an invalidation cascade executes, a "
        "substantive autonomous decision is made, or the break-glass is "
        "pulled.",
        "Recording that a stage is skipped because of an `excluded` verdict "
        "from its preceding review.",
    ),
)
