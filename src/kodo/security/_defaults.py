"""The built-in default rule tables for the heuristic command engine.

One ordered table per shell dialect, consumed by :func:`._rules.
evaluate_command`. **Order matters**: the evaluator takes the first matching
rule per segment, so specific rules (``git push --force``) precede general
ones (``git push``), and a tool's ask-rules precede its allow-rules.

Curation principles (doc/SECURITY_RULES_PLAN.md):

- **Build/test script runners always allow** — running the project's own
  build, tests, linters, and scripts is the agent's core loop and is
  workspace-confined by the path checks that run before these rules.
- **Deployment, destructive, system-state, network-egress, and privilege
  operations always ask**, each with a category-labelled, deterministic
  reason.
- **Unlisted commands ask by default** (the evaluator's default) — the
  tables only need to cover the *known* safe and dangerous sets, not the
  world.
- ``rule_eligible=True`` marks asks a Phase 2 user rule may permanently
  override; destructive / privilege / obfuscation findings are never
  eligible.
"""

from __future__ import annotations

from ._rules import CommandRule

__all__ = ["default_rules"]


def _ask(
    executable: str | tuple[str, ...],
    subcommand: str | None = None,
    flags_any: tuple[str, ...] = (),
    *,
    category: str,
    reason: str,
    eligible: bool = False,
) -> CommandRule:
    return CommandRule(
        executable=executable,
        subcommand=subcommand,
        flags_any=flags_any,
        verdict="ask",
        category=category,
        reason=reason,
        rule_eligible=eligible,
    )


def _allow(
    executable: str | tuple[str, ...],
    subcommand: str | None = None,
    flags_any: tuple[str, ...] = (),
) -> CommandRule:
    return CommandRule(
        executable=executable,
        subcommand=subcommand,
        flags_any=flags_any,
        verdict="allow",
        category="benign-dev",
    )


# ---------------------------------------------------------------------------
# Cross-platform developer tools (git, package managers, build/test runners).
# ---------------------------------------------------------------------------

_GIT_SAFE_SUBCOMMANDS = (
    "status",
    "diff",
    "log",
    "show",
    "blame",
    "grep",
    "rev-parse",
    "describe",
    "shortlog",
    "reflog",
    "ls-files",
    "add",
    "commit",
    "checkout",
    "switch",
    "restore",
    "branch",
    "stash",
    "fetch",
    "pull",
    "merge",
    "rebase",
    "cherry-pick",
    "tag",
    "init",
    "rm",
    "mv",
    "worktree",
    "bisect",
    "apply",
    "format-patch",
    "notes",
)

# Per-subcommand allows: a bare `git <unknown-subcommand>` must fall to the
# default-ask, not blanket-allow.
_GIT_ALLOWS = tuple(_allow("git", sub) for sub in _GIT_SAFE_SUBCOMMANDS)

