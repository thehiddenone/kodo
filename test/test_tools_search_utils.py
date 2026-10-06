"""Behavior tests for the util-backed tools: ``find_files``, ``find_text_in_files``,
``read_file``'s pattern mode, and the ``run_command`` shell tool.

The bundled ``fd`` / ``rg`` binaries are never required: each test injects a
tiny POSIX shell stub in their place (via ``util_paths``) that records the
argument vector it was given and/or prints a canned ``rg --json`` stream, so the
tools' input translation, output parsing, error mapping, and timeout handling
are all exercised deterministically and offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Awaitable
from pathlib import Path

import pytest

from kodo.runtime import SessionState
from kodo.tools import DISPATCHABLE_TOOLS_BY_NAME, RootPath, ToolDispatcher
from kodo.toolspecs import requires_intent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the util stand-ins are POSIX shell scripts"
)

_INTENT = "exercise this tool in a behavior test"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RootResolver:
    """Resolves paths under one root and refuses anything that escapes it."""

    __root: Path

    def __init__(self, root: Path) -> None:
        """Bind the resolver to *root*.

        Args:
            root (Path): The single allowed root.
        """
        self.__root = root.resolve()

    def resolve(self, path: str) -> Path:
        """Resolve *path* under the root, raising on an escape.

        Args:
            path (str): Absolute or root-relative path.

        Returns:
            Path: The resolved absolute path.
        """
        candidate = Path(path)
        resolved = (
            candidate.resolve() if candidate.is_absolute() else (self.__root / path).resolve()
        )
        if resolved != self.__root and self.__root not in resolved.parents:
            raise PermissionError(f"Path {path!r} is outside the allowed roots.")
        return resolved

    @property
    def default_cwd(self) -> Path:
        return self.__root


class _Gate:
    """Structural ``GateLike``; none of the tools under test prompt the user."""

    async def fire_questions(
        self, questions: list[dict[str, object]], tool_call_id: str = ""
    ) -> list[dict[str, object]]:
        """Answer every question with an empty selection.

        Args:
            questions (list[dict[str, object]]): The question batch.
            tool_call_id (str): The calling tool-use id.

        Returns:
            list[dict[str, object]]: One empty answer per question.
        """
        return [{"selected": [], "free_text": None} for _ in questions]


class _Services:
    """Structural ``EngineServices`` with one bound project root."""

    __root: Path

    def __init__(self, root: Path) -> None:
        """Bind the services to the project *root*.

        Args:
            root (Path): The bound project root.
        """
        self.__root = root

    def has_workspace(self) -> bool:
        """Report a bound workspace.

        Returns:
            bool: Always ``True``.
        """
        return True

    def root_paths(self) -> tuple[RootPath, ...]:
        """Return the one bound root.

        Returns:
            tuple[RootPath, ...]: The bound root.
        """
        return (RootPath(name="proj", path=str(self.__root)),)

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        """Accept the in-progress notification.

        Args:
            tool_call_id (str): The calling tool-use id.
        """
        return None


def _make_dispatcher(root: Path, util_paths: dict[str, Path] | None = None) -> ToolDispatcher:
    return ToolDispatcher(
        resolver=_RootResolver(root),
        gate=_Gate(),
        session=SessionState(),
        services=_Services(root),  # type: ignore[arg-type]
        agent_name="test_agent",
        session_id="sess-test",
        util_paths=util_paths,
    )


async def _call(d: ToolDispatcher, name: str, payload: dict[str, object]) -> dict[str, object]:
    if requires_intent(DISPATCHABLE_TOOLS_BY_NAME[name]):
        payload = {"intent": _INTENT, **payload}
    result = json.loads(await d.dispatch(name, payload))
    assert isinstance(result, dict)
    return result


def _stub(
    bin_dir: Path,
    *,
    canned: str = "",
    stderr: str = "",
    exit_code: int = 0,
    sleep: float = 0.0,
) -> tuple[Path, Path]:
    """Write a util stand-in; return ``(script, argv_record_file)``.

    The script records its argv (one per line), prints *canned* to stdout and
    *stderr* to stderr, optionally sleeps, and exits with *exit_code*.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    canned_file = bin_dir / "canned.txt"
    canned_file.write_text(canned, encoding="utf-8")
    argv_file = bin_dir / "argv.txt"
    lines = [
        "#!/bin/sh",
        f"printf '%s\\n' \"$@\" > '{argv_file}'",
        f"cat '{canned_file}'",
    ]
    if stderr:
        lines.append(f"printf '%s' '{stderr}' >&2")
    if sleep:
        lines.append(f"exec sleep {sleep}")
    lines.append(f"exit {exit_code}")
    script = bin_dir / "util"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script, argv_file


