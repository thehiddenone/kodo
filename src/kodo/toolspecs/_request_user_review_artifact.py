"""``request_user_review_artifact`` tool spec — leaf sub-agent report tool.

A critic or solo agent presents a converged artifact for the user's
accept/feedback. Auto-accepted in autonomous mode.
"""

from __future__ import annotations

from ._spec import ToolSpec

__all__ = ["REQUEST_USER_REVIEW_ARTIFACT"]


REQUEST_USER_REVIEW_ARTIFACT: ToolSpec = ToolSpec(
    name="request_user_review_artifact",
    external_name="Request Review",
    user_description="Request user review of an artifact",
    description=(
        "Present a converged, just-published artifact to the user for "
        "accept/feedback by its artifact_id. The user acts as critic, judging "
        "a finished artifact. Blocks until the user responds; accept ends the "
        "review gate, feedback opens a revision round. Held by critics and solo "
        "agents — the agent that owns the convergence verdict. In autonomous "
        "mode the engine auto-accepts and returns immediately, so call it "
        "unconditionally without branching on mode."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {
                "type": "string",
                "description": (
                    "Workspace ID of the artifact to present, as returned by "
                    "the preceding publish_artifact call."
                ),
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph summary shown to the user with the artifact.",
            },
        },
        "required": ["artifact_id"],
    },
    when_to_use=(
        "A review's verdict on an artifact is `accepted` and the artifact "
        "is ready for the user's sign-off.",
        "An artifact has been published without a separate review step and "
        "is considered ready, and the user — the source of the underlying "
        "information — should confirm the synthesis captures their intent.",
        'Never used to ask whether an artifact "looks ok" mid-draft — that '
        "is what `ask_user` is for. This tool is a structured sign-off on a "
        "specific, finished `artifact_id`.",
    ),
    autonomous_mode=(
        "auto-accepted — when the user is away, the engine synthesizes an "
        "accept and returns immediately, so the caller fires it "
        "unconditionally without branching on mode."
    ),
)
