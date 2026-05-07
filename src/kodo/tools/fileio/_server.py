"""MCP stdio server providing file create, edit, delete, copy, and move tools."""

from __future__ import annotations

import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP


class FileIO:
    """MCP stdio server that exposes file I/O operations as tools.

    When ``base_dir`` is supplied all paths are validated to stay inside it,
    preventing path-traversal outside the designated workspace.
    """

    __base_dir: Path | None
    __app: FastMCP

    def __init__(self, base_dir: str | Path | None = None) -> None:
        """Initialise the server and register all file-operation tools.

        Args:
            base_dir (str | Path | None): Optional root directory.  When set,
                every file-path argument is resolved and checked to be inside
                this directory.  Pass ``None`` to allow unrestricted access.
        """
        self.__base_dir = Path(base_dir).resolve() if base_dir else None
        self.__app = FastMCP("kodo-fileio")

        self.__app.tool(
            name="create_file",
            description="Create a new file with content. Fails if the file already exists.",
        )(self.__create_file)

        self.__app.tool(
            name="edit_file",
            description="Replace the entire content of an existing file.",
        )(self.__edit_file)

        self.__app.tool(
            name="delete_file",
            description="Delete a file permanently.",
        )(self.__delete_file)

        self.__app.tool(
            name="copy_file",
            description="Copy a file to a new location, preserving metadata.",
        )(self.__copy_file)

        self.__app.tool(
            name="move_file",
            description="Move or rename a file.",
        )(self.__move_file)

    def run(self) -> None:
        """Start the MCP stdio server and block until the client disconnects."""
        self.__app.run(transport="stdio")

    def __resolve(self, path: str) -> Path:
        resolved = Path(path).resolve()
        if self.__base_dir is not None:
            try:
                resolved.relative_to(self.__base_dir)
            except ValueError as exc:
                raise PermissionError(
                    f"Path {path!r} is outside the allowed directory {str(self.__base_dir)!r}"
                ) from exc
        return resolved

    def __create_file(self, path: str, content: str) -> str:
        target = self.__resolve(path)
        if target.exists():
            raise FileExistsError(f"File already exists: {path!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Created: {path}"

    def __edit_file(self, path: str, content: str) -> str:
        target = self.__resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path!r}")
        target.write_text(content, encoding="utf-8")
        return f"Edited: {path}"

    def __delete_file(self, path: str) -> str:
        target = self.__resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path!r}")
        target.unlink()
        return f"Deleted: {path}"

    def __copy_file(self, source: str, destination: str) -> str:
        src = self.__resolve(source)
        dst = self.__resolve(destination)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return f"Copied: {source!r} → {destination!r}"

    def __move_file(self, source: str, destination: str) -> str:
        src = self.__resolve(source)
        dst = self.__resolve(destination)
        if not src.exists():
            raise FileNotFoundError(f"Source not found: {source!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dst)
        return f"Moved: {source!r} → {destination!r}"