def _argv(argv_file: Path) -> list[str]:
    return argv_file.read_text(encoding="utf-8").splitlines()


def _echo_args_stub(bin_dir: Path) -> Path:
    """A stand-in whose stdout *is* its argv, one argument per line."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "echo_args"
    script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def short_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap every ``asyncio.wait_for`` at 50 ms so timeout paths run instantly."""
    real_wait_for = asyncio.wait_for

    async def _capped(aw: Awaitable[object], timeout: float | None = None) -> object:
        return await real_wait_for(aw, timeout=0.05 if timeout is None else min(timeout, 0.05))

    monkeypatch.setattr(asyncio, "wait_for", _capped)


def _rg_event(kind: str, path: str = "./a.py", line: int = 1, text: str = "") -> str:
    return json.dumps(
        {
            "type": kind,
            "data": {
                "path": {"text": path},
                "line_number": line,
                "lines": {"text": text},
            },
        }
    )


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------


async def test_find_files_without_fd_reports_unavailable(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "find_files", {"root": str(tmp_path)})
    assert "not available" in str(result["error"])


async def test_find_files_rejects_root_outside_allowed_roots(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    d = _make_dispatcher(proj, {"fd": _echo_args_stub(tmp_path / "bin")})
    result = await _call(d, "find_files", {"root": "../elsewhere"})
    assert "outside the allowed roots" in str(result["error"])


async def test_find_files_rejects_a_root_that_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    d = _make_dispatcher(tmp_path, {"fd": _echo_args_stub(tmp_path / "bin")})
    result = await _call(d, "find_files", {"root": "file.txt"})
    assert "is not a directory" in str(result["error"])


async def test_find_files_translates_every_filter_into_fd_flags(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, {"fd": _echo_args_stub(tmp_path / "bin")})
    result = await _call(
        d,
        "find_files",
        {
            "root": str(tmp_path),
            "glob": True,
            "type": "file",
            "extension": ".py",
            "hidden": True,
            "no_ignore": True,
            "pattern": "-starts-with-dash",
        },
    )
    files = result["files"]
    assert isinstance(files, list)
    assert files[-2:] == ["--", "-starts-with-dash"]
    for flag in ("--glob", "--hidden", "--no-ignore"):
        assert flag in files
    assert files[files.index("--type") + 1] == "f"
    assert files[files.index("--extension") + 1] == "py"
    assert result["truncated"] is False


async def test_find_files_directory_type_maps_to_fd_directory_flag(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, {"fd": _echo_args_stub(tmp_path / "bin")})
    result = await _call(d, "find_files", {"root": str(tmp_path), "type": "directory"})
    files = result["files"]
    assert isinstance(files, list)
    assert files[files.index("--type") + 1] == "d"


@pytest.mark.parametrize("raw_max", ["not-a-number", 0, -5, True, [3]])
async def test_find_files_invalid_max_results_falls_back_to_default(
    tmp_path: Path, raw_max: object
) -> None:
    d = _make_dispatcher(tmp_path, {"fd": _echo_args_stub(tmp_path / "bin")})
    result = await _call(d, "find_files", {"root": str(tmp_path), "max_results": raw_max})
    files = result["files"]
    assert isinstance(files, list)
    # The default cap (1000) is requested from fd as 1001 to detect truncation.
    assert files[files.index("--max-results") + 1] == "1001"
    assert result["truncated"] is False


async def test_find_files_caps_results_and_flags_truncation(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path, {"fd": _echo_args_stub(tmp_path / "bin")})
    result = await _call(d, "find_files", {"root": str(tmp_path), "max_results": "2"})
    assert result["count"] == 2
    assert result["truncated"] is True


async def test_find_files_surfaces_fd_stderr_on_failure(tmp_path: Path) -> None:
    script, _ = _stub(tmp_path / "bin", stderr="fd: bad regex", exit_code=2)
    d = _make_dispatcher(tmp_path, {"fd": script})
    result = await _call(d, "find_files", {"root": str(tmp_path)})
    assert result == {"error": "fd: bad regex"}


async def test_find_files_failure_without_stderr_has_generic_message(tmp_path: Path) -> None:
    script, _ = _stub(tmp_path / "bin", exit_code=3)
    d = _make_dispatcher(tmp_path, {"fd": script})
    result = await _call(d, "find_files", {"root": str(tmp_path)})
    assert result == {"error": "fd failed"}


@pytest.mark.usefixtures("short_waits")
async def test_find_files_reports_a_timed_out_search(tmp_path: Path) -> None:
    script, _ = _stub(tmp_path / "bin", sleep=5)
    d = _make_dispatcher(tmp_path, {"fd": script})
    result = await _call(d, "find_files", {"root": str(tmp_path)})
    assert "timed out" in str(result["error"])


@pytest.mark.usefixtures("short_waits")
async def test_search_timeout_tolerates_an_already_gone_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_killpg = os.killpg

    def _kill_then_report_gone(pgid: int, sig: int) -> None:
        real_killpg(pgid, signal.SIGKILL)
        raise ProcessLookupError(pgid)

    monkeypatch.setattr(os, "killpg", _kill_then_report_gone)
    script, _ = _stub(tmp_path / "bin", sleep=5)
    d = _make_dispatcher(tmp_path, {"fd": script})
    result = await _call(d, "find_files", {"root": str(tmp_path)})
    assert "timed out" in str(result["error"])


# ---------------------------------------------------------------------------
# find_text_in_files
# ---------------------------------------------------------------------------


async def test_find_text_requires_root(tmp_path: Path) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "find_text_in_files", {"query": "x"})
    assert "requires a 'root'" in str(result["error"])


