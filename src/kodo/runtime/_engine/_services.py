"""The engine-side adapter behind the tools' ``EngineServices`` protocol."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from kodo.tools import RootPath


class _EngineServices:
    """Adapts the engine's operations to the tools ``EngineServices`` protocol.

    Every engine-side action a tool can trigger — spawning sub-agents, running
    an Author/Critic round, rolling back, disabling autonomous mode, and
    creating a project — is funnelled through this single adapter. It lets
    the tools depend only on the protocol declared in :mod:`kodo.tools` while
    agent loading and the LLM tool-loop stay in the engine. The engine builds
    one instance and injects it into every per-run :class:`ToolDispatcher`.
    """

    def __init__(
        self,
        *,
        run_subagent: Callable[
            [str, str, dict[str, object], int | None], Awaitable[dict[str, object]]
        ],
        run_dependency_manager: Callable[[dict[str, object]], Awaitable[dict[str, object]]],
        run_web_search_agent: Callable[[dict[str, object], str], Awaitable[dict[str, object]]],
        rollback: Callable[[str, str], Awaitable[None]],
        disable_autonomous: Callable[[], Awaitable[None]],
        create_project: Callable[[str, str | None, bool], Awaitable[dict[str, object]]],
        init_project: Callable[[str], Awaitable[dict[str, object]]],
        bootstrap_project: Callable[[str], Awaitable[dict[str, object]]],
        notify_tool_call_in_progress: Callable[[str], Awaitable[None]],
        add_security_rule: Callable[[str, str, str], Awaitable[None]],
        add_security_path_rule: Callable[[str, str, str], Awaitable[None]],
        has_workspace: Callable[[], bool],
        root_paths: Callable[[], tuple[RootPath, ...]],
    ) -> None:
        self.__run_subagent = run_subagent
        self.__run_dependency_manager = run_dependency_manager
        self.__run_web_search_agent = run_web_search_agent
        self.__rollback = rollback
        self.__disable_autonomous = disable_autonomous
        self.__create_project = create_project
        self.__init_project = init_project
        self.__bootstrap_project = bootstrap_project
        self.__notify_tool_call_in_progress = notify_tool_call_in_progress
        self.__add_security_rule = add_security_rule
        self.__add_security_path_rule = add_security_path_rule
        self.__has_workspace = has_workspace
        self.__root_paths = root_paths

    async def run_subagent(
        self,
        caller: str,
        name: str,
        task_input: dict[str, object],
        max_rounds: int | None = None,
    ) -> dict[str, object]:
        """Delegate to the engine's caller-gated sub-agent spawn.

        The engine decides from the callee's own frontmatter whether this is
        one pass or a whole author/critic loop; ``max_rounds`` only bounds the
        latter."""
        return await self.__run_subagent(caller, name, task_input, max_rounds)

    async def run_dependency_manager(self, task_input: dict[str, object]) -> dict[str, object]:
        """Delegate to the engine's ungated dependency-manager spawn."""
        return await self.__run_dependency_manager(task_input)

    async def run_web_search_agent(
        self, task_input: dict[str, object], tool_call_id: str
    ) -> dict[str, object]:
        """Delegate to the engine's ungated, silent web_search agent run."""
        return await self.__run_web_search_agent(task_input, tool_call_id)

    async def rollback(self, root: str, target_sha: str) -> None:
        """Delegate to the engine's ``_run_rollback``."""
        await self.__rollback(root, target_sha)

    async def disable_autonomous_mode(self) -> None:
        """Delegate to the engine's ``_disable_autonomous``."""
        await self.__disable_autonomous()

    async def create_project(
        self, name: str = "", path: str | None = None, force: bool = False
    ) -> dict[str, object]:
        """Delegate to the engine's ``_create_project``."""
        return await self.__create_project(name, path, force)

    async def init_project(self, path: str) -> dict[str, object]:
        """Delegate to the engine's ``_init_project``."""
        return await self.__init_project(path)

    async def bootstrap_project(self, name: str = "") -> dict[str, object]:
        """Delegate to the engine's ``_bootstrap_project``."""
        return await self.__bootstrap_project(name)

    async def notify_tool_call_in_progress(self, tool_call_id: str) -> None:
        """Delegate to the emitters' ``notify_tool_call_in_progress``."""
        await self.__notify_tool_call_in_progress(tool_call_id)

    async def add_security_rule(self, scope: str, executable: str, subcommand: str) -> None:
        """Delegate to the engine's ``add_security_rule``."""
        await self.__add_security_rule(scope, executable, subcommand)

    async def add_security_path_rule(self, scope: str, executable: str, path: str) -> None:
        """Delegate to the engine's ``add_security_path_rule``."""
        await self.__add_security_path_rule(scope, executable, path)

    def has_workspace(self) -> bool:
        """Delegate to the engine's ``_has_workspace``, read live."""
        return self.__has_workspace()

    def root_paths(self) -> tuple[RootPath, ...]:
        """Delegate to the engine's ``_root_paths``, read live."""
        return self.__root_paths()
