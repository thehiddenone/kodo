"""``filesystem`` tool — one handler for every file/directory operation.

Dispatches on the ``operation`` field to delete, copy, or move a file or a
directory. Every path is resolved via ``LogicalPathResolver`` (a bound root's
name, or an absolute path). This handler replaces the former per-operation file tools
(``delete_file`` / ``copy_file`` / ``move_file``) and adds their directory
counterparts. Creating a brand-new file lives in the separate ``create_file``
tool (:class:`~kodo.tools._create_file.CreateFileTool`); creating a directory
lives in the separate ``create_directory`` tool
(:class:`~kodo.tools._create_directory.CreateDirectoryTool`).

``temporary: true`` resolves ``path``/``source``/``destination`` under the
session's private scratch directory instead (see
:meth:`~kodo.tools.Tool.resolve_path`).

**A bound root is not deletable through this tool.** ``delete_dir`` and
``move_dir`` refuse a target that *is*, or *contains*, one of the session's
bound project roots (:attr:`~kodo.tools.ToolContext.root_paths`) — see
:meth:`FilesystemTool._assert_not_bound_root`. This is a hard refusal in the
handler, not a security-layer verdict, precisely so it holds under every
Command Control posture and in autonomous runs: a sub-agent once issued
``operation: "delete_dir"`` on its own freshly scaffolded project root while
its ``intent`` read "List the project root directory to see current state",
permissive posture allowed the High-impact call, and the session was left
bound to a directory that no longer existed (its checkpoint mirror included).
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
        target = self.resolve_path(path, temporary=bool(tool_input.get("temporary", False)))
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path!r}")
        if target.is_dir():
            raise IsADirectoryError(f"Not a file (use delete_dir): {path!r}")
        target.unlink()
        return {"status": "deleted", "operation": "delete_file", "path": path}

    def _delete_dir(self, tool_input: dict[str, object]) -> dict[str, object]:
        path = str(tool_input.get("path", ""))
        target = self.resolve_path(path, temporary=bool(tool_input.get("temporary", False)))
        if not target.exists():
            raise FileNotFoundError(f"Directory not found: {path!r}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory (use delete_file): {path!r}")
        self._assert_not_bound_root(target, path, "Deleting")
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
        self._assert_not_bound_root(src, source, "Moving")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dst)
        return self._pair_result("moved", "move_dir", source, destination)

    # -- helpers ---------------------------------------------------------------

    def _assert_not_bound_root(self, target: Path, spelled: str, verb: str) -> None:
        """Refuse *target* when it is, or encloses, one of the session's bound roots.

        The whole session is anchored on those directories: they are what
        ``get_root_paths`` reports, what every logical path resolves through,
        what the checkpoint mirrors track, and what
        ``TransientStore.lock_workspace_path`` has permanently written into the
        session's remembered workspace shape. Removing one from underneath all
        of that leaves nothing recoverable — so the refusal is unconditional
        and lands as an ordinary tool error the agent must deal with, exactly
        like a missing source file.

        *Encloses* matters as much as *is*: deleting the workspace home that a
        project root sits inside destroys the root just as thoroughly, and is
        a likelier slip (an agent aiming one directory too high).

        Args:
            target: The already-resolved directory the operation would destroy.
            spelled: The path exactly as the caller wrote it, for the message.
            verb: Gerund naming the operation ("Deleting" / "Moving").

        Raises:
            ValueError: *target* is or contains a bound root.
        """
        resolved = target.resolve()
        for root in self.context.root_paths:
            root_path = Path(root.path).resolve()
            if root_path == resolved:
                raise ValueError(
                    f"{verb} {spelled!r} is refused: it is the bound project root "
                    f"{root.name!r}. The session, its logical paths and its "
                    "checkpoint history are all anchored on this directory. Remove "
                    "the contents you actually meant to remove, or ask the user to "
                    "retire the project."
                )
            if resolved in root_path.parents:
                raise ValueError(
                    f"{verb} {spelled!r} is refused: it contains the bound project "
                    f"root {root.name!r} ({root_path}). Target the specific "
                    "directory you meant instead of an ancestor of a bound root."
                )

    def _resolve_pair(self, tool_input: dict[str, object]) -> tuple[Path, Path, str, str]:
        source = str(tool_input.get("source", ""))
        destination = str(tool_input.get("destination", ""))
        temporary = bool(tool_input.get("temporary", False))
        return (
            self.resolve_path(source, temporary=temporary),
            self.resolve_path(destination, temporary=temporary),
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
