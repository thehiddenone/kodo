"""``find_files`` tool — wrapper around the bundled ``fd`` util.

Resolves the agent-supplied ``root`` through the active path resolver (so the
search is confined to the agent's allowed roots), then runs ``fd`` under that
root and returns matching paths relative to it. Searches one root; a
multi-project workspace is covered by one call per ``get_root_paths`` entry.

``temporary: true`` resolves ``root`` under the session's private scratch
directory instead (see :meth:`~kodo.tools.Tool.resolve_path`).
"""

from __future__ import annotations

import json
import logging

from ._search import UtilTimeout, run_util
from ._tool import Tool

__all__ = ["FindFilesTool"]

_log = logging.getLogger(__name__)

_DEFAULT_MAX = 1000


class FindFilesTool(Tool):
    """Find files/directories by name under one resolved root using ``fd``."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        root_raw = tool_input.get("root")
        if not root_raw:
            return json.dumps({"error": "find_files requires a 'root'."})

        fd = ctx.util_paths.get("fd")
        if fd is None:
            return json.dumps({"error": "The 'fd' search util is not available."})

        try:
            root = self.resolve_path(
                str(root_raw), temporary=bool(tool_input.get("temporary", False))
            )
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})
        if not root.is_dir():
            return json.dumps({"error": f"Root {str(root)!r} is not a directory."})

        max_results = self.__resolve_max(tool_input.get("max_results"))
        args = self.__build_args(tool_input, max_results)

        _log.info("find_files from %s under %s: %s", ctx.agent_name, root, args)
        try:
            returncode, stdout, stderr = await run_util(str(fd), args, str(root))
        except UtilTimeout as exc:
            return json.dumps({"error": str(exc)})
        # fd exits 0 normally; 1 means no matches on some builds, >1 is an error.
        if returncode not in (0, 1):
            msg = stderr.decode("utf-8", errors="replace").strip() or "fd failed"
            return json.dumps({"error": msg})

        lines = [ln for ln in stdout.decode("utf-8", errors="replace").splitlines() if ln]
        truncated = len(lines) > max_results
        files = lines[:max_results]
        return json.dumps(
            {
                "root": str(root),
                "files": files,
                "count": len(files),
                "truncated": truncated,
            }
        )

    @staticmethod
    def __resolve_max(raw: object) -> int:
        if not isinstance(raw, (int, float, str)) or isinstance(raw, bool):
            return _DEFAULT_MAX
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_MAX
        return value if value > 0 else _DEFAULT_MAX

    @classmethod
    def __build_args(cls, tool_input: dict[str, object], max_results: int) -> list[str]:
        # --strip-cwd-prefix makes paths relative to the (cwd=)root with no ./
        # prefix; request one extra result so we can detect truncation.
        args = ["--color", "never", "--strip-cwd-prefix", "--max-results", str(max_results + 1)]
        if tool_input.get("glob"):
            args.append("--glob")
        type_ = tool_input.get("type")
        if type_ == "file":
            args += ["--type", "f"]
        elif type_ == "directory":
            args += ["--type", "d"]
        extension = tool_input.get("extension")
        if isinstance(extension, str) and extension:
            args += ["--extension", extension.lstrip(".")]
        if tool_input.get("hidden"):
            args.append("--hidden")
        if tool_input.get("no_ignore"):
            args.append("--no-ignore")
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern:
            # `--` guards against a pattern that begins with a dash.
            args += ["--", pattern]
        return args
