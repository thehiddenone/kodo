"""``read_file`` tool — read a file whole, by line ranges, or by regex pattern."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ._search import UtilTimeout, run_util
from ._tool import Tool

__all__ = ["ReadFileTool"]

_log = logging.getLogger(__name__)
_DEFAULT_MAX_MATCHES = 200


class ReadFileTool(Tool):
    """Read a file whole, by line ranges, or by regex pattern with context."""

    async def handle(self, tool_input: dict[str, object]) -> str:
        ctx = self.context
        path = str(tool_input.get("path", ""))
        ranges = tool_input.get("ranges")
        pattern = tool_input.get("pattern")

        if ranges and pattern:
            return json.dumps({"error": "`ranges` and `pattern` are mutually exclusive."})

        try:
            target = ctx.resolver.resolve(path)
        except PermissionError as exc:
            return json.dumps({"error": str(exc)})
        if not target.exists():
            return json.dumps({"error": f"File not found: {path!r}"})
        if target.is_dir():
            return json.dumps({"error": f"Not a file: {path!r}"})

        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.info("read_file from %s failed: %s", ctx.agent_name, exc)
            return json.dumps({"error": str(exc)})

        lines = text.splitlines()
        total_lines = len(lines)

        if isinstance(pattern, str) and pattern:
            return await self.__search(tool_input, pattern, target, path, total_lines)

        return json.dumps(self.__read_sections(tool_input, lines, path, total_lines))

    @staticmethod
    def __read_sections(
        tool_input: dict[str, object], lines: list[str], path: str, total_lines: int
    ) -> dict[str, object]:
        raw_ranges = tool_input.get("ranges")
        sections: list[dict[str, object]]
        if isinstance(raw_ranges, list) and raw_ranges:
            sections = []
            for r in raw_ranges:
                if not isinstance(r, dict):
                    continue
                start = max(1, int(r.get("start_line", 1)))
                end = min(total_lines, int(r.get("end_line", total_lines)))
                content = "\n".join(lines[start - 1 : end])
                sections.append({"start_line": start, "end_line": end, "content": content})
        else:
            sections = [{"start_line": 1, "end_line": total_lines, "content": "\n".join(lines)}]
        return {"path": path, "total_lines": total_lines, "sections": sections}

    async def __search(
        self,
        tool_input: dict[str, object],
        pattern: str,
        target: Path,
        path: str,
        total_lines: int,
    ) -> str:
        ctx = self.context
        rg = ctx.util_paths.get("ripgrep")
        if rg is None:
            return json.dumps({"error": "The 'ripgrep' search util is not available."})

        context_before = max(0, self.__as_int(tool_input.get("context_before"), 0))
        context_after = max(0, self.__as_int(tool_input.get("context_after"), 0))
        max_matches = self.__as_int(tool_input.get("max_matches"), _DEFAULT_MAX_MATCHES)
        if max_matches <= 0:
            max_matches = _DEFAULT_MAX_MATCHES

        args = ["--json", "--line-number", f"-B{context_before}", f"-A{context_after}"]
        if tool_input.get("ignore_case"):
            args.append("-i")
        args += ["-e", pattern, "--", target.name]

        try:
            returncode, stdout, stderr = await run_util(str(rg), args, str(target.parent))
        except UtilTimeout as exc:
            return json.dumps({"error": str(exc)})
        # rg exits 0 (matches), 1 (no matches — normal), 2 (error).
        if returncode not in (0, 1):
            msg = stderr.decode("utf-8", errors="replace").strip() or "ripgrep failed"
            return json.dumps({"error": msg})

        matches, truncated = self.__parse(stdout, max_matches, context_before, context_after)
        return json.dumps(
            {"path": path, "total_lines": total_lines, "matches": matches, "truncated": truncated}
        )

    @staticmethod
    def __as_int(raw: object, default: int) -> int:
        if not isinstance(raw, (int, float, str)) or isinstance(raw, bool):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __parse(
        stdout: bytes, max_matches: int, context_before: int, context_after: int
    ) -> tuple[list[dict[str, object]], bool]:
        """Parse ``rg --json`` output into matches with bounded context."""
        matches: list[dict[str, object]] = []
        truncated = False
        pending_before: list[str] = []
        current: dict[str, object] | None = None
        for raw in stdout.decode("utf-8", errors="replace").splitlines():
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            etype = event.get("type")
            if etype not in ("context", "match"):
                current = None
                continue
            data = event.get("data", {})
            text = data.get("lines", {}).get("text", "").rstrip("\r\n")
            if etype == "context":
                if current is not None:
                    after = current["context_after"]
                    assert isinstance(after, list)
                    after.append(text)
                else:
                    pending_before.append(text)
            else:  # match
                if len(matches) >= max_matches:
                    truncated = True
                    break
                current = {
                    "line_number": data.get("line_number", 0),
                    "line": text,
                    "context_before": pending_before[-context_before:] if context_before else [],
                    "context_after": [],
                }
                matches.append(current)
                pending_before = []
        for m in matches:
            after = m["context_after"]
            assert isinstance(after, list)
            m["context_after"] = after[:context_after]
        return matches, truncated
