"""``edit_file`` tool — overwrites an existing file inside the project root."""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["EditFileTool"]

_log = logging.getLogger(__name__)


class EditFileTool(Tool):
    """Overwrite an existing file's contents (fails if it does not exist)."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        content = str(tool_input.get("content", ""))
        try:
            target = ctx.resolver.resolve(path)
            if not target.exists():
                raise FileNotFoundError(f"File not found: {path!r}")
            try:
                old_content: str | None = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # Diff capture is best-effort — a binary/unreadable previous
                # version must not block the edit itself.
                old_content = None
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            _log.info("edit_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        result: dict[str, object] = {"status": "edited", "path": path}
        if old_content is not None:
            # Undeclared field — not in EDIT_FILE.output_schema, so the engine's
            # normalize_output() strips it before it reaches the LLM or the UI
            # parameters table. It's an engine-only side channel (see
            # kodo.state.write_diff_files) that lets the WebView offer a
            # "view diff" link for this edit. Same convention is meant to be
            # reused by publish_artifact later.
            result["diff"] = {
                "label": path,
                "filename": target.name,
                "old_content": old_content,
                "new_content": content,
            }
        return json.dumps(result)