_SHARED_RULES: tuple[CommandRule, ...] = (
    # --- git ---
    _ask(
        "git",
        "push",
        ("--force", "-f", "--force-with-lease", "--delete", "--mirror"),
        category="destructive",
        reason="'git push' with force/delete rewrites or removes remote history.",
    ),
    _ask(
        "git",
        "push",
        category="deployment",
        reason="'git push' publishes commits to a remote.",
        eligible=True,
    ),
    _ask(
        "git",
        "reset",
        ("--hard",),
        category="destructive",
        reason="'git reset --hard' discards uncommitted work.",
    ),
    _ask(
        "git",
        "clean",
        category="destructive",
        reason="'git clean' permanently deletes untracked files.",
    ),
    _ask(
        "git",
        "config",
        ("--global", "--system"),
        category="system",
        reason="Changes git configuration outside this repository.",
    ),
    *_GIT_ALLOWS,
    # --- npm / pnpm / yarn / bun ---
    _ask(
        ("npm", "pnpm", "yarn", "bun"),
        "publish",
        category="deployment",
        reason="Publishes a package to a registry.",
        eligible=True,
    ),
    _ask(
        ("npm", "pnpm", "yarn", "bun"),
        None,
        ("-g", "--global"),
        category="system",
        reason="Installs or removes packages globally, outside the workspace.",
        eligible=True,
    ),
    _ask(
        ("npx", "uvx"),
        None,
        category="system",
        reason="Downloads and executes a package that is not a project dependency.",
        eligible=True,
    ),
    _ask(
        ("npm", "pnpm", "yarn"),
        "exec",
        category="system",
        reason="Downloads and executes a package that is not a project dependency.",
        eligible=True,
    ),
    _ask(
        ("pnpm", "yarn"),
        "dlx",
        category="system",
        reason="Downloads and executes a package that is not a project dependency.",
        eligible=True,
    ),
    _allow(("npm", "pnpm", "yarn", "bun"), None),
    # --- Python packaging / runners ---
    _ask(
        ("pip", "pip3"),
        "install",
        ("--user", "--break-system-packages", "--target"),
        category="system",
        reason="Installs Python packages outside the project environment.",
        eligible=True,
    ),
    _ask(
        ("twine",),
        None,
        category="deployment",
        reason="Uploads a package to a registry.",
        eligible=True,
    ),
    _ask(
        ("uv", "poetry", "hatch", "flit"),
        "publish",
        category="deployment",
        reason="Publishes a package to a registry.",
        eligible=True,
    ),
    _allow(("pip", "pip3"), None),
    _allow(("uv", "poetry", "pipenv", "hatch", "tox", "nox", "flit"), None),
    # --- Rust / Go / JVM / .NET ---
    _ask(
        "cargo",
        "publish",
        category="deployment",
        reason="Publishes a crate to a registry.",
        eligible=True,
    ),
    _ask(
        "cargo",
        "install",
        category="system",
        reason="Installs a binary globally, outside the workspace.",
        eligible=True,
    ),
    _allow("cargo", None),
    _ask(
        "go",
        "install",
        category="system",
        reason="Installs a binary globally, outside the workspace.",
        eligible=True,
    ),
    _allow("go", None),
    _ask(
        ("mvn", "gradle", "gradlew"),
        "deploy",
        category="deployment",
        reason="Deploys artifacts to a remote repository.",
        eligible=True,
    ),
    _ask(
        ("mvn", "gradle", "gradlew"),
        "publish",
        category="deployment",
        reason="Publishes artifacts to a remote repository.",
        eligible=True,
    ),
    _allow(("mvn", "gradle", "gradlew"), None),
    _allow("dotnet", None),
    # --- Build systems & test runners (always allowed by decision) ---
    _allow(("make", "cmake", "ctest", "ninja", "meson", "bazel"), None),
    _allow(("pytest", "jest", "vitest", "mocha", "playwright", "cypress", "rspec", "phpunit")),
    # --- Linters / formatters / type-checkers ---
    _allow(
        (
            "ruff",
            "black",
            "isort",
            "flake8",
            "pylint",
            "mypy",
            "pyright",
            "eslint",
            "prettier",
            "tsc",
            "rustfmt",
            "golangci-lint",
            "shellcheck",
            "stylelint",
        )
    ),
    # --- Interpreters / compilers running workspace sources ---
    # (`python -c` inline code never reaches these: it is flagged opaque
    # upstream; `python -m mod` is re-classified as `mod`.)
    _allow(
        (
            "python",
            "python3",
            "node",
            "ruby",
            "perl",
            "php",
            "java",
            "javac",
            "gcc",
            "g++",
            "cc",
            "c++",
            "clang",
            "clang++",
            "rustc",
            "swiftc",
        )
    ),
    # Python stdlib modules agents routinely run via `python -m` (which the
    # classifier re-writes to the module name): syntax checks, venv creation,
    # the stdlib test runner.
    _allow(("py_compile", "compileall", "venv", "unittest")),
    # --- Containers ---
    _ask(
        ("docker", "podman"),
        "push",
        category="deployment",
        reason="Pushes an image to a registry.",
        eligible=True,
    ),
    _ask(
        ("docker", "podman"),
        "login",
        category="network",
        reason="Authenticates against a remote registry.",
        eligible=True,
    ),
    _ask(
        ("docker", "podman"),
        "run",
        category="system",
        reason="Starts a container that may mount and modify host paths.",
        eligible=True,
    ),
    _allow(("docker", "podman"), "build"),
    _allow(("docker", "podman"), "images"),
    _allow(("docker", "podman"), "ps"),
    _allow(("docker", "podman"), "logs"),
    _allow(("docker", "podman"), "inspect"),
    # --- Deployment / infrastructure CLIs: always ask, whole executable ---
    _ask(
        (
            "kubectl",
            "helm",
            "terraform",
            "pulumi",
            "ansible",
            "ansible-playbook",
            "aws",
            "gcloud",
            "az",
            "doctl",
            "heroku",
            "flyctl",
            "fly",
            "vercel",
            "netlify",
            "wrangler",
            "gh",
        ),
        None,
        category="deployment",
        reason="Operates on remote infrastructure or services.",
        eligible=True,
    ),
    # --- Network ---
    _ask(
        ("curl", "wget"),
        None,
        (
            "-x",
            "--request",
            "-d",
            "--data",
            "--data-raw",
            "--data-binary",
            "-f",
            "--form",
            "-t",
            "--upload-file",
        ),
        category="network",
        reason="Sends data to a remote host.",
    ),
    _ask(
        ("curl", "wget"),
        None,
        category="network",
        reason="Fetches content from the network.",
        eligible=True,
    ),
    _ask(
        ("ssh", "scp", "sftp", "rsync", "ftp"),
        None,
        category="network",
        reason="Transfers data to or from a remote host.",
        eligible=True,
    ),
    _ask(
        ("nc", "ncat", "netcat", "telnet"),
        None,
        category="network",
        reason="Opens a raw network connection.",
    ),
)

