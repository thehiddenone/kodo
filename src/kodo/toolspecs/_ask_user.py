"""``ask_user`` tool spec — batched user questioning.

An agent gathers every open question about its current topic of work into one
call; the user answers them all in a single WebView form. Always granted,
in interactive and autonomous sessions alike — an agent never needs to check
whether it is available or branch its own behavior on the session's mode. When
there is no user to answer (an autonomous session), the tool synthesizes an
answer instead of blocking: see ``kodo.tools.AskUserTool``.

The questioning discipline — think first, batch every question, derive real
candidate answers, best assumption first — lives in :attr:`description` below,
**not** in a shared prompt block. It used to be an "Asking the User Questions"
section of the block every agent includes, which meant all of them read ~380
words about a tool only four of them hold, alongside a near-duplicate half of
the same rules here. Keeping it on the spec makes the guidance self-gating: it
reaches exactly the agents granted the tool, and there is one copy to maintain.
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
        "answers to all of them. This is your only channel for questions, and "
        "it carries a strict discipline:\n"
        "- **Think before you ask.** Work through the topic first and identify "
        "*everything* genuinely unclear about it, then ask all of it in ONE "
        "call — never a drip of single-question calls for things you could "
        "have foreseen together.\n"
        "- **Derive the answers yourself first.** For each question, list the "
        "real candidate answers — the assumptions you could defensibly make on "
        "your own. Those are the `options`. Your single best assumption goes "
        "FIRST (the top choice is never marked in any other way), the rest in "
        "descending plausibility. Options are genuine answers, not "
        "placeholders.\n"
        "- **Never add a free-text option.** The UI appends a free-text field "
        "as the last option of every question; an 'Other'/'none of the above' "
        "option of your own just duplicates it.\n"
        "- **Act on the full set.** The user answers everything at once and "
        "confirms. Incorporate the whole batch before proceeding. A follow-up "
        "call is justified only for questions the earlier answers *newly "
        "opened* — never to re-ask what an earlier answer already covered, "
        "even indirectly.\n"
        "- **It always returns an answer**, whether or not anyone is there to "
        "give one — never check for or reason about whether a user is present. "
        "See `answers` in the output for how a no-user answer looks and what "
        "to do with it.\n\n"
        "When to use: information about the current topic of work is "
        "uncovered or only partially covered, or user-supplied input "
        "contradicts itself and must be reconciled before you incorporate it. "
        "Always call it when a genuine open question exists."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "All questions for the current topic, in the order the user should read them."
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
                    "user's own text, or null when they did not use it. When "
                    "no user was present to answer, a single_choice question "
                    "comes back with its first option selected (your own "
                    "stated best guess) and a multi_choice question comes "
                    "back with 'selected' empty and 'free_text' carrying a "
                    "notice that nobody was there to answer — read that as an "
                    "instruction to decide for yourself, not a real preference."
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
    autonomous_mode=(
        "Auto-accepted — the tool stays available, and with no user present "
        "to answer, its own handler synthesizes an answer instead of blocking "
        "(see `output_schema` above). An agent never needs to check the mode "
        "or branch its own behavior on it."
    ),
)
