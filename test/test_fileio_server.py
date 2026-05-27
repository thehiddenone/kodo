"""Behavior tests for kodo.tools.fileio._server.FileIO.

FileIO registers file-operation tools with an MCP FastMCP app.
Tests cover the constructor (tool registration) and the path-sandboxing
invariant observable by listing the registered tools and checking
the base_dir constraint through tool metadata introspection.

Note: FileIO's tool implementations are private methods bound to the instance.
Direct behavioral tests of file operations require calling those bound methods
via the Tool.fn attribute — this is the only way to exercise them without
spawning a real MCP server process.  The fn attribute is public on the
mcp.server.fastmcp.tools.base.Tool Pydantic model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodo.tools.fileio._server import FileIO

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app(instance: FileIO) -> object:
    """Return the FastMCP app stored by the FileIO constructor."""
    return vars(instance)["_FileIO__app"]


def _tool_fn(instance: FileIO, tool_name: str) -> object:
    """Return the bound function for a registered tool."""
    app = _app(instance)
    return app._tool_manager._tools[tool_name].fn


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_fileio_can_be_created_without_base_dir() -> None:
    """
    Given no base_dir argument,
    when FileIO is instantiated,
    then no exception is raised.
    """
    FileIO()


def test_fileio_can_be_created_with_path_base_dir(tmp_path: Path) -> None:
    """
    Given a Path object as base_dir,
    when FileIO is instantiated,
    then no exception is raised.
    """
    FileIO(base_dir=tmp_path)


def test_fileio_can_be_created_with_string_base_dir(tmp_path: Path) -> None:
    """
    Given a string path as base_dir,
    when FileIO is instantiated,
    then no exception is raised.
    """
    FileIO(base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# Registered tool names
# ---------------------------------------------------------------------------


def _tool_names(instance: FileIO) -> list[str]:
    return list(_app(instance)._tool_manager._tools.keys())


def test_fileio_registers_create_file_tool() -> None:
    """
    Given a FileIO instance,
    when registered tool names are inspected,
    then 'create_file' is present.
    """
    assert "create_file" in _tool_names(FileIO())


def test_fileio_registers_edit_file_tool() -> None:
    assert "edit_file" in _tool_names(FileIO())


def test_fileio_registers_delete_file_tool() -> None:
    assert "delete_file" in _tool_names(FileIO())


def test_fileio_registers_copy_file_tool() -> None:
    assert "copy_file" in _tool_names(FileIO())


def test_fileio_registers_move_file_tool() -> None:
    assert "move_file" in _tool_names(FileIO())


def test_fileio_registers_exactly_five_tools() -> None:
    """
    Given a FileIO instance,
    when tools are counted,
    then exactly five tools are registered.
    """
    assert len(_tool_names(FileIO())) == 5


# ---------------------------------------------------------------------------
# File operation behaviors (via tool bound functions)
# ---------------------------------------------------------------------------


def test_create_file_creates_file_on_disk(tmp_path: Path) -> None:
    """
    Given a path inside the sandbox,
    when the create_file tool function is invoked,
    then the file appears on disk with the given content.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "create_file")
    target = str(tmp_path / "hello.txt")
    fn(path=target, content="hello world")
    assert Path(target).read_text(encoding="utf-8") == "hello world"


def test_create_file_raises_when_file_already_exists(tmp_path: Path) -> None:
    """
    Given an existing file,
    when create_file is invoked for the same path,
    then FileExistsError is raised.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "create_file")
    target = str(tmp_path / "exists.txt")
    Path(target).write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        fn(path=target, content="new")


def test_create_file_creates_parent_directories(tmp_path: Path) -> None:
    """
    Given a path inside the sandbox whose parent directory does not exist,
    when create_file is invoked,
    then the parent directory is created and the file is written.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "create_file")
    target = str(tmp_path / "sub" / "dir" / "file.txt")
    fn(path=target, content="nested")
    assert Path(target).read_text(encoding="utf-8") == "nested"


