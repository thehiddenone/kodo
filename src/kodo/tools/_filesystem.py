"""``filesystem`` tool — one handler for every file/directory operation.

Dispatches on the ``operation`` field to delete, copy, or move a file or a
directory. Every path is resolved against the project root and rejected if it
would escape it. This handler replaces the former per-operation file tools
(``delete_file`` / ``copy_file`` / ``move_file``) and adds their directory
counterparts. Creating a brand-new file lives in the separate ``create_file``
tool (:class:`~kodo.tools._create_file.CreateFileTool`); creating a directory
lives in the separate ``create_directory`` tool
(:class:`~kodo.tools._create_directory.CreateDirectoryTool`).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from ._tool import Tool

__all__ = ["FilesystemTool"]

_log = logging.getLogger(__name__)


class FilesystemTool(Tool):
    """Perform one filesystem operation selected by ``operation``."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        operation = str(tool_input.get("operation", ""))
        try:
            handler = self._HANDLERS.get(operation)
            if handler is None:
                raise ValueError(
                    f"Unknown operation {operation!r}; expected one of: "
                    + ", ".join(sorted(self._HANDLERS))
                )
            result = handler(self, tool_input)
        except (OSError, ValueError) as exc:
            _log.info("filesystem(%s) from %s failed: %s", operation, ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})
        return json.dumps(result)

    # -- per-operation handlers ------------------------------------------------
    # Each resolves its paths, performs the work, and returns the success
    # envelope. They raise OSError/ValueError on failure; handle() catches it.

    def _delete_file(self, tool_input: dict[str, object]) -> dict[str, object]:
        path = str(tool_input.get("path", ""))
        target = self.context.resolver.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path!r}")
        if target.is_dir():
            raise IsADirectoryError(f"Not a file (use delete_dir): {path!r}")
        target.unlink()
        return {"status": "deleted", "operation": "delete_file", "path": path}

    def _delete_dir(self, tool_input: dict[str, object]) -> dict[str, object]:
        path = str(tool_input.get("path", ""))
        target = self.context.resolver.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"Directory not found: {path!r}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory (use delete_file): {path!r}")
        shutil.rmtree(target)
        return {"status": "deleted", "operation": "delete_dir", "path": path}

    def _copy_file(self, tool_input: dict[str, object]) -> dict[str, object]:
        src, dst, source, destination = self._resolve_pair(tool_input)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source!r}")
        if src.is_dir():
            raise IsADirectoryError(f"Source is a directory (use copy_dir): {source!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return self._pair_result("copied", "copy_file", source, destination)

    def _copy_dir(self, tool_input: dict[str, object]) -> dict[str, object]:
        src, dst, source, destination = self._resolve_pair(tool_input)
        if not src.is_dir():
            raise NotADirectoryError(f"Source directory not found: {source!r}")
        if dst.exists():
            raise FileExistsError(f"Destination already exists: {destination!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        return self._pair_result("copied", "copy_dir", source, destination)

    def _move_file(self, tool_input: dict[str, object]) -> dict[str, object]:
        src, dst, source, destination = self._resolve_pair(tool_input)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source!r}")
        if src.is_dir():
            raise IsADirectoryError(f"Source is a directory (use move_dir): {source!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dst)
        return self._pair_result("moved", "move_file", source, destination)

    def _move_dir(self, tool_input: dict[str, object]) -> dict[str, object]:
        src, dst, source, destination = self._resolve_pair(tool_input)
        if not src.is_dir():
            raise NotADirectoryError(f"Source directory not found: {source!r}")
        if dst.exists():
            raise FileExistsError(f"Destination already exists: {destination!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dst)
        return self._pair_result("moved", "move_dir", source, destination)

    # -- helpers ---------------------------------------------------------------

    def _resolve_pair(self, tool_input: dict[str, object]) -> tuple[Path, Path, str, str]:
        source = str(tool_input.get("source", ""))
        destination = str(tool_input.get("destination", ""))
        return (
            self.context.resolver.resolve(source),
            self.context.resolver.resolve(destination),
            source,
            destination,
        )

    @staticmethod
    def _pair_result(
        status: str, operation: str, source: str, destination: str
    ) -> dict[str, object]:
        return {
            "status": status,
            "operation": operation,
            "source": source,
            "destination": destination,
        }

    _HANDLERS = {
        "delete_file": _delete_file,
        "delete_dir": _delete_dir,
        "copy_file": _copy_file,
        "copy_dir": _copy_dir,
        "move_file": _move_file,
        "move_dir": _move_dir,
    }
