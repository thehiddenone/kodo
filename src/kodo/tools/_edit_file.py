"""``edit_file`` tool — targeted string-match replacement inside the project root.

Replaces one exact, unique occurrence of ``old_string`` with ``new_string`` and
leaves the rest of the file byte-for-byte untouched. The match must be unique:
zero matches (not found) and more-than-one match (ambiguous) both fail loudly
without writing anything, so the model cannot silently edit the wrong place. The
whole-file counterpart is :class:`~kodo.tools._rewrite_file.RewriteFileTool`.
"""

from __future__ import annotations

import json
import logging

from ._tool import Tool

__all__ = ["EditFileTool"]

_log = logging.getLogger(__name__)


class EditFileTool(Tool):
    """Replace one exact, unique snippet of an existing file."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        old_string = str(tool_input.get("old_string", ""))
        new_string = str(tool_input.get("new_string", ""))

        if old_string == "":
            return json.dumps(
                {"error": "old_string must not be empty — provide the exact text to replace."}
            )
        if old_string == new_string:
            return json.dumps(
                {"error": "old_string and new_string are identical — nothing to change."}
            )

        try:
            target = ctx.resolver.resolve(path)
            if not target.exists():
                raise FileNotFoundError(f"File not found: {path!r}")
            old_content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.info("edit_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        occurrences = old_content.count(old_string)
        if occurrences == 0:
            return json.dumps(
                {
                    "error": (
                        f"old_string not found in {path!r}. It must match the file "
                        "content exactly, including whitespace and indentation."
                    )
                }
            )
        if occurrences > 1:
            return json.dumps(
                {
                    "error": (
                        f"old_string is not unique in {path!r} ({occurrences} matches). "
                        "Include more surrounding context so it identifies exactly one "
                        "location."
                    )
                }
            )

        new_content = old_content.replace(old_string, new_string, 1)
        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            _log.info("edit_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        result: dict[str, object] = {"status": "edited", "path": path}
        # Undeclared field — not in EDIT_FILE.output_schema, so the engine's
        # normalize_output() strips it before it reaches the LLM or the UI
        # parameters table. It's an engine-only side channel (see
        # kodo.state.write_diff_files) that lets the WebView offer a
        # "view diff" link for this edit.
        result["diff"] = {
            "label": path,
            "filename": target.name,
            "old_content": old_content,
            "new_content": new_content,
        }
        return json.dumps(result)
