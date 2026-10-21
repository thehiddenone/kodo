"""``report_agent_review`` tool — hand one agent review's verdict to the engine.

Dispatch handler for :data:`kodo.toolspecs.REPORT_AGENT_REVIEW`. It checks the
review's shape and hands it to :meth:`~kodo.tools.EngineServices.report_agent_review`,
which shows it and offers Agent Builder on its concerns (doc/AGENT_BUILDER.md §5.3).
"""

from __future__ import annotations

import json

from ._context import AgentReview, AgentReviewConcern
from ._tool import Tool

__all__ = ["ReportAgentReviewTool"]

_CONCERN_FIELDS = ("rule", "quote", "location", "fix")


class ReportAgentReviewTool(Tool):
    """Report Agent Critic's review of one agent to the engine."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        review = self.__parse(tool_input)
        if isinstance(review, str):
            return json.dumps({"error": f"{review}; retry."})
        refusal = await self.context.services.report_agent_review(review)
        if refusal is not None:
            return json.dumps({"error": refusal})
        return json.dumps({"reported": True, "concerns": len(review.concerns)})

    @staticmethod
    def __parse(tool_input: dict[str, object]) -> AgentReview | str:
        """The review *tool_input* describes, or what is wrong with it."""
        name = tool_input.get("name")
        if not isinstance(name, str) or not name.strip():
            return "'name' must be the reviewed agent's name"
        accept = tool_input.get("accept")
        if not isinstance(accept, bool):
            return "'accept' must be true or false"
        summary = tool_input.get("summary")
        if not isinstance(summary, str):
            return "'summary' must be a string"
        raw_concerns = tool_input.get("concerns")
        if not isinstance(raw_concerns, list):
            return "'concerns' must be an array"
        concerns: list[AgentReviewConcern] = []
        for i, raw in enumerate(raw_concerns):
            if not isinstance(raw, dict):
                return f"concerns[{i}] is not an object"
            values = {key: raw.get(key) for key in _CONCERN_FIELDS}
            missing = [key for key, value in values.items() if not isinstance(value, str)]
            if missing:
                return f"concerns[{i}] needs string fields {', '.join(missing)}"
            fields = {key: str(value).strip() for key, value in values.items()}
            if not fields["rule"] or not fields["fix"]:
                return f"concerns[{i}] needs a non-empty 'rule' and 'fix'"
            concerns.append(AgentReviewConcern(**fields))
        if accept == bool(concerns):
            return "'accept' is true exactly when 'concerns' is empty"
        return AgentReview(
            name=name.strip(), accept=accept, concerns=tuple(concerns), summary=summary.strip()
        )