async def test_find_text_rejects_root_outside_allowed_roots(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    script, _ = _stub(tmp_path / "bin")
    d = _make_dispatcher(proj, {"ripgrep": script})
    result = await _call(d, "find_text_in_files", {"query": "x", "root": "../elsewhere"})
    assert "outside the allowed roots" in str(result["error"])


async def test_find_text_rejects_a_root_that_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    script, _ = _stub(tmp_path / "bin")
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "find_text_in_files", {"query": "x", "root": "file.txt"})
    assert "is not a directory" in str(result["error"])


async def test_find_text_parses_rg_json_and_skips_noise(tmp_path: Path) -> None:
    canned = "\n".join(
        [
            "",
            "this is not json",
            json.dumps({"type": "begin", "data": {}}),
            _rg_event("match", "./a.py", 3, "needle one\r\n"),
            _rg_event("match", "sub\\b.py", 7, "needle two\n"),
        ]
    )
    script, _ = _stub(tmp_path / "bin", canned=canned)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "find_text_in_files", {"query": "needle", "root": str(tmp_path)})
    assert result["matches"] == [
        {"path": "a.py", "line": 3, "text": "needle one"},
        {"path": "sub/b.py", "line": 7, "text": "needle two"},
    ]
    assert result["truncated"] is False


async def test_find_text_caps_matches_and_flags_truncation(tmp_path: Path) -> None:
    canned = "\n".join(_rg_event("match", "./a.py", n, f"hit {n}") for n in range(1, 4))
    script, _ = _stub(tmp_path / "bin", canned=canned)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(
        d, "find_text_in_files", {"query": "hit", "root": str(tmp_path), "max_results": 2}
    )
    assert result["count"] == 2
    assert result["truncated"] is True


