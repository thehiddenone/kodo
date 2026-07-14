"""``find_text_in_files`` tool — wrapper around the bundled ripgrep (``rg``).

Resolves the agent-supplied ``root`` through the active path resolver (so the
search is confined to the agent's allowed roots), then runs ``rg --json`` under
that root and returns one entry per matching line, with paths relative to the
root. Searches one root; a multi-project workspace is covered by one call per
``get_root_paths`` entry.

``temporary: true`` resolves ``root`` under the session's private scratch
directory instead (see :meth:`~kodo.tools.Tool.resolve_path`).
"""

from __future__ import annotations

import json
import logging

from ._search import UtilTimeout, run_util
from ._tool import Tool

__all__ = ["FindTextInFilesTool"]

_log = logging.getLogger(__name__)

_DEFAULT_MAX = 1000


class FindTextInFilesTool(Tool):
    """Search file contents under one resolved root using ripgrep."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        query = tool_input.get("query")
        root_raw = tool_input.get("root")
        if not query:
            return json.dumps({"error": "find_text_in_files requires a 'query'."})
        if not root_raw:
            return json.dumps({"error": "find_text_in_files requires a 'root'."})

        rg = ctx.util_paths.get("ripgrep")
        if rg is None:
            return json.dumps({"error": "The 'ripgrep' search util is not available."})

        try:
            root = self.resolve_path(
                str(root_raw), temporary=bool(tool_input.get("temporary", False))
            )
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})
        if not root.is_dir():
            return json.dumps({"error": f"Root {str(root)!r} is not a directory."})

        max_results = self.__resolve_max(tool_input.get("max_results"))
        args = self.__build_args(tool_input, str(query))

        _log.info("find_text_in_files from %s under %s: %s", ctx.agent_name, root, args)
        try:
            returncode, stdout, stderr = await run_util(str(rg), args, str(root))
        except UtilTimeout as exc:
            return json.dumps({"error": str(exc)})
        # rg exits 0 (matches), 1 (no matches — normal), 2 (error).
        if returncode not in (0, 1):
            msg = stderr.decode("utf-8", errors="replace").strip() or "ripgrep failed"
            return json.dumps({"error": msg})

        matches, truncated = self.__parse(stdout, max_results)
        return json.dumps(
            {
                "root": str(root),
                "matches": matches,
                "count": len(matches),
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

    @staticmethod
    def __build_args(tool_input: dict[str, object], query: str) -> list[str]:
        args = ["--json"]
        if tool_input.get("fixed_strings"):
            args.append("--fixed-strings")
        if tool_input.get("case_insensitive"):
            args.append("--ignore-case")
        else:
            args.append("--smart-case")
        if tool_input.get("hidden"):
            args.append("--hidden")
        if tool_input.get("no_ignore"):
            args.append("--no-ignore")
        glob = tool_input.get("glob")
        if isinstance(glob, str) and glob:
            args += ["--glob", glob]
        # `-e` guards against a query that begins with a dash; `.` searches the
        # (cwd=)root, yielding paths relative to it.
        args += ["-e", query, "."]
        return args

    @staticmethod
    def __parse(stdout: bytes, max_results: int) -> tuple[list[dict[str, object]], bool]:
        """Parse ``rg --json`` output into capped ``{path, line, text}`` entries."""
        matches: list[dict[str, object]] = []
        truncated = False
        for raw in stdout.decode("utf-8", errors="replace").splitlines():
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") != "match":
                continue
            if len(matches) >= max_results:
                truncated = True
                break
            data = event.get("data", {})
            path = data.get("path", {}).get("text", "")
            if path.startswith("./"):
                path = path[2:]
            text = data.get("lines", {}).get("text", "")
            matches.append(
                {
                    "path": path,
                    "line": data.get("line_number", 0),
                    "text": text.rstrip("\n"),
                }
            )
        return matches, truncated
