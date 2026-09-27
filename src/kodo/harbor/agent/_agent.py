"""``KodoAgent`` — Kodo as a Harbor installed agent.

Harbor loads it by import path (``--agent-import-path
kodo.harbor.agent:KodoAgent``); no Harbor change is involved. Each trial:

1. ``install`` — uv, a standalone Python, ``py-kodo`` (the adapter's own
   version from PyPI, or an uploaded wheel) as a uv tool, the bundled
   rg/fd/uv utilities (so the network-restricted agent phase never needs to
   download them), and any user-installed agents.
2. ``run`` — writes the instruction to a file (never a shell argument) and runs
   one ``kodo-headless`` turn in the task's working directory: autonomous, no
   user, mutation confined to that directory (doc/HEADLESS.md). Its stdout
   goes to ``kodo.jsonl``; only the final ``KODO-RESULT`` line reaches the
   exec output, so Harbor's ``ERROR_PATTERNS`` classify the run's own error and
   never text a tool happened to print.
3. ``populate_context_post_run`` — tokens, cost, per-model usage and kodo's
   metadata from ``kodo-result.json`` (:class:`~._context.KodoRunRecord`),
   and an ATIF ``trajectory.json`` from the exported session log
   (:func:`~._trajectory.session_to_trajectory`).

Kodo's exit codes pass through unchanged. Harbor records a non-zero exit as
the trial's exception and still runs the verifier, so a run that timed out or
failed is graded on whatever it left behind — the same treatment every other
agent gets.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, override

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import (
    AgentAuthenticationError,
    BaseInstalledAgent,
    ErrorPattern,
    with_prompt_template,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from ._context import KODO_JSONL, KODO_RESULT_JSON, KodoRunRecord
from ._options import KodoAgentOptions
from ._trajectory import session_to_trajectory

__all__ = ["KODO_AGENT_NAME", "KodoAgent"]

#: The agent's Harbor name (``AgentInfo.name`` in every trial result).
KODO_AGENT_NAME = "kodo"

_INSTRUCTION_FILE = "instruction.md"
_SESSION_DIR = "kodo-session"
_STDERR_FILE = "kodo-stderr.log"
_TRAJECTORY_FILE = "trajectory.json"
_STAGING_DIR = PurePosixPath("/installed-agent")
_PID_FILE = "kodo-headless.pid"
_STOP_GRACE_SECONDS = 25
_WHEEL_VERSION = re.compile(r"^py_kodo-([^-]+)-")


class KodoAgent(BaseInstalledAgent):
    """Runs one autonomous Kodo turn inside a Harbor task container."""

    capabilities = AgentCapabilities(atif=True)
    options_model = KodoAgentOptions
    ERROR_PATTERNS = [
        # kodo-headless's own wording for a missing credential (headless/_run.py).
        ErrorPattern(
            r"KODO-RESULT outcome=startup_error error=No API key", AgentAuthenticationError
        ),
        *BaseInstalledAgent.ERROR_PATTERNS,
    ]

    __kodo_options: KodoAgentOptions

    def __init__(self, logs_dir: Path, **kwargs: Any) -> None:
        """Bind Harbor's constructor arguments and the parsed kwargs.

        Args:
            logs_dir (Path): The trial's agent log directory (host side).
            **kwargs (Any): Harbor's keyword arguments (``model_name``,
                ``extra_env``, …), including every :class:`KodoAgentOptions`
                field given as ``--agent-kwarg``.

        Raises:
            ValueError: A kwarg is invalid, or no ``-m`` model was given.
        """
        super().__init__(logs_dir, **kwargs)
        options = self.options
        if not isinstance(options, KodoAgentOptions):
            raise ValueError("KodoAgent requires KodoAgentOptions")
        if not self.model_name:
            raise ValueError(
                "KodoAgent needs a model: -m VENDOR/MODEL_ID (cloud) or -m local/ENTRY"
            )
        self.__kodo_options = options

    @staticmethod
    @override
    def name() -> str:
        """The agent's Harbor name.

        Returns:
            str: ``"kodo"``.
        """
        return KODO_AGENT_NAME

    @override
    def version(self) -> str | None:
        """The py-kodo version the trial installs.

        Returns:
            str | None: ``version`` if given, else the uploaded wheel's
            version, else this adapter's own py-kodo version.
        """
        options = self.__kodo_options
        if options.version:
            return options.version
        if options.kodo_wheel:
            match = _WHEEL_VERSION.match(Path(options.kodo_wheel).name)
            if match:
                return match.group(1)
        return _own_version()

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Install uv, Python and py-kodo (plus user agents) in the container.

        Args:
            environment (BaseEnvironment): The trial's environment.
        """
        options = self.__kodo_options
        await self.ensure_system_dependencies(environment, ("curl", "git", "tar"))
        version = self.version()
        requirement = f"py-kodo=={version}" if version else "py-kodo"
        if options.kodo_wheel:
            wheel = Path(options.kodo_wheel).expanduser()
            target = str(_STAGING_DIR / wheel.name)
            await environment.upload_file(wheel, target)
            await self.exec_as_root(environment, command=f"chmod a+r {shlex.quote(target)}")
            requirement = target
        python = shlex.quote(options.python_version)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                "if ! command -v uv >/dev/null 2>&1; then "
                "curl -LsSf https://astral.sh/uv/install.sh | sh; fi; "
                f"uv python install {python}; "
                f"uv tool install --force --python {python} {shlex.quote(requirement)}; "
                "kodo-headless --help >/dev/null; "
                '"$(uv tool dir)/py-kodo/bin/python" -c '
                '"from pathlib import Path; from kodo.binutils import ensure_all_utils; '
                "ensure_all_utils(Path.home() / '.kodo')\""
            ),
        )
        if options.agents_dir:
            staged = str(_STAGING_DIR / "kodo-agents")
            await environment.upload_dir(Path(options.agents_dir).expanduser(), staged)
            await self.exec_as_root(environment, command=f"chmod -R a+rX {shlex.quote(staged)}")
            await self.exec_as_agent(
                environment,
                command=(
                    'mkdir -p "$HOME/.kodo" && rm -rf "$HOME/.kodo/agents" && '
                    f'cp -R {shlex.quote(staged)} "$HOME/.kodo/agents"'
                ),
            )

    @override
    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """Run one autonomous kodo turn on the task.

        Args:
            instruction (str): The task instruction (already templated).
            environment (BaseEnvironment): The trial's environment.
            context (AgentContext): Filled after the run by
                :meth:`populate_context_post_run`.
        """
        if self.mcp_servers:
            self.logger.warning("Kodo has no MCP client; the task's MCP servers are not used")
        logs = PurePosixPath(str(self.environment_logs_dir))
        await self.__upload_text(environment, instruction, str(logs / _INSTRUCTION_FILE))
        try:
            await self.exec_as_agent(environment, command=self.__run_command(logs))
        except asyncio.CancelledError:
            # Harbor's agent timeout cancels the exec, but the process inside the
            # container would keep going through verification. SIGTERM makes
            # kodo-headless stop the turn, write its result and clean up.
            await asyncio.shield(self.__stop_in_container(environment))
            raise

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Fill the context and write ``trajectory.json`` from the synced logs.

        Args:
            context (AgentContext): The trial's (still empty) context.
        """
        version = self.version() or "unknown"
        record = KodoRunRecord.load(self.logs_dir)
        if record is not None:
            record.fill(context, kodo_version=version)
        session_dir = self.logs_dir / _SESSION_DIR
        if not session_dir.is_dir():
            return
        try:
            trajectory = session_to_trajectory(
                session_dir,
                agent_version=version,
                model_name=str(self.model_name),
                top_agent=self.__kodo_options.agent,
                session_id=self.session_id,
            )
        except ValueError as exc:
            self.logger.warning("Could not convert kodo's session log to ATIF: %s", exc)
            return
        if trajectory is not None:
            (self.logs_dir / _TRAJECTORY_FILE).write_text(
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # ------------------------------------------------------------------

    def __run_command(self, logs: PurePosixPath) -> str:
        options = self.__kodo_options
        args = [
            "kodo-headless",
            "--prompt-file",
            str(logs / _INSTRUCTION_FILE),
            "--model",
            str(self.model_name),
            "--agent",
            options.agent,
            "--cwd",
            "$PWD",
            "--result",
            str(logs / KODO_RESULT_JSON),
            "--transcript-dir",
            str(logs / _SESSION_DIR),
        ]
        if options.llama_url:
            args += ["--llama-url", options.llama_url]
        if options.registry_file:
            args += ["--registry-file", options.registry_file]
        if options.thinking_level:
            args += ["--thinking-level", options.thinking_level]
        # Without an explicit bound, Harbor's own agent timeout governs.
        timeout = options.turn_timeout_sec if options.turn_timeout_sec is not None else 86400.0
        args += ["--timeout", str(timeout)]
        command = " ".join('"$PWD"' if a == "$PWD" else shlex.quote(a) for a in args)
        prelude = ['export PATH="$HOME/.local/bin:$PATH"']
        if self.skills_dir:
            skills = shlex.quote(str(self.skills_dir))
            prelude.append(
                f'if [ -d {skills} ]; then mkdir -p "$HOME/.kodo/skills" && '
                f'cp -R {skills}/. "$HOME/.kodo/skills/"; fi'
            )
        if options.workdir:
            prelude.append(f"cd {shlex.quote(options.workdir)}")
        prelude.append(
            'if [ "$PWD" = "/" ]; then echo "KODO-RESULT outcome=startup_error '
            "error=the task's working directory is /; pass --ak workdir=DIR\"; exit 4; fi"
        )
        stdout = shlex.quote(str(logs / KODO_JSONL))
        stderr = shlex.quote(str(logs / _STDERR_FILE))
        pid_file = shlex.quote(str(logs / _PID_FILE))
        return "; ".join(
            [
                *prelude,
                f"{command} > {stdout} 2> {stderr} & echo $! > {pid_file}",
                f"wait $(cat {pid_file}); rc=$?",
                f"rm -f {pid_file}",
                f"tail -n 1 {stdout}",
                "exit $rc",
            ]
        )

    async def __upload_text(self, environment: BaseEnvironment, text: str, target: str) -> None:
        with tempfile.TemporaryDirectory(prefix="kodo-harbor-") as tmp:
            source = Path(tmp) / Path(target).name
            source.write_text(text, encoding="utf-8")
            await environment.upload_file(source, target)
        if environment.default_user is not None:
            owner = shlex.quote(str(environment.default_user))
            await self.exec_as_root(environment, command=f"chown {owner} {shlex.quote(target)}")

    async def __stop_in_container(self, environment: BaseEnvironment) -> None:
        pid_file = shlex.quote(str(PurePosixPath(str(self.environment_logs_dir)) / _PID_FILE))
        command = (
            f"[ -f {pid_file} ] || exit 0; pid=$(cat {pid_file}); kill -TERM $pid 2>/dev/null; "
            f"for _ in $(seq {_STOP_GRACE_SECONDS}); do "
            "kill -0 $pid 2>/dev/null || exit 0; sleep 1; done; kill -KILL $pid 2>/dev/null"
        )
        with contextlib.suppress(Exception):
            await environment.exec(command=command, timeout_sec=_STOP_GRACE_SECONDS + 10)


def _own_version() -> str | None:
    try:
        return importlib.metadata.version("py-kodo")
    except importlib.metadata.PackageNotFoundError:
        return None
