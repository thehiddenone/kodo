"""``query_frontier`` tool — reports the next artifact type per responsibility."""

from __future__ import annotations

import json

from kodo.workspace import ArtifactType

from ._tool import Tool

__all__ = ["QueryFrontierTool"]

# Canonical per-responsibility artifact execution order (DESIGN.md §2.2).
_PER_RESPONSIBILITY_ORDER: tuple[ArtifactType, ...] = (
    ArtifactType.FUNCTIONAL_DESIGN,
    ArtifactType.TEST_PLAN,
    ArtifactType.TEST,
    ArtifactType.CODE,
)


class QueryFrontierTool(Tool):
    """Return the earliest missing artifact type for each responsibility."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        frontier: list[dict[str, str]] = []
        completed = self.context.index.completed_entries()

        resp_codes: set[str] = {e.responsibility_code for e in completed}

        for resp_code in sorted(resp_codes):
            completed_types = {e.type for e in completed if e.responsibility_code == resp_code}
            for artifact_type in _PER_RESPONSIBILITY_ORDER:
                if artifact_type not in completed_types:
                    frontier.append(
                        {
                            "responsibility_code": resp_code,
                            "next_type": artifact_type.value,
                        }
                    )
                    break  # only report the earliest missing type per responsibility

        return json.dumps({"frontier": frontier})
