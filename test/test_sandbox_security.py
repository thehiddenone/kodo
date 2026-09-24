"""The headless sandbox posture (``kodo.security.SandboxSecurityLayer``) and the
dispatcher's ``deny`` verdict (doc/HEADLESS.md, doc/SECURITY.md).

The sandbox root is a ``tmp_path`` subdirectory. Command-side "outside"
targets use an absolute path outside every OS temp directory, since commands
keep ``_analysis``'s temp-directory carve-out; file tools have no carve-out,
so ``tmp_path``'s own parent is "outside" for them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kodo.security import SandboxSecurityLayer, SecurityDecision, SecurityLayer
from kodo.tools import DISPATCHABLE_TOOLS_BY_NAME

_OUTSIDE = "/usr/local/kodo-sandbox-test/target.txt"


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    (path / ".git").mkdir()
    return path


async def _eval(
    root: Path,
    tool_name: str,
    tool_input: dict[str, object],
    *,
    command_control: str = "smart",
    autonomous: bool = True,
) -> SecurityDecision:
    return await SandboxSecurityLayer(root, windows=False).evaluate(
        tool_name=tool_name,
        tool_input=tool_input,
        command_control=command_control,
        autonomous=autonomous,
        default_cwd=str(root),
        roots=(str(root),),
    )


async def _cmd(root: Path, command: str, **extra: object) -> SecurityDecision:
    return await _eval(root, "run_command", {"command": command, **extra})


# ----------------------------------------------------------------------
# Coverage and posture independence
# ----------------------------------------------------------------------


def test_every_dispatchable_tool_has_an_explicit_sandbox_policy() -> None:
    missing = sorted(set(DISPATCHABLE_TOOLS_BY_NAME) - SandboxSecurityLayer.classified_tools())
    assert missing == [], f"tools with no sandbox policy (would be denied): {missing}"


async def test_unclassified_tool_is_denied(root: Path) -> None:
    decision = await _eval(root, "some_future_tool", {})
    assert decision.action == "deny"


@pytest.mark.parametrize("command_control", ["permissive", "defensive", "smart"])
@pytest.mark.parametrize("autonomous", [True, False])
async def test_verdict_ignores_command_control_and_autonomous(
    root: Path, command_control: str, autonomous: bool
) -> None:
    denied = await _eval(
        root,
        "run_command",
        {"command": "git commit -m x"},
        command_control=command_control,
        autonomous=autonomous,
    )
    allowed = await _eval(
        root,
        "run_command",
        {"command": "pytest -q"},
        command_control=command_control,
        autonomous=autonomous,
    )
    assert (denied.action, allowed.action) == ("deny", "allow")


async def test_sandbox_never_asks(root: Path) -> None:
    for tool in sorted(DISPATCHABLE_TOOLS_BY_NAME):
        decision = await _eval(root, tool, {})
        assert decision.action in ("allow", "deny"), tool


@pytest.mark.parametrize("tool", ["scaffold_new_project", "disable_autonomous_mode"])
async def test_denied_tools(root: Path, tool: str) -> None:
    assert (await _eval(root, tool, {"path": str(root)})).action == "deny"


# ----------------------------------------------------------------------
# File tools
# ----------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["create_file", "create_directory", "edit_file"])
async def test_path_writers_inside_allowed_outside_denied(root: Path, tool: str) -> None:
    assert (await _eval(root, tool, {"path": str(root / "src" / "a.py")})).action == "allow"
    assert (await _eval(root, tool, {"path": "proj/src/a.py"})).action == "allow"
    outside = root.parent / "escape.py"
    assert (await _eval(root, tool, {"path": str(outside)})).action == "deny"
    assert (await _eval(root, tool, {"path": "proj/../escape.py"})).action == "deny"


async def test_symlink_inside_root_pointing_outside_is_caught(root: Path) -> None:
    elsewhere = root.parent / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, root / "link")
    decision = await _eval(root, "create_file", {"path": str(root / "link" / "x.txt")})
    assert decision.action == "deny"


async def test_writes_into_dot_git_denied(root: Path) -> None:
    decision = await _eval(root, "edit_file", {"path": str(root / ".git" / "config")})
    assert decision.action == "deny"
    assert ".git" in decision.reason


async def test_temporary_writes_allowed(root: Path) -> None:
    decision = await _eval(root, "create_file", {"path": "/anywhere/x", "temporary": True})
    assert decision.action == "allow"


async def test_reads_anywhere_allowed(root: Path) -> None:
    assert (await _eval(root, "read_file", {"path": "/etc/hosts"})).action == "allow"
    assert (await _eval(root, "find_files", {"root": "/usr"})).action == "allow"


async def test_filesystem_copy_reads_source_but_move_mutates_it(root: Path) -> None:
    outside_src = str(root.parent / "src.txt")
    inside_dst = str(root / "dst.txt")
    copy = {"operation": "copy_file", "source": outside_src, "destination": inside_dst}
    move = {"operation": "move_file", "source": outside_src, "destination": inside_dst}
    assert (await _eval(root, "filesystem", copy)).action == "allow"
    assert (await _eval(root, "filesystem", move)).action == "deny"
    delete = {"operation": "delete_dir", "path": str(root / ".git")}
    assert (await _eval(root, "filesystem", delete)).action == "deny"


async def test_toolchain_deps_rejects_suspicious_names_and_outside_roots(root: Path) -> None:
    ok = {"action": "add", "name": "requests", "project_root_path": str(root)}
    assert (await _eval(root, "toolchain_deps", ok)).action == "allow"
    url = {**ok, "name": "git+https://evil/x"}
    assert (await _eval(root, "toolchain_deps", url)).action == "deny"
    outside = {**ok, "project_root_path": str(root.parent)}
    assert (await _eval(root, "toolchain_deps", outside)).action == "deny"


# ----------------------------------------------------------------------
# run_command — git
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -5",
        "git diff HEAD~1 -- src",
        "git show HEAD:README.md",
        "git -C sub log",
        "git --no-pager diff",
        "git branch",
        "git branch -a -v",
        "git branch --contains HEAD",
        "git tag -l 'v*'",
        "git remote -v",
        "git stash list",
        "git config --get user.name",
        "git config list",
        "git worktree list",
        "git grep -o foo",
        "git --version",
        "git status | head -5",
        "cd src && git log",
    ],
)
async def test_read_only_git_allowed(root: Path, command: str) -> None:
    decision = await _cmd(root, command)
    assert decision.action == "allow", decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m wip",
        "git add .",
        "git push",
        "git checkout main",
        "git reset --hard HEAD~1",
        "git stash",
        "git branch feature",
        "git branch -D main",
        "git tag v1.0",
        "git config user.name x",
        "git -c core.hooksPath=/x status",
        "git --git-dir=/elsewhere/.git log",
        "git -C sub commit -m x",
        "git log --output=out.txt",
        "env GIT_TRACE=1 git commit -m x",
        "sh -c 'git commit -m x'",
        "echo ok && git merge other",
        "xargs git add",
        "find . -name '*.py' -exec git add {} ;",
        "sudo git clean -fdx",
        "echo $(git commit -m x)",
        "python -c \"import subprocess; subprocess.run(['git', 'commit'])\"",
    ],
)
async def test_mutating_git_denied(root: Path, command: str) -> None:
    decision = await _cmd(root, command)
    assert decision.action == "deny", command


# ----------------------------------------------------------------------
# run_command — writes
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "make build",
        "pip install requests",
        "npm install",
        "python build.py",
        "cat /etc/hosts",
        "grep -r TODO /usr/include",
        "cp /usr/share/dict/words words.txt",
        "gcc -I/usr/include/foo -o app main.c",
        "echo hi > out.txt",
        "echo hi > /tmp/scratch.txt",
        "echo hi > ../sibling-in-os-temp.txt",
        "cd /usr/include && ls",
        "cd src && touch new.py",
        "mkdir -p build/out",
        "rm -rf build",
        "sed -i 's/a/b/' src/x.py",
        "ls /",
    ],
)
async def test_confined_or_opaque_commands_allowed(root: Path, command: str) -> None:
    decision = await _cmd(root, command)
    assert decision.action == "allow", decision.reason


@pytest.mark.parametrize(
    "command",
    [
        f"echo hi > {_OUTSIDE}",
        f"echo hi >> {_OUTSIDE}",
        f"cd {Path(_OUTSIDE).parent} && touch t.txt",
        f"cd {Path(_OUTSIDE).parent}; echo x > t.txt",
        "cd $SOMEWHERE && touch t.txt",
        f"if true; then cd {Path(_OUTSIDE).parent}; fi; touch t.txt",
        f"rm -f {_OUTSIDE}",
        f"touch {_OUTSIDE}",
        f"cp words.txt {_OUTSIDE}",
        f"mv {_OUTSIDE} here.txt",
        f"sed -i 's/a/b/' {_OUTSIDE}",
        f"tee {_OUTSIDE}",
        f"curl -o {_OUTSIDE} https://example.com",
        f"tar -xf a.tar -C {Path(_OUTSIDE).parent}",
        f"dd if=/dev/zero of={_OUTSIDE}",
        "rm -rf .git",
        "echo x > .git/HEAD",
        "python tool.py .git/config",
        "rm -rf $TARGET",
        "$RUNNER build",
    ],
)
async def test_evidence_of_outside_or_git_writes_denied(root: Path, command: str) -> None:
    decision = await _cmd(root, command)
    assert decision.action == "deny", command


async def test_working_dir_outside_root_denied(root: Path) -> None:
    decision = await _cmd(root, "ls", working_dir=str(root.parent))
    assert decision.action == "deny"


# ----------------------------------------------------------------------
# Dispatcher: the `deny` verdict
# ----------------------------------------------------------------------


class _RecordingGate:
    def __init__(self) -> None:
        self.fired: list[dict[str, object]] = []

    async def fire_permission(self, **kwargs: object):  # noqa: ANN201
        self.fired.append(kwargs)
        raise AssertionError("a deny must never reach the permission prompt")

    async def fire_questions(self, questions, tool_call_id=""):  # noqa: ANN001, ANN201
        raise AssertionError("not used")

    async def fire_approval(self, gate_type, **kwargs):  # noqa: ANN001, ANN201
        raise AssertionError("not used")


class _FakeSession:
    phase = "running"
    effective_autonomous = True
    command_control = "permissive"
    security_rules: frozenset[tuple[str, str]] = frozenset()
    security_path_rules: frozenset[tuple[str, str]] = frozenset()


class _FakeWorkspaceServices:
    def __init__(self, root_paths: tuple[object, ...]) -> None:
        self._root_paths = root_paths

    def has_workspace(self) -> bool:
        return True

    def root_paths(self) -> tuple[object, ...]:
        return self._root_paths

    def project_root(self) -> object | None:
        return None

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        return None


class _DenyAll:
    async def evaluate(self, **kwargs: object) -> SecurityDecision:
        return SecurityDecision(action="deny", reason="nope", source="sandbox")


def _dispatcher(root: Path, security: object, gate: _RecordingGate):  # noqa: ANN202
    from kodo.project import SessionWorkspace
    from kodo.tools import LogicalPathResolver, RootPath, ToolDispatcher

    return ToolDispatcher(
        resolver=LogicalPathResolver(SessionWorkspace(root, {"proj": root})),
        gate=gate,  # type: ignore[arg-type]
        security=security,  # type: ignore[arg-type]
        session=_FakeSession(),  # type: ignore[arg-type]
        services=_FakeWorkspaceServices((RootPath(name="proj", path=str(root)),)),  # type: ignore[arg-type]
        agent_name="tester",
        session_id="s1",
    )


async def test_deny_returns_error_without_prompt_or_handler(root: Path) -> None:
    gate = _RecordingGate()
    marker = root / "marker.txt"
    result = json.loads(
        await _dispatcher(root, _DenyAll(), gate).dispatch(
            "run_command",
            {"intent": "write marker", "command": f"touch {marker}", "timeout": 5},
            "tu_1",
        )
    )
    assert result == {"error": "Blocked by the headless sandbox: nope"}
    assert not marker.exists()
    assert gate.fired == []


async def test_sandbox_layer_through_dispatcher_blocks_git_commit_and_runs_pytest_like(
    root: Path,
) -> None:
    gate = _RecordingGate()
    dispatcher = _dispatcher(root, SandboxSecurityLayer(root), gate)
    blocked = json.loads(
        await dispatcher.dispatch(
            "run_command", {"intent": "commit", "command": "git commit -m x", "timeout": 5}, "tu_2"
        )
    )
    assert blocked["error"].startswith("Blocked by the headless sandbox:")
    ran = json.loads(
        await dispatcher.dispatch(
            "run_command", {"intent": "print", "command": "echo hi", "timeout": 5}, "tu_3"
        )
    )
    assert ran["exit_code"] == 0
    assert gate.fired == []


async def test_default_security_layer_never_denies(root: Path) -> None:
    for tool in sorted(DISPATCHABLE_TOOLS_BY_NAME):
        decision = await SecurityLayer().evaluate(
            tool_name=tool,
            tool_input={},
            command_control="smart",
            autonomous=False,
            default_cwd=str(root),
            roots=(str(root),),
        )
        assert decision.action != "deny", tool