def test_create_file_blocked_outside_base_dir(tmp_path: Path) -> None:
    """
    Given a FileIO with a restricted sandbox,
    when create_file is called with a path outside the sandbox,
    then PermissionError is raised.
    """
    inner = tmp_path / "inner"
    inner.mkdir()
    fio = FileIO(base_dir=inner)
    fn = _tool_fn(fio, "create_file")
    outside = str(tmp_path / "escape.txt")
    with pytest.raises(PermissionError):
        fn(path=outside, content="x")


def test_edit_file_replaces_content(tmp_path: Path) -> None:
    """
    Given an existing file,
    when edit_file is invoked with new content,
    then the file content is replaced.
    """
    fio = FileIO(base_dir=tmp_path)
    target = tmp_path / "edit.txt"
    target.write_text("old", encoding="utf-8")
    fn = _tool_fn(fio, "edit_file")
    fn(path=str(target), content="new content")
    assert target.read_text(encoding="utf-8") == "new content"


def test_edit_file_raises_when_file_missing(tmp_path: Path) -> None:
    """
    Given no file at the path,
    when edit_file is invoked,
    then FileNotFoundError is raised.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "edit_file")
    with pytest.raises(FileNotFoundError):
        fn(path=str(tmp_path / "ghost.txt"), content="x")


def test_delete_file_removes_file(tmp_path: Path) -> None:
    """
    Given an existing file,
    when delete_file is invoked,
    then the file no longer exists.
    """
    fio = FileIO(base_dir=tmp_path)
    target = tmp_path / "delete_me.txt"
    target.write_text("bye", encoding="utf-8")
    fn = _tool_fn(fio, "delete_file")
    fn(path=str(target))
    assert not target.exists()


def test_delete_file_raises_when_file_missing(tmp_path: Path) -> None:
    """
    Given no file at the path,
    when delete_file is invoked,
    then FileNotFoundError is raised.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "delete_file")
    with pytest.raises(FileNotFoundError):
        fn(path=str(tmp_path / "ghost.txt"))


def test_copy_file_creates_destination_with_same_content(tmp_path: Path) -> None:
    """
    Given a source file and a new destination path,
    when copy_file is invoked,
    then the destination has the same content as the source.
    """
    fio = FileIO(base_dir=tmp_path)
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("copy me", encoding="utf-8")
    fn = _tool_fn(fio, "copy_file")
    fn(source=str(src), destination=str(dst))
    assert dst.read_text(encoding="utf-8") == "copy me"


def test_copy_file_source_remains_after_copy(tmp_path: Path) -> None:
    """
    Given a source file,
    when copy_file is invoked,
    then the source file still exists.
    """
    fio = FileIO(base_dir=tmp_path)
    src = tmp_path / "source.txt"
    dst = tmp_path / "dest.txt"
    src.write_text("data", encoding="utf-8")
    fn = _tool_fn(fio, "copy_file")
    fn(source=str(src), destination=str(dst))
    assert src.exists()


def test_copy_file_raises_when_source_missing(tmp_path: Path) -> None:
    """
    Given no source file,
    when copy_file is invoked,
    then FileNotFoundError is raised.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "copy_file")
    with pytest.raises(FileNotFoundError):
        fn(source=str(tmp_path / "ghost.txt"), destination=str(tmp_path / "dst.txt"))


def test_move_file_renames_file(tmp_path: Path) -> None:
    """
    Given a source file,
    when move_file is invoked to a new path,
    then the source is gone and the destination has the original content.
    """
    fio = FileIO(base_dir=tmp_path)
    src = tmp_path / "old.txt"
    dst = tmp_path / "new.txt"
    src.write_text("move me", encoding="utf-8")
    fn = _tool_fn(fio, "move_file")
    fn(source=str(src), destination=str(dst))
    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "move me"


def test_move_file_raises_when_source_missing(tmp_path: Path) -> None:
    """
    Given no source file,
    when move_file is invoked,
    then FileNotFoundError is raised.
    """
    fio = FileIO(base_dir=tmp_path)
    fn = _tool_fn(fio, "move_file")
    with pytest.raises(FileNotFoundError):
        fn(source=str(tmp_path / "ghost.txt"), destination=str(tmp_path / "dst.txt"))
