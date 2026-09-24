"""The headless sandbox posture: allow or deny, never ask.

Used by a kodo server started with ``--headless-sandbox DIR`` (the
``kodo-headless`` runner, doc/HEADLESS.md). There is no user to answer a
permission prompt, so every verdict is final: ``allow`` or ``deny`` (the
dispatcher turns a ``deny`` into an error result without running the tool).
The session's Command Control posture and Autonomous mode are ignored — this
layer *is* the posture.

The policy, in one line: **mutation is confined to the sandbox root; reads
are not; git may only be read.**

- File tools may write only inside the root, and never inside a ``.git``
  directory. Paths are resolved (symlinks followed), so a link inside the
  root that points outside is caught. ``temporary: true`` calls write to the
  session's own scratch directory and are allowed.
- ``run_command`` is **allow unless evidence**: a command is denied only when
  static analysis finds a write target outside the root (a redirection, or the
  target of a known file-mutating program), a path into ``.git`` handed to a
  non-read-only program, a git invocation outside the read-only set, or an
  executable that is itself a substitution. Opaque programs (``pytest``,
  ``make``, ``pip install`` …) are allowed. This is best-effort by nature — a
  program can write anywhere it likes at run time — so the real jail is the
  container the run is in (doc/HEADLESS.md "Limits").
- Every tool is classified explicitly; a tool without a policy is denied.
"""

from __future__ import annotations

import logging
import os
import os.path
import re
from pathlib import Path

from kodo.common import system_temp_roots
from kodo.shellparser import is_fd_merge_target, redirection_writes_file

from ._analysis import (
    _READONLY_CMDLETS,
    _READONLY_EXECUTABLES,
    _track_cwd,
    mask_substitutions,
    parse_masked,
)
from ._classify import CD_EXECUTABLES, SUB_MARK, leaf_name, normalize_segments, peel_prefixes
from ._layer import _SUSPICIOUS_DEP_RE, SecurityDecision, SecurityLayer
from ._rules import _MAX_DEPTH, _strip_command_sub

__all__ = ["SandboxSecurityLayer"]

_log = logging.getLogger(__name__)

# Tools with no effect outside kodo's own session state: always allowed.
_ALLOWED_TOOLS = frozenset(
    {
        "ask_user",
        "finalize_project",
        "find_files",
        "find_text_in_files",
        "get_findings",
        "get_plan",
        "get_root_paths",
        "get_web_search_state",
        "guided_dev_status",
        "plan_step_forward",
        "query_search_engine",
        "read_attachment",
        "read_file",
        "read_webpage",
        "remaining_time",
        "return_result",
        "rollback",
        "run_subagent",
        "submit_evaluation",
        "update_web_search_state",
        "use_skill",
        "wait",
        "web_search",
    }
)

# Tools a sandboxed run must never use, with the reason the agent is told.
_DENIED_TOOLS: dict[str, str] = {
    "scaffold_new_project": (
        "The sandbox root is fixed for this run; work inside the existing workspace."
    ),
    "disable_autonomous_mode": (
        "No user is present in this run; continue autonomously and make the call yourself."
    ),
}

# Single-path writers: the `path` input must land inside the root.
_PATH_WRITERS = frozenset({"create_file", "create_directory", "edit_file"})

# Every policy-bearing tool name — what the registry-driven coverage test reads.
_SPECIAL_TOOLS = frozenset({"filesystem", "run_command", "toolchain_build", "toolchain_deps"})

# Wrappers that run a trailing command the transparent-prefix peel does not
# unwrap (`xargs git commit`, `find . -exec git add {} ;`, `sudo git push`).
_EXEC_WRAPPERS = frozenset(
    {
        "xargs",
        "find",
        "parallel",
        "watch",
        "flock",
        "setsid",
        "sudo",
        "doas",
        "strace",
        "ltrace",
        "ionice",
        "chronic",
        "command",
        "builtin",
        "unbuffer",
    }
)

