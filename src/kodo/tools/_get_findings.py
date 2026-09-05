"""``get_findings`` tool — the shared author/critic backlog (Guided mode only)."""

from __future__ import annotations

import json

from kodo.findings import outstanding_findings, read_findings

from ._tool import Tool

__all__ = ["GetFindingsTool"]


class GetFindingsTool(Tool):
    """Report the findings recorded against the work product under review.

    Auto-scoped: the engine binds the round's target work product to the run
    (``ToolContext.findings_key``/``findings_dir``), so this tool takes no
    argument naming one. With no scope bound — outside a review round, or on an
    author's first pass before anything is written — it answers with an empty
    list rather than an error, which is what keeps one prompt correct on every
    pass (doc/FINDINGS.md §3).

    One backlog covers every file the round produced, so a finding carries its
    own ``locations`` naming which file(s) it is about.
    """

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        if ctx.mode != "guided":
            return json.dumps({"error": "get_findings is only available in Guided mode."})
        if ctx.findings_dir is None or not ctx.findings_key:
            return json.dumps({"findings": []})
        findings = read_findings(ctx.findings_dir, ctx.findings_key)
        if not bool(tool_input.get("show_all")):
            findings = outstanding_findings(findings)
        return json.dumps({"findings": findings})