@pytest.mark.parametrize("raw_max", ["many", 0, False, {"n": 1}])
async def test_find_text_invalid_max_results_falls_back_to_default(
    tmp_path: Path, raw_max: object
) -> None:
    canned = "\n".join(_rg_event("match", "./a.py", n, f"hit {n}") for n in range(1, 4))
    script, _ = _stub(tmp_path / "bin", canned=canned)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(
        d, "find_text_in_files", {"query": "hit", "root": str(tmp_path), "max_results": raw_max}
    )
    assert result["count"] == 3
    assert result["truncated"] is False


async def test_find_text_translates_options_into_rg_flags(tmp_path: Path) -> None:
    script, argv_file = _stub(tmp_path / "bin", exit_code=1)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(
        d,
        "find_text_in_files",
        {
            "query": "-dash",
            "root": str(tmp_path),
            "fixed_strings": True,
            "case_insensitive": True,
            "hidden": True,
            "no_ignore": True,
            "glob": "*.py",
        },
    )
    assert result["matches"] == [] and result["count"] == 0
    argv = _argv(argv_file)
    for flag in ("--json", "--fixed-strings", "--ignore-case", "--hidden", "--no-ignore"):
        assert flag in argv
    assert "--smart-case" not in argv
    assert argv[argv.index("--glob") + 1] == "*.py"
    assert argv[-3:] == ["-e", "-dash", "."]


async def test_find_text_defaults_to_smart_case(tmp_path: Path) -> None:
    script, argv_file = _stub(tmp_path / "bin", exit_code=1)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    await _call(d, "find_text_in_files", {"query": "x", "root": str(tmp_path)})
    argv = _argv(argv_file)
    assert "--smart-case" in argv
    assert "--ignore-case" not in argv


async def test_find_text_surfaces_rg_errors(tmp_path: Path) -> None:
    script, _ = _stub(tmp_path / "bin", stderr="regex parse error", exit_code=2)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "find_text_in_files", {"query": "(", "root": str(tmp_path)})
    assert result == {"error": "regex parse error"}

    script, _ = _stub(tmp_path / "bin2", exit_code=2)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "find_text_in_files", {"query": "(", "root": str(tmp_path)})
    assert result == {"error": "ripgrep failed"}


@pytest.mark.usefixtures("short_waits")
async def test_find_text_reports_a_timed_out_search(tmp_path: Path) -> None:
    script, _ = _stub(tmp_path / "bin", sleep=5)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "find_text_in_files", {"query": "x", "root": str(tmp_path)})
    assert "timed out" in str(result["error"])


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