# Programs whose positional arguments are write targets, and how to read them:
# "all" — every positional is mutated; "last" — only the final positional (the
# destination: sources are merely read); "flagged" — only when an in-place
# flag is present (`sed -i`).
_MUTATING_PROGRAMS: dict[str, str] = {
    "rm": "all",
    "rmdir": "all",
    "unlink": "all",
    "shred": "all",
    "touch": "all",
    "mkdir": "all",
    "chmod": "all",
    "chown": "all",
    "chgrp": "all",
    "truncate": "all",
    "tee": "all",
    "mv": "all",
    "cp": "last",
    "ln": "last",
    "install": "last",
    "rsync": "last",
    "sed": "flagged",
    "perl": "flagged",
}
_IN_PLACE_FLAGS = ("-i", "--in-place")

# Flags whose value is a write target (destination directory / output file).
_TARGET_FLAGS: dict[str, tuple[str, ...]] = {
    "cp": ("-t", "--target-directory"),
    "mv": ("-t", "--target-directory"),
    "ln": ("-t", "--target-directory"),
    "install": ("-t", "--target-directory"),
    "tar": ("-C", "--directory"),
    "unzip": ("-d",),
    "patch": ("-d", "--directory", "-o", "--output"),
    "curl": ("-o", "--output"),
    "wget": ("-O", "--output-document", "-P", "--directory-prefix"),
}

# "git" as a whole word inside inline code (`python -c "…git…"`).
_GIT_WORD_RE = re.compile(r"(?<![\w.-])git(?![\w.-])")

# ---- git -------------------------------------------------------------------

# Subcommands that only ever read.
_GIT_READ_ONLY = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "annotate",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "describe",
        "shortlog",
        "grep",
        "merge-base",
        "name-rev",
        "for-each-ref",
        "rev-list",
        "show-ref",
        "count-objects",
        "version",
        "help",
        "whatchanged",
        "diff-tree",
        "diff-files",
        "diff-index",
        "check-ignore",
        "check-attr",
        "cherry",
        "show-branch",
        "range-diff",
        "var",
    }
)
# Global options (before the subcommand) that take no value and change nothing.
_GIT_HARMLESS_GLOBALS = frozenset(
    {
        "--no-pager",
        "-P",
        "-p",
        "--paginate",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
        "--bare",
        "--version",
        "--help",
        "-h",
    }
)
# Options anywhere in a read-only command that still write or execute.
_GIT_WRITING_OPTIONS = ("--output", "--open-files-in-pager", "--ext-diff")

# Listing forms: flags that keep the subcommand read-only, flags whose next
# token is a value, and flags that make positional arguments mere patterns.
_GIT_BRANCH_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-l",
        "--list",
        "-v",
        "-vv",
        "--verbose",
        "--show-current",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--sort",
        "--format",
        "--column",
        "--no-column",
        "--color",
        "--no-color",
        "-i",
        "--ignore-case",
        "--omit-empty",
    }
)
_GIT_TAG_FLAGS = frozenset(
    {
        "-l",
        "--list",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--sort",
        "--format",
        "--column",
        "--no-column",
        "--color",
        "--no-color",
        "-i",
        "--ignore-case",
        "--omit-empty",
    }
)
_GIT_VALUE_FLAGS = frozenset(
    {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
)
_GIT_CONFIG_READS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--get-color",
        "--get-colorbool",
        "--list",
        "-l",
    }
)
_GIT_CONFIG_WRITES = frozenset(
    {
        "--add",
        "--unset",
        "--unset-all",
        "--replace-all",
        "--rename-section",
        "--remove-section",
        "-e",
        "--edit",
    }
)


def _deny(reason: str) -> SecurityDecision:
    return SecurityDecision(action="deny", reason=reason, source="sandbox")


def _allow(reason: str) -> SecurityDecision:
    return SecurityDecision(action="allow", reason=reason, source="sandbox")


def _flag_name(token: str) -> str:
    return token.split("=", 1)[0]


