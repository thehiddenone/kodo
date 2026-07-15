"""``read_attachment`` tool spec — fetch a prompt attachment's content by ID."""

from __future__ import annotations

from ._spec import SecurityImpact, ToolSpec

__all__ = ["READ_ATTACHMENT"]


READ_ATTACHMENT: ToolSpec = ToolSpec(
    name="read_attachment",
    external_name="Read Attachment",
    user_description="Read an attached file's content",
    description=(
        "Return the full text content of a file the user attached to a prompt. "
        "Attachments never appear inline — after the user's message you will see "
        "one self-closing tag per attached file, for example:\n"
        '<ATTACHMENT ID="3fa85f64-5717-4562-b3fc-2c963f66afa6" filename="notes.txt"/>\n'
        "Call this tool with that tag's `attachment_id` to read the file. Always "
        "check the end of the user's message for these tags and read any "
        "attachment that is relevant to the request — do not assume you already "
        "know its content."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "attachment_id": {
                "type": "string",
                "description": (
                    'The `ID` value from an <ATTACHMENT ID="..." filename="..."/> tag.'
                ),
            },
        },
        "required": ["attachment_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "The attachment's original filename.",
            },
            "content": {
                "type": "string",
                "description": "The attachment's full text content.",
            },
        },
        "required": ["filename", "content"],
    },
    security_impact=SecurityImpact.MINIMAL,
    input_visibility={"attachment_id": "always"},
    output_visibility={"filename": "always", "content": "visible"},
    when_to_use=(
        "The user's prompt (or an earlier message in this conversation) ends with one or more "
        '<ATTACHMENT ID="..." filename="..."/> tags and the request depends on that '
        "file's content.",
        "Before answering a question about an attached file, or acting on its content — the tag "
        "alone only gives you the filename, not the content.",
    ),
)