async def test_read_file_rejects_path_outside_allowed_roots(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    d = _make_dispatcher(proj)
    result = await _call(d, "read_file", {"path": "../secret.txt"})
    assert "outside the allowed roots" in str(result["error"])


async def test_read_file_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_file", {"path": "pkg"})
    assert result == {"error": "Not a file: 'pkg'"}


async def test_read_file_reports_undecodable_content(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_file", {"path": "blob.bin"})
    assert "error" in result


async def test_read_file_skips_malformed_ranges_and_clamps_bounds(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    d = _make_dispatcher(tmp_path)
    result = await _call(
        d,
        "read_file",
        {"path": "f.txt", "ranges": ["bogus", {"start_line": 0, "end_line": 99}]},
    )
    assert result["sections"] == [{"start_line": 1, "end_line": 3, "content": "one\ntwo\nthree"}]


async def test_read_file_pattern_without_ripgrep_reports_unavailable(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "read_file", {"path": "f.txt", "pattern": "x"})
    assert "not available" in str(result["error"])


async def test_read_file_pattern_groups_context_around_each_match(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("\n".join(f"line {n}" for n in range(1, 13)), encoding="utf-8")
    canned = "\n".join(
        [
            "",
            "garbage",
            json.dumps({"type": "begin", "data": {}}),
            _rg_event("context", line=1, text="line 1\n"),
            _rg_event("match", line=2, text="line 2\n"),
            _rg_event("context", line=3, text="line 3\n"),
            _rg_event("context", line=4, text="line 4\n"),
            json.dumps({"type": "end", "data": {}}),
            _rg_event("context", line=8, text="line 8\n"),
            _rg_event("context", line=9, text="line 9\n"),
            _rg_event("match", line=10, text="line 10\n"),
            _rg_event("match", line=12, text="line 12\n"),
        ]
    )
    script, argv_file = _stub(tmp_path / "bin", canned=canned)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(
        d,
        "read_file",
        {
            "path": "f.txt",
            "pattern": "line",
            "context_before": 1,
            "context_after": "1",
            "max_matches": 2,
            "ignore_case": True,
        },
    )
    assert result["total_lines"] == 12
    assert result["truncated"] is True
    assert result["matches"] == [
        {
            "line_number": 2,
            "line": "line 2",
            "context_before": ["line 1"],
            "context_after": ["line 3"],
        },
        {"line_number": 10, "line": "line 10", "context_before": ["line 9"], "context_after": []},
    ]
    assert "-i" in _argv(argv_file)


@pytest.mark.parametrize("bad", ["lots", True, None, 0])
async def test_read_file_pattern_invalid_numbers_fall_back_to_defaults(
    tmp_path: Path, bad: object
) -> None:
    (tmp_path / "f.txt").write_text("a\nb\n", encoding="utf-8")
    canned = "\n".join(
        [
            _rg_event("context", line=1, text="a\n"),
            _rg_event("match", line=2, text="b\n"),
        ]
    )
    script, argv_file = _stub(tmp_path / "bin", canned=canned)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(
        d,
        "read_file",
        {
            "path": "f.txt",
            "pattern": "b",
            "context_before": bad,
            "context_after": bad,
            "max_matches": bad,
        },
    )
    # No context requested -> none returned; the default max (200) is not hit.
    assert result["matches"] == [
        {"line_number": 2, "line": "b", "context_before": [], "context_after": []}
    ]
    assert result["truncated"] is False
    argv = _argv(argv_file)
    assert "-B0" in argv and "-A0" in argv
    assert "-i" not in argv


async def test_read_file_pattern_surfaces_rg_errors(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    script, _ = _stub(tmp_path / "bin", stderr="bad pattern", exit_code=2)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "read_file", {"path": "f.txt", "pattern": "("})
    assert result == {"error": "bad pattern"}

    script, _ = _stub(tmp_path / "bin2", exit_code=2)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "read_file", {"path": "f.txt", "pattern": "("})
    assert result == {"error": "ripgrep failed"}


@pytest.mark.usefixtures("short_waits")
async def test_read_file_pattern_reports_a_timed_out_search(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    script, _ = _stub(tmp_path / "bin", sleep=5)
    d = _make_dispatcher(tmp_path, {"ripgrep": script})
    result = await _call(d, "read_file", {"path": "f.txt", "pattern": "x"})
    assert "timed out" in str(result["error"])


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_timeout", ["soon", [5]])
async def test_run_command_rejects_non_numeric_timeout(tmp_path: Path, bad_timeout: object) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "run_command", {"command": "true", "timeout": bad_timeout})
    assert "must be a number of seconds" in str(result["error"])


@pytest.mark.parametrize("bad_timeout", [0, -1.5])
async def test_run_command_rejects_non_positive_timeout(tmp_path: Path, bad_timeout: float) -> None:
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "run_command", {"command": "true", "timeout": bad_timeout})
    assert "greater than 0" in str(result["error"])


@pytest.mark.usefixtures("short_waits")
async def test_run_command_timeout_survives_unkillable_group_and_stuck_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The process group has (apparently) vanished and the post-kill drain
    # never completes: the tool must still return promptly with the timeout
    # note rather than wedge the worker.
    def _gone(pgid: int, sig: int) -> None:
        raise ProcessLookupError(pgid)

    monkeypatch.setattr(os, "killpg", _gone)
    d = _make_dispatcher(tmp_path)
    result = await _call(d, "run_command", {"command": "sleep 0.3", "timeout": 0.01})
    assert result["exit_code"] is None
    assert result["stdout"] == ""
    assert str(result["stderr"]).startswith("Command timed out after 0.01s")
