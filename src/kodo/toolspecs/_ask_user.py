"""``ask_user`` tool spec — batched user questioning.

An agent gathers every open question about its current topic of work into one
call; the user answers them all in a single WebView form. Withheld in
autonomous mode (no answer to synthesize). The questioning discipline itself —
think first, derive real candidate answers, top choice first — lives in the
performance preamble ("Asking the User Questions").
"""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["ASK_USER"]


ASK_USER: ToolSpec = ToolSpec(
    name="ask_user",
    external_name="Ask User",
    user_description="Ask the user questions",
    description=(
        "Present the user a set of questions and block until they confirm "
        "answers to all of them. Bundle EVERY open question about the topic "
        "you are working on into one call — never a drip of single-question "
        "calls. Each question carries the candidate answers you derived "
        "yourself (your best assumption FIRST); the UI automatically appends "
        "a free-text option to every question, so never add an 'Other'/'free "
        "text' option yourself. See the 'Asking the User Questions' preamble "
        "section for the full discipline. Unavailable in autonomous mode "
        "(there is no answer to synthesize when the user is away); assume and "
        "document, or escalate_blocker, instead. Distinct from "
        "request_user_review_artifact, which is a sign-off on a finished "
        "artifact rather than a question."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "All questions for the current topic, in the order the "
                    "user should read them."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": (
                                "One focused question in plain language. No "
                                "bundled sub-questions — split them into "
                                "separate entries of this array."
                            ),
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["single_choice", "multi_choice"],
                            "description": (
                                "single_choice: the answers are mutually "
                                "exclusive, the user picks exactly one (an "
                                "option or their free text). multi_choice: "
                                "several answers can apply, the user picks "
                                "one or more."
                            ),
                        },
                        "options": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": (
                                "The candidate answers you derived — the "
                                "assumptions you could make yourself. Your "
                                "single best assumption comes FIRST (it is "
                                "not marked in any other way), the rest in "
                                "descending plausibility. Do NOT add an "
                                "'Other'/'free text'/'none of the above' "
                                "option: the UI always appends a free-text "
                                "field as the last option."
                            ),
                        },
                    },
                    "required": ["question", "kind", "options"],
                },
            },
        },
        "required": ["questions"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "description": (
                    "One entry per question, in the same order. 'selected' "
                    "echoes the chosen option texts verbatim (empty when the "
                    "user answered only in free text); 'free_text' is the "
                    "user's own text, or null when they did not use it."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "selected": {"type": "array", "items": {"type": "string"}},
                        "free_text": {"type": ["string", "null"]},
                    },
                    "required": ["selected", "free_text"],
                },
            },
        },
        "required": ["answers"],
    },
    security_impact=SecurityImpact.NONE,
    input_visibility={"questions": "always"},
    output_visibility={"answers": "always"},
    when_to_use=(
        "Eliciting every uncovered or partially-covered piece of information "
        "about the current topic of work in one batch, or resolving "
        "contradictions in user-supplied input before incorporating it — all "
        "open questions in one call, each with your derived candidate "
        "answers.",
    ),
    autonomous_mode=(
        "Unavailable — there is no answer to synthesize when the user is "
        "away, so this tool is withheld entirely. An agent that would have "
        "asked must instead assume-and-document or, if blocked, "
        "`escalate_blocker`."
    ),
)
