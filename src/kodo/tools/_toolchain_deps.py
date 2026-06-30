"""``toolchain_deps`` tool — dependency management. Not yet implemented.

Deliberately out of scope for this pass: every call returns a clear
"not implemented" response so an agent gets a usable answer rather than an
unhandled-tool error. Real dependency management is a future addition.
"""

from __future__ import annotations

import json

from ._tool import Tool

__all__ = ["ToolchainDepsTool"]

_NOT_IMPLEMENTED_MESSAGE = (
    "Dependency management is not implemented yet. See DEVELOPMENT.md's "
    "Dependency Management section for the manifest commands, or use "
    "run_command with the toolchain's native package manager directly."
)


class ToolchainDepsTool(Tool):
    """Stub: always reports dependency management as not implemented."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        return json.dumps({"success": False, "message": _NOT_IMPLEMENTED_MESSAGE})
