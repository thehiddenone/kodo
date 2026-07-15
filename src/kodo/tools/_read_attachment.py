"""``read_attachment`` tool — fetch a prompt attachment's content by ID.

Attachments are manifested to the LLM as ``<ATTACHMENT ID="..." filename="..."/>``
tags (:func:`kodo.runtime._attachments.inject_attachments`), never inlined.
This tool resolves the ID straight from disk via
``kodo.project.session_attachments_dir`` — the stored filename embeds the same
ID (``<id>__<basename>``, see ``TransientStore.store_attachment``) — so no
session-storage collaborator needs to be threaded through ``ToolContext``.
"""

from __future__ import annotations

import json
import uuid

from kodo.project import session_attachments_dir

from ._tool import Tool

__all__ = ["ReadAttachmentTool"]


class ReadAttachmentTool(Tool):
    """Read back one prompt attachment's stored text by its tag ID."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        attachment_id = str(tool_input.get("attachment_id", "")).strip()
        try:
            uuid.UUID(attachment_id)
        except ValueError:
            return json.dumps({"error": f"Not a valid attachment ID: {attachment_id!r}"})

        attachments_dir = session_attachments_dir(self.context.session_id)
        matches = sorted(attachments_dir.glob(f"{attachment_id}__*"))
        if not matches:
            return json.dumps(
                {"error": f"No attachment found with ID {attachment_id!r}. It may be unavailable."}
            )

        target = matches[0]
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as exc:
            return json.dumps({"error": f"Could not read attachment: {exc}"})

        _, _, filename = target.name.partition("__")
        return json.dumps({"filename": filename or target.name, "content": content})
