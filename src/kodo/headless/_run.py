"""One headless run, end to end.

1. Build the isolated home (:func:`~._home.build_headless_home`).
2. Spawn mode only: start ``kodo-llama-server --foreground`` on a free port,
   with the user's *real* home (that is where the models are).
3. Spawn ``kodo-server --headless-sandbox <cwd> --llama-url <url>`` with the
   isolated home, via :class:`kodo.validator.ServerProcess`.
4. Drive one session: ``hello`` → ``agent.set`` → ``workspace.folders`` (the
   one root) → ``mode.set {autonomous}`` → optional ``thinking_level.set`` →
   ``prompt.submit`` → wait for the turn to end (bounded by ``timeout``).
5. Always clean up — stop the turn on timeout or signal, shut down the server
   and llama-server, remove ``<cwd>/.kodo`` if this run created it, and the
   isolated home unless asked to keep it — then report a
   :class:`~._result.RunResult`.

Never imports the engine: the server and llama-server are spawned by module
name, so protocol drift breaks this loudly instead of silently (the same rule
``kodo.validator`` holds).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from kodo.transport import (
    MSG_AGENT_SET,
    MSG_MODE_SET,
    MSG_PROMPT_SUBMIT,
    MSG_THINKING_LEVEL_SET,
    MSG_TOP_AGENTS_LIST,
    MSG_WORKSPACE_FOLDERS,
)
from kodo.validator import ServerProcess, ServerStartError

from ._client import HeadlessClient, RequestError
from ._events import EventSink
from ._home import build_headless_home
from ._result import RunOutcome, RunResult

__all__ = ["HeadlessOptions", "HeadlessRun", "install_signal_handlers"]

_log = logging.getLogger(__name__)

_LLAMA_READY_TIMEOUT = 300.0
_STOP_SETTLE_TIMEOUT = 30.0
_SERVER_START_TIMEOUT = 60.0


@dataclass(frozen=True)
class HeadlessOptions:
    """Everything one run needs, as parsed from the command line.

    Attributes:
        prompt: The instruction for the agent.
        model: Local-registry entry name (LLM + quant). Required even with
            ``llama_url``: it is kodo's model identity (thinking family,
            context window, sampling defaults), checked against the endpoint.
        cwd: The sandbox root — the only directory the run may mutate.
        agent: Top-level agent (``agent.set``).
        llama_url: Attach to this running llama-server; ``None`` spawns one.
        llama_port: Port for a spawned llama-server (``None`` picks a free one).
        registry_file: Local-LLM registry to use instead of ``~/.kodo``'s.
        thinking_level: Thinking tier for the session, or ``None``.
        timeout: Seconds the turn may take.
        home: Where to build the isolated home (kept); ``None`` = temporary.
        keep_home: Keep a temporary isolated home after the run.
        keep_kodo_dir: Keep ``<cwd>/.kodo`` even if this run created it.
        result_path: Also write the result JSON here.
        log_level: The spawned server's log level.
    """

    prompt: str
    model: str
    cwd: Path
    agent: str = "kodo_problem_solver"
    llama_url: str | None = None
    llama_port: int | None = None
    registry_file: Path | None = None
    thinking_level: str | None = None
    timeout: float = 3000.0
    home: Path | None = None
    keep_home: bool = False
    keep_kodo_dir: bool = False
    result_path: Path | None = None
    log_level: str = "INFO"


class HeadlessRun:
    """Runs one prompt through kodo with no user, and reports the result."""

    __options: HeadlessOptions
    __sink: EventSink
    __stream_deltas: bool
    __stop_requested: asyncio.Event

    def __init__(
        self, options: HeadlessOptions, sink: EventSink, *, stream_deltas: bool = False
    ) -> None:
        """Bind the run's options and output.

        Args:
            options (HeadlessOptions): The run's parameters.
            sink (EventSink): Where every event is written.
            stream_deltas (bool): Emit raw thinking/text deltas.
        """
        self.__options = options
        self.__sink = sink
        self.__stream_deltas = stream_deltas
        self.__stop_requested = asyncio.Event()

    def request_stop(self) -> None:
        """Ask the run to stop (signal handlers call this)."""
        self.__stop_requested.set()

    async def run(self) -> RunResult:
        """Execute the run; never raises.

        Returns:
            RunResult: The outcome and everything measured.
        """
        opts = self.__options
        started = time.monotonic()
        cwd = opts.cwd.resolve()
        kodo_dir_existed = (cwd / ".kodo").exists()
        temporary_home = opts.home is None
        home = (
            opts.home.resolve()
            if opts.home is not None
            else Path(tempfile.mkdtemp(prefix="kodo-headless-"))
        )
        llama: subprocess.Popen[bytes] | None = None
        llama_port = 0
        server: ServerProcess | None = None
        client: HeadlessClient | None = None
        outcome = RunOutcome.COMPLETED
        error: str | None = None
        final_phase = ""

        self.__sink.emit(
            "run.start",
            agent=opts.agent,
            model=opts.model,
            cwd=str(cwd),
            llama_url=opts.llama_url,
            home=str(home),
        )
        try:
            build_headless_home(
                home,
                model=opts.model,
                template_kodo_dir=Path.home() / ".kodo",
                registry_file=opts.registry_file,
            )
            url = opts.llama_url
            if url is None:
                llama_port = opts.llama_port or _free_port()
                llama = self.__spawn_llama(llama_port)
                url = await self.__wait_llama(llama, llama_port)

            server = ServerProcess(
                home,
                log_level=opts.log_level,
                console_log=home / "server-console.log",
                extra_args=("--headless-sandbox", str(cwd), "--llama-url", url),
            )
            await server.start(timeout=_SERVER_START_TIMEOUT)
            client = HeadlessClient(
                server.ws_url, self.__sink, cwd, stream_deltas=self.__stream_deltas
            )
            await client.connect()
            await self.__setup_session(client, cwd)
            outcome, final_phase = await self.__drive(client)
            if outcome is RunOutcome.TIMEOUT:
                error = f"The turn did not finish within {opts.timeout:.0f}s"
            elif outcome is RunOutcome.STOPPED:
                error = "Stopped by a signal before the turn finished"
            elif outcome is RunOutcome.COMPLETED and client.turn_error is not None:
                outcome = RunOutcome.RUNTIME_ERROR
                error = client.turn_error
        except _StartupError as exc:
            outcome, error = RunOutcome.STARTUP_ERROR, str(exc)
        except (ServerStartError, RequestError) as exc:
            outcome, error = RunOutcome.STARTUP_ERROR, str(exc)
        except ConnectionError as exc:
            outcome, error = RunOutcome.CRASHED, str(exc)
        except Exception as exc:  # noqa: BLE001 - the run must always report
            _log.exception("headless run crashed")
            outcome, error = RunOutcome.CRASHED, f"{type(exc).__name__}: {exc}"
        finally:
            if client is not None:
                await client.close()
            if server is not None:
                await server.stop()
            if llama is not None:
                self.__stop_llama(llama, llama_port)
            if not kodo_dir_existed and not opts.keep_kodo_dir:
                shutil.rmtree(cwd / ".kodo", ignore_errors=True)
            if temporary_home and not opts.keep_home:
                shutil.rmtree(home, ignore_errors=True)

        cumulative = client.cumulative if client is not None else {}
        result = RunResult(
            outcome=outcome.value,
            session_id=client.session_id if client is not None else "",
            agent=opts.agent,
            model=opts.model,
            final_phase=final_phase,
            assistant_text=client.assistant_text if client is not None else "",
            cumulative_input_tokens=int(cumulative.get("cumulative_input_tokens", 0)),
            cumulative_input_tokens_uncached=int(
                cumulative.get("cumulative_input_tokens_uncached", 0)
            ),
            cumulative_output_tokens=int(cumulative.get("cumulative_output_tokens", 0)),
            cumulative_usd=float(cumulative.get("cumulative_usd", 0.0)),
            per_model=client.per_model if client is not None else {},
            per_agent=client.per_agent if client is not None else {},
            tool_calls=client.tool_calls if client is not None else 0,
            tool_denials=client.tool_denials if client is not None else 0,
            questions_asked=client.questions_asked if client is not None else 0,
            nudges=client.nudges if client is not None else 0,
            wall_seconds=round(time.monotonic() - started, 3),
            error=error,
        )
        self.__sink.emit("run.result", **result.to_dict())
        if opts.result_path is not None:
            opts.result_path.parent.mkdir(parents=True, exist_ok=True)
            opts.result_path.write_text(result.to_json(), encoding="utf-8")
        self.__sink.write_line(result.summary_line())
        return result

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    async def __setup_session(self, client: HeadlessClient, cwd: Path) -> None:
        opts = self.__options
        await client.hello()
        catalog = await client.request(MSG_TOP_AGENTS_LIST, session_scoped=False)
        raw_agents = catalog.get("agents")
        names = (
            {str(a.get("name")) for a in raw_agents if isinstance(a, dict)}
            if isinstance(raw_agents, list)
            else set()
        )
        if names and opts.agent not in names:
            raise _StartupError(
                f"Unknown top-level agent {opts.agent!r}; available: {', '.join(sorted(names))}"
            )
        await client.request(MSG_AGENT_SET, {"name": opts.agent})
        await client.request(
            MSG_WORKSPACE_FOLDERS, {"physical_root": str(cwd), "folders": {cwd.name: str(cwd)}}
        )
        await client.request(MSG_MODE_SET, {"autonomous": True})
        if opts.thinking_level is not None:
            reply = await client.request(
                MSG_THINKING_LEVEL_SET, {"thinking_level": opts.thinking_level}
            )
            if reply.get("ok") is False:
                raise _StartupError(
                    f"Thinking level {opts.thinking_level!r} is not valid for {opts.model!r}"
                )

    async def __drive(self, client: HeadlessClient) -> tuple[RunOutcome, str]:
        client.begin_turn()
        await client.request(MSG_PROMPT_SUBMIT, {"text": self.__options.prompt})
        turn = asyncio.create_task(client.wait_turn_end(timeout=self.__options.timeout))
        stop = asyncio.create_task(self.__stop_requested.wait())
        try:
            done, _ = await asyncio.wait({turn, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if turn in done:
            try:
                return RunOutcome.COMPLETED, turn.result()
            except TimeoutError:
                outcome = RunOutcome.TIMEOUT
        else:
            turn.cancel()
            outcome = RunOutcome.STOPPED
        await client.stop()
        with contextlib.suppress(TimeoutError, ConnectionError):
            await client.wait_turn_end(timeout=_STOP_SETTLE_TIMEOUT)
        return outcome, client.phase

    # ------------------------------------------------------------------
    # Spawned llama-server
    # ------------------------------------------------------------------

    def __spawn_llama(self, port: int) -> subprocess.Popen[bytes]:
        log_dir = Path.home() / ".kodo" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / f"llama-server-{port}-headless.log", "wb")  # noqa: SIM115
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "kodo.llamaserver",
                    "start",
                    "--model",
                    self.__options.model,
                    "--port",
                    str(port),
                    "--foreground",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        finally:
            log.close()
        self.__sink.emit("llama.spawn", model=self.__options.model, port=port, pid=proc.pid)
        return proc

    async def __wait_llama(self, proc: subprocess.Popen[bytes], port: int) -> str:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + _LLAMA_READY_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise _StartupError(
                    f"kodo-llama-server exited with code {proc.returncode}; see "
                    f"~/.kodo/logs/llama-server-{port}-headless.log"
                )
            if self.__stop_requested.is_set():
                raise _StartupError("Stopped while llama-server was starting")
            if await asyncio.to_thread(_healthy, url):
                self.__sink.emit("llama.ready", url=url)
                return url
            await asyncio.sleep(0.5)
        raise _StartupError(f"llama-server on port {port} did not become ready in time")

    def __stop_llama(self, proc: subprocess.Popen[bytes], port: int) -> None:
        # Through the CLI, not a signal: on Windows terminating the supervisor
        # is TerminateProcess, which would orphan its llama-server.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [sys.executable, "-m", "kodo.llamaserver", "stop", "--port", str(port)],
                capture_output=True,
                timeout=60,
                check=False,
            )
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class _StartupError(RuntimeError):
    """The run could not get as far as submitting the prompt."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=1.0) as resp:
            return int(resp.status) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def install_signal_handlers(run: HeadlessRun) -> None:
    """Route SIGINT/SIGTERM to :meth:`HeadlessRun.request_stop`.

    Args:
        run (HeadlessRun): The run to stop.
    """
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, run.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: no loop signal handlers; Ctrl+C still arrives here
            # (SIGTERM there is TerminateProcess and cannot be handled).
            signal.signal(signum, lambda _s, _f: loop.call_soon_threadsafe(run.request_stop))
