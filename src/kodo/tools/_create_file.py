"""``create_file`` tool — whole-file creation inside the project root.

Writes ``content`` verbatim at ``path``. Never touches an existing file: if
one is already there, the call fails loudly and nothing is written, so the
model cannot silently clobber existing content. Changing an existing file's
contents lives in the separate ``edit_file`` tool
(:class:`~kodo.tools._edit_file.EditFileTool`).
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["CreateFileTool"]

_log = logging.getLogger(__name__)


class CreateFileTool(Tool):
    """Create a brand-new file with the given content."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))

        try:
            target = ctx.resolver.resolve(path)
            if target.exists():
                raise FileExistsError(
                    f"File already exists: {path!r}. Use edit_file to change an "
                    "existing file's contents — create_file never modifies one."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            _log.info("create_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        result: dict[str, object] = {"status": "created", "path": path}
        # Undeclared field — not in CREATE_FILE.output_schema, so the engine's
        # normalize_output() strips it before it reaches the LLM or the UI
        # parameters table. It's an engine-only side channel (see
        # kodo.state.write_diff_files) that lets the WebView offer a
        # "view diff" link for this creation.
        result["diff"] = {
            "label": path,
            "filename": target.name,
            "old_content": "",
            "new_content": content,
        }
        return json.dumps(result)