# ---------------------------------------------------------------------------
# POSIX-specific.
# ---------------------------------------------------------------------------

_POSIX_RULES: tuple[CommandRule, ...] = _SHARED_RULES + (
    _ask(
        ("sudo", "su", "doas"),
        None,
        category="privilege",
        reason="Escalates privileges.",
    ),
    _ask(
        "rm",
        None,
        ("-r", "-R", "--recursive"),
        category="destructive",
        reason="Recursively deletes a directory tree.",
    ),
    _allow("rm"),
    _ask(
        ("dd", "shred", "mkfs", "fdisk", "diskutil"),
        None,
        category="destructive",
        reason="Writes directly to disks or destroys data irrecoverably.",
    ),
    _ask(
        ("shutdown", "reboot", "halt", "poweroff"),
        None,
        category="system",
        reason="Shuts down or restarts the machine.",
    ),
    _ask(
        ("systemctl", "service", "launchctl", "crontab", "at"),
        None,
        category="system",
        reason="Changes system services or scheduled tasks.",
    ),
    _ask(
        (
            "apt",
            "apt-get",
            "yum",
            "dnf",
            "pacman",
            "apk",
            "brew",
            "port",
            "snap",
            "flatpak",
        ),
        None,
        category="system",
        reason="Installs or removes system-level packages.",
        eligible=True,
    ),
    _ask(
        ("chown", "chgrp"),
        None,
        category="system",
        reason="Changes file ownership.",
    ),
    _ask(
        ("pkill", "killall"),
        None,
        category="system",
        reason="Kills processes by name.",
    ),
    _ask(
        ("useradd", "userdel", "usermod", "passwd"),
        None,
        category="system",
        reason="Modifies user accounts.",
    ),
    _ask(
        ("ifconfig", "ip", "route", "iptables", "ufw", "pfctl"),
        None,
        category="system",
        reason="Changes network configuration.",
    ),
    _ask(
        "find",
        None,
        ("-delete", "-exec", "-execdir", "-ok", "-okdir"),
        category="destructive",
        reason="'find' with -delete/-exec acts on every matched file.",
    ),
    _allow("find"),
    # In-workspace file mutators: their path arguments are visible to the
    # outside-workspace check that runs before these rules, and workspace
    # changes are checkpointed.
    _allow(
        (
            "cp",
            "mv",
            "mkdir",
            "touch",
            "ln",
            "chmod",
            "sed",
            "awk",
            "sort",
            "tee",
            "tr",
            "patch",
            "truncate",
            "split",
            "csplit",
            "xxd",
            "od",
        )
    ),
    _allow(("tar", "zip", "unzip", "gzip", "gunzip", "xz", "zstd", "7z")),
    _allow(("base64", "md5sum", "shasum", "sha1sum", "sha256sum", "sha512sum")),
    # Shell builtins that only affect the (per-call, throwaway) shell.
    # `ulimit` is judged per-segment instead (`._rules._DUAL_MODE`) — a value
    # argument sets a resource limit, which isn't unconditionally benign.
    _allow(("cd", "export", "set", "unset", "alias", "source", ".")),
    # Process/system introspection (read-only in effect). `sysctl` is judged
    # per-segment instead (`._rules._DUAL_MODE`) — `-w`/assignment form
    # writes a live kernel parameter.
    _allow(("ps", "top", "free", "uptime", "lsof", "netstat", "ss", "nproc")),
)