class SandboxSecurityLayer(SecurityLayer):
    """Allow/deny judgement confining a headless run's mutations to one root."""

    __root: Path
    __windows: bool

    def __init__(self, sandbox_root: Path, *, windows: bool | None = None) -> None:
        """Bind the sandbox root.

        Args:
            sandbox_root (Path): The directory the run may mutate. Resolved
                (symlinks followed) once, here.
            windows (bool | None): Parse commands as PowerShell/cmd (``True``)
                or POSIX (``False``); defaults to the current platform.
        """
        self.__root = Path(os.path.realpath(sandbox_root))
        self.__windows = os.name == "nt" if windows is None else windows

    @property
    def sandbox_root(self) -> Path:
        """The resolved directory this layer confines mutation to."""
        return self.__root

    @staticmethod
    def classified_tools() -> frozenset[str]:
        """Every tool name this layer has an explicit policy for.

        Returns:
            frozenset[str]: Tool names; any other tool is denied.
        """
        return _ALLOWED_TOOLS | frozenset(_DENIED_TOOLS) | _PATH_WRITERS | _SPECIAL_TOOLS

    async def evaluate(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, object],
        command_control: str,
        autonomous: bool,
        default_cwd: str,
        roots: tuple[str, ...],
        session_rules: frozenset[tuple[str, str]] = frozenset(),
        session_path_rules: frozenset[tuple[str, str]] = frozenset(),
    ) -> SecurityDecision:
        """Judge one tool call — ``allow`` or ``deny``, never ``ask``.

        Args:
            tool_name: The tool being called.
            tool_input: The call's parsed input parameters.
            command_control: Ignored — this layer is the posture.
            autonomous: Ignored — a sandboxed run never prompts.
            default_cwd: The run's default working directory (``run_command``).
            roots: Absolute workspace root paths (to resolve logical paths).
            session_rules: Ignored — there are no user grants to honor.
            session_path_rules: Ignored — there are no user grants to honor.

        Returns:
            SecurityDecision: ``allow`` or ``deny``, with a reason.
        """
        decision = self.__judge(tool_name, tool_input, default_cwd, roots)
        _log.info("sandbox: %s %s: %s", decision.action.upper(), tool_name, decision.reason or "ok")
        return decision

    # ------------------------------------------------------------------
    # Per-tool policies
    # ------------------------------------------------------------------

    def __judge(
        self,
        tool_name: str,
        tool_input: dict[str, object],
        default_cwd: str,
        roots: tuple[str, ...],
    ) -> SecurityDecision:
        if tool_name in _DENIED_TOOLS:
            return _deny(_DENIED_TOOLS[tool_name])
        if tool_name in _ALLOWED_TOOLS:
            return _allow("")
        if bool(tool_input.get("temporary")) and tool_name in (_PATH_WRITERS | {"filesystem"}):
            return _allow("Session-scoped temporary location.")
        if tool_name in _PATH_WRITERS:
            return self.__judge_write_paths(tool_name, [tool_input.get("path")], roots)
        if tool_name == "filesystem":
            return self.__judge_filesystem(tool_input, roots)
        if tool_name == "toolchain_build":
            return self.__judge_write_paths(tool_name, [tool_input.get("project_path")], roots)
        if tool_name == "toolchain_deps":
            return self.__judge_toolchain_deps(tool_input, roots)
        if tool_name == "run_command":
            return self.__judge_run_command(tool_input, default_cwd)
        return _deny(f"'{tool_name}' has no sandbox policy.")

    def __judge_filesystem(
        self, tool_input: dict[str, object], roots: tuple[str, ...]
    ) -> SecurityDecision:
        operation = str(tool_input.get("operation", ""))
        if operation in ("copy_file", "copy_dir"):
            keys = ["destination"]  # the source is only read
        elif operation in ("move_file", "move_dir"):
            keys = ["source", "destination"]  # a move mutates both ends
        elif operation in ("delete_file", "delete_dir"):
            keys = ["path"]
        else:
            keys = ["path", "source", "destination"]
        return self.__judge_write_paths("filesystem", [tool_input.get(key) for key in keys], roots)

    def __judge_toolchain_deps(
        self, tool_input: dict[str, object], roots: tuple[str, ...]
    ) -> SecurityDecision:
        name = str(tool_input.get("name", "") or "")
        version = str(tool_input.get("version", "") or "")
        if name and _SUSPICIOUS_DEP_RE.search(name):
            return _deny(f"The dependency name '{name}' is not a plain registry package name.")
        if "://" in version or version.startswith("git+"):
            return _deny(f"The version constraint '{version}' points at an external source.")
        return self.__judge_write_paths(
            "toolchain_deps", [tool_input.get("project_root_path")], roots
        )

    def __judge_write_paths(
        self, tool_name: str, raw_paths: list[object], roots: tuple[str, ...]
    ) -> SecurityDecision:
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                continue  # Absent: the tool itself rejects a missing required path.
            target = self.__resolve_logical(raw, roots)
            if target is None:
                continue  # Unknown folder name: the tool itself rejects it.
            reason = self.__write_violation(target)
            if reason:
                return _deny(f"{tool_name}: {reason}")
        return _allow("")

    # ------------------------------------------------------------------
    # run_command
    # ------------------------------------------------------------------

    def __judge_run_command(
        self, tool_input: dict[str, object], default_cwd: str
    ) -> SecurityDecision:
        cwd = self.__effective_cwd(tool_input, default_cwd)
        if not self.__inside(cwd):
            return _deny(f"The working directory '{cwd}' is outside the sandbox root.")
        reason = self.__command_violation(str(tool_input.get("command", "")), cwd, 0)
        return _deny(reason) if reason else _allow("")

    def __command_violation(self, command: str, cwd: Path, depth: int) -> str:
        """The reason *command* must be denied, or ``""`` to allow it."""
        if depth > _MAX_DEPTH:
            return "The command nests other commands too deeply to analyze."
        win = self.__windows
        masked, _, command_subs = mask_substitutions(command, windows=win)
        for snippet in command_subs:
            inner = _strip_command_sub(snippet)
            if not inner:
                return f"Unparseable command substitution: {snippet}"
            reason = self.__command_violation(inner, cwd, depth + 1)
            if reason:
                return reason

        parsed = parse_masked(masked, windows=win)
        normalized = normalize_segments(parsed, windows=win)
        # Each segment is judged in the directory it actually runs in: an
        # inline `cd x && …` shifts it, exactly as in `_analysis`.
        cwds = _track_cwd(normalized, parsed.operators, str(cwd), win, parsed.contexts)
        for i, (raw, segment) in enumerate(zip(parsed.segments, normalized, strict=True)):
            cwd = Path(cwds[i]) if i < len(cwds) else cwd
            if segment.executable in CD_EXECUTABLES:
                reason = self.__cd_violation(segment.args, cwd, parsed.contexts, i)
                if reason:
                    return reason
                continue
            if segment.opaque_reason:
                return (
                    f"The command has a construct that cannot be analyzed: {segment.opaque_reason}"
                )
            for redir in raw.redirections:
                if not redirection_writes_file(redir) or is_fd_merge_target(redir.target):
                    continue
                reason = self.__command_target_violation(redir.target, cwd)
                if reason:
                    return f"Redirection {reason}"
            tokens = (
                peel_prefixes([raw.executable, *raw.args], windows=win) if raw.executable else []
            )
            if not tokens:
                continue
            if SUB_MARK in tokens[0]:
                return "The program to run is itself a substitution and cannot be judged."
            exe = leaf_name(tokens[0])
            if segment.nested_command is not None:
                reason = self.__command_violation(segment.nested_command, cwd, depth + 1)
                if reason:
                    return reason
                continue
            if segment.nested_opaque and _GIT_WORD_RE.search(" ".join(raw.args)):
                return f"'{exe}' runs inline code that invokes git, which cannot be judged."
            reason = self.__git_violation(exe, tokens[1:])
            if reason:
                return reason
            reason = self.__mutation_violation(exe, tokens[1:], cwd)
            if reason:
                return reason
        return ""

    def __cd_violation(
        self,
        args: tuple[str, ...],
        cwd: Path,
        contexts: tuple[frozenset[str], ...],
        index: int,
    ) -> str:
        """A ``cd`` the cwd tracker cannot follow is how a write escapes.

        ``_track_cwd`` deliberately leaves the chain untouched for a ``cd``
        whose target is a substitution, or that sits in a loop/conditional
        (whether it ran is unknowable) — so everything after it would be
        judged against the wrong directory. Deny those when they could leave
        the root.
        """
        if not args:
            return ""
        target = args[0]
        if SUB_MARK in target:
            return f"'cd' to a directory that cannot be resolved statically ({target!r})."
        context = contexts[index] if index < len(contexts) else frozenset()
        if context - {"background"} and not self.__inside(self.__resolve_os(target, cwd)):
            return "A conditional or looped 'cd' leaves the sandbox root; it cannot be followed."
        return ""

    def __git_violation(self, exe: str, rest: list[str]) -> str:
        if exe == "git":
            return self.__git_args_violation(rest)
        if exe in _EXEC_WRAPPERS:
            for i, token in enumerate(rest):
                if SUB_MARK not in token and leaf_name(token) == "git":
                    return self.__git_args_violation(rest[i + 1 :])
        return ""

    @staticmethod
    def __git_args_violation(args: list[str]) -> str:
        """Why ``git <args>`` is not a read-only git command, or ``""``."""
        i = 0
        while i < len(args) and args[i].startswith("-"):
            flag = args[i]
            name = _flag_name(flag)
            if name == "-C":
                i += 2
                continue
            if name in (
                "-c",
                "--config-env",
                "--git-dir",
                "--work-tree",
                "--namespace",
                "--exec-path",
                "--super-prefix",
            ):
                return f"git option '{name}' can redirect git or change its configuration."
            if name not in _GIT_HARMLESS_GLOBALS:
                return f"git option '{name}' is not allowed in the sandbox."
            i += 1
        if i >= len(args):
            return ""  # `git --version`, bare `git`: help text only.
        sub = args[i].lower()
        rest = args[i + 1 :]
        for token in rest:
            name = _flag_name(token)
            if name in _GIT_WRITING_OPTIONS or (name.startswith("-O") and sub == "grep"):
                return f"'git {sub} {name}' writes a file or runs a program."
        if sub in _GIT_READ_ONLY:
            return ""
        if sub == "branch" and SandboxSecurityLayer.__listing_only(rest, _GIT_BRANCH_FLAGS):
            return ""
        if sub == "tag" and SandboxSecurityLayer.__listing_only(rest, _GIT_TAG_FLAGS):
            return ""
        if sub == "remote" and (not rest or rest[0] in ("-v", "--verbose", "show", "get-url")):
            return ""
        if sub == "stash" and rest and rest[0] in ("list", "show"):
            return ""
        if sub == "worktree" and rest and rest[0] == "list":
            return ""
        if sub == "reflog" and (
            not rest or rest[0] in ("show", "exists") or rest[0].startswith("-")
        ):
            return ""
        if sub == "notes" and rest and rest[0] in ("list", "show"):
            return ""
        if sub == "submodule" and rest and rest[0] in ("status", "summary"):
            return ""
        if sub == "config":
            names = {_flag_name(t) for t in rest if t.startswith("-")}
            if (rest and rest[0] in ("get", "list")) or (
                names & _GIT_CONFIG_READS and not names & _GIT_CONFIG_WRITES
            ):
                return ""
        return f"'git {sub}' can modify the repository; only read-only git commands are allowed."

    @staticmethod
    def __listing_only(rest: list[str], allowed_flags: frozenset[str]) -> bool:
        """Whether a ``branch``/``tag`` argument list only lists refs."""
        is_list = any(_flag_name(t) in ("-l", "--list") for t in rest)
        expect_value = False
        for token in rest:
            if expect_value:
                expect_value = False
                continue
            if token.startswith("-"):
                name = _flag_name(token)
                if name.startswith("-n") and len(name) > 2 and name[2:].isdigit():
                    continue  # `git tag -n5`
                if name not in allowed_flags:
                    return False
                expect_value = name in _GIT_VALUE_FLAGS and "=" not in token
                continue
            if not is_list:
                return False  # A bare name creates a branch/tag.
        return True

    def __mutation_violation(self, exe: str, rest: list[str], cwd: Path) -> str:
        for token in self.__flag_values(rest, _TARGET_FLAGS.get(exe, ())):
            reason = self.__command_target_violation(token, cwd)
            if reason:
                return f"'{exe}' {reason}"
        positionals = [t for t in rest if not t.startswith("-")]
        if exe == "dd":
            for token in rest:
                if token.startswith("of="):
                    reason = self.__command_target_violation(token[3:], cwd)
                    if reason:
                        return f"'dd' output {reason}"
            return ""
        mode = _MUTATING_PROGRAMS.get(exe)
        if mode == "flagged":
            if not any(t == f or t.startswith(f) for t in rest for f in _IN_PLACE_FLAGS):
                mode = None
            else:
                mode = "all"
        if mode is not None:
            targets = positionals if mode == "all" else positionals[-1:]
            for token in targets:
                if SUB_MARK in token:
                    return (
                        f"'{exe}' is given a target that cannot be resolved statically ({token!r})."
                    )
                reason = self.__command_target_violation(token, cwd)
                if reason:
                    return f"'{exe}' {reason}"
            return ""
        readonly = (
            _READONLY_EXECUTABLES | _READONLY_CMDLETS if self.__windows else _READONLY_EXECUTABLES
        )
        if exe in readonly:
            return ""
        # Any other program: only a path *into .git* is evidence of mutation.
        for token in positionals:
            if SUB_MARK in token:
                continue
            target = self.__resolve_os(token, cwd)
            if self.__inside(target) and self.__in_git_dir(target):
                return f"'{exe}' is given a path inside .git; the repository may only be read."
        return ""

    @staticmethod
    def __flag_values(rest: list[str], flags: tuple[str, ...]) -> list[str]:
        """Values of *flags* in *rest* (``-o x``, ``--output=x``, ``-ox``)."""
        values: list[str] = []
        for i, token in enumerate(rest):
            for flag in flags:
                if token == flag and i + 1 < len(rest):
                    values.append(rest[i + 1])
                elif flag.startswith("--") and token.startswith(f"{flag}="):
                    values.append(token[len(flag) + 1 :])
                elif (
                    not flag.startswith("--") and token.startswith(flag) and len(token) > len(flag)
                ):
                    values.append(token[len(flag) :])
        return values

    def __command_target_violation(self, token: str, cwd: Path) -> str:
        """Why writing to *token* (from a command) is denied, or ``""``."""
        if not token:
            return ""
        if SUB_MARK in token:
            return f"targets a path that cannot be resolved statically ({token!r})."
        if token.replace("\\", "/").lower() in ("/dev/null", "/dev/stdout", "/dev/stderr", "nul"):
            return ""
        target = self.__resolve_os(token, cwd)
        if not self.__inside(target) and any(
            self.__is_under(target, Path(os.path.realpath(t))) for t in system_temp_roots()
        ):
            return ""  # The OS temp directory is carved out for commands, as in _analysis.
        return self.__write_violation(target)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def __write_violation(self, target: Path) -> str:
        if not self.__inside(target):
            return f"writes to '{target}', outside the sandbox root '{self.__root}'."
        if self.__in_git_dir(target):
            return f"writes inside .git ('{target}'); the repository may only be read."
        return ""

    def __resolve_logical(self, raw: str, roots: tuple[str, ...]) -> Path | None:
        """Mirror of ``kodo.tools.resolve_logical``, then symlinks resolved."""
        candidate = Path(os.path.expanduser(raw))
        if candidate.is_absolute():
            return Path(os.path.realpath(candidate))
        parts = candidate.parts
        if not parts:
            return None
        folders = {Path(root).name: Path(root) for root in roots}
        folders.setdefault(self.__root.name, self.__root)
        base = folders.get(parts[0])
        if base is None:
            return None
        return Path(os.path.realpath(base.joinpath(*parts[1:])))

    @staticmethod
    def __resolve_os(token: str, cwd: Path) -> Path:
        candidate = Path(os.path.expanduser(token))
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return Path(os.path.realpath(candidate))

    def __effective_cwd(self, tool_input: dict[str, object], default_cwd: str) -> Path:
        base = Path(default_cwd) if default_cwd else self.__root
        raw = tool_input.get("working_dir")
        if isinstance(raw, str) and raw.strip():
            return self.__resolve_os(raw, base)
        return Path(os.path.realpath(base))

    def __inside(self, target: Path) -> bool:
        return self.__is_under(target, self.__root)

    def __in_git_dir(self, target: Path) -> bool:
        try:
            relative = target.relative_to(self.__root)
        except ValueError:
            return False
        return ".git" in relative.parts

    @staticmethod
    def __is_under(target: Path, base: Path) -> bool:
        return target == base or base in target.parents