# ---------------------------------------------------------------------------
# Windows-specific (canonical cmdlet names — aliases resolved upstream).
# ---------------------------------------------------------------------------

_WINDOWS_RULES: tuple[CommandRule, ...] = _SHARED_RULES + (
    _ask(
        "remove-item",
        None,
        ("-recurse", "-r", "/s"),  # `/s` covers the `rd /s` cmd builtin form
        category="destructive",
        reason="Recursively deletes a directory tree.",
    ),
    _allow("remove-item"),
    _ask(
        "invoke-expression",
        None,
        category="obfuscation",
        reason="Executes a dynamically-built command string.",
    ),
    _ask(
        ("invoke-webrequest", "invoke-restmethod"),
        None,
        ("-method", "-body", "-infile"),
        category="network",
        reason="Sends data to a remote host.",
    ),
    _ask(
        ("invoke-webrequest", "invoke-restmethod"),
        None,
        category="network",
        reason="Fetches content from the network.",
        eligible=True,
    ),
    _ask(
        "start-process",
        None,
        ("-verb",),
        category="privilege",
        reason="Launches a process with elevated privileges.",
    ),
    _ask(
        ("set-executionpolicy", "reg", "schtasks", "sc", "net", "netsh", "powercfg", "wmic"),
        None,
        category="system",
        reason="Changes system configuration, services, or scheduled tasks.",
    ),
    _ask(
        ("new-itemproperty", "set-itemproperty", "remove-itemproperty"),
        None,
        category="system",
        reason="Writes to the Windows registry.",
    ),
    _ask(
        ("start-service", "stop-service", "set-service", "restart-service"),
        None,
        category="system",
        reason="Changes system services.",
    ),
    _ask(
        ("stop-process", "taskkill"),
        None,
        category="system",
        reason="Kills processes.",
    ),
    _ask(
        ("bcdedit", "diskpart", "format"),
        None,
        category="destructive",
        reason="Writes directly to disks or boot configuration.",
    ),
    _ask(
        ("rundll32", "regsvr32", "mshta", "certutil", "bitsadmin", "cscript", "wscript"),
        None,
        category="obfuscation",
        reason="Executes code through an indirect system host.",
    ),
    _ask(
        ("icacls", "takeown"),
        None,
        category="privilege",
        reason="Changes file ownership or permissions.",
    ),
    _ask(
        ("winget", "choco", "scoop"),
        None,
        category="system",
        reason="Installs or removes system-level packages.",
        eligible=True,
    ),
    _allow(("copy-item", "move-item", "rename-item", "new-item", "set-location")),
    _allow(("set-content", "add-content", "out-file")),
    _allow(("xcopy", "robocopy", "mklink", "attrib")),
)


def default_rules(windows: bool) -> tuple[CommandRule, ...]:
    """The built-in ordered rule table for the given dialect."""
    return _WINDOWS_RULES if windows else _POSIX_RULES
