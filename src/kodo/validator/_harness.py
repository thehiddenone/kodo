"""High-level facade: one isolated kodo run, driven end to end.

:class:`ValidationHarness` composes the pieces of this package into the
lifecycle a validation needs:

1. clone a template ``~/.kodo`` into the run directory (:mod:`._home`);
2. start the real server subprocess against it (:mod:`._server`);
3. connect and handshake as the pseudo-extension (:mod:`._client`);
4. push the simulated workspace (:mod:`._workspace`);
5. apply modes, submit prompts, and let the :class:`~kodo.validator._user.
   UserSimulator` answer every interactive gate;
6. leave a complete :class:`~kodo.validator._transcript.Transcript` behind
   for the (phase-2) evaluator.

Everything lives under a single ``run_dir``::

    <run_dir>/home/.kodo         isolated kodo home (bin/, llama.cpp/, titler/ symlinked)
    <run_dir>/workspace/<root>/  simulated VS Code workspace folders
    <run_dir>/transcript.jsonl   every frame + interaction, in order
    <run_dir>/home/server-console.log  the server subprocess's stdout/stderr
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Literal, cast

from kodo.transport import (
    MSG_COMMAND_CONTROL_SET,
    MSG_EDIT_CONTROL_SET,
    MSG_MODE_SET,
    MSG_PROJECT_SET,
    MSG_PROMPT_SUBMIT,
    MSG_STOP,
    MSG_WORKFLOW_SET,
    MSG_WORKSPACE_FOLDERS,
)

from ._client import ValidatorClient
from ._home import clone_kodo_home
from ._models import ensure_local_llms_installed
from ._server import ServerProcess
from ._transcript import Transcript, TranscriptEntry
from ._user import ScriptedUser, UserSimulator
from ._workspace import SimulatedWorkspace

__all__ = ["Modes", "TurnResult", "ValidationHarness"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Modes:
    """The four session toggles a validation run pins before prompting.

    Attributes:
        autonomous: Autonomous (True) vs Interactive (False).
        workflow: ``guided`` (Guide pipeline) or ``problem_solving``.
        edit_control: Edit Control posture.
        command_control: Command Control (security) posture.
    """

    autonomous: bool = False
    workflow: Literal["guided", "problem_solving"] = "problem_solving"
    edit_control: Literal["review_all", "allow_all", "smart"] = "smart"
    command_control: Literal["defensive", "permissive", "smart"] = "smart"


@dataclass(frozen=True)
class TurnResult:
    """Everything one prompt produced, sliced out of the transcript.

    Attributes:
        prompt: The submitted prompt text.
        final_phase: Resting phase the turn ended on.
        assistant_text: Concatenated streamed assistant output.
        tool_calls: Dispatched tool calls (prep merged with detail).
        interactions: Simulated user interactions during the turn.
        errors: ``error`` event payloads during the turn.
        entries: Every transcript entry recorded during the turn.
    """

    prompt: str
    final_phase: str
    assistant_text: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    interactions: list[TranscriptEntry] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)
    entries: list[TranscriptEntry] = field(default_factory=list)


class ValidationHarness:
    """One isolated, fully-driven kodo session for automated validation.

    Args:
        run_dir: Directory owning every artifact of this run.
        llm_under_test: Local registry name of the LLM this run actually
            exercises. Pinned as the run's active model (``mode: "local"``,
            ``models.local``) before the server starts, and downloaded first
            if missing.
        validation_llm: Local registry name of the fixed, capable model
            reserved for the (not-yet-built) Phase 2 evaluator. Not invoked
            by anything in phase 1 — only ensured present/downloaded, so it's
            ready when the evaluator lands.
        template_home: Existing ``.kodo`` to clone (``bin/``, ``llama.cpp/``,
            and ``titler/`` symlinked, per-run state skipped, rest copied).
            None starts from an empty home with server defaults.
        user: Interactive-request policy; a default :class:`ScriptedUser`
            (first option / approve / allow / env API keys) when omitted.
        settings_overrides: Keys deep-merged into the cloned
            ``etc/settings.json`` before the server starts. The
            ``llm_under_test`` pin (``mode``/``models.local``) is applied on
            top of these, so it always wins on conflict.
        server_log_level: ``--log-level`` for the server subprocess.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        llm_under_test: str,
        validation_llm: str,
        template_home: Path | None = None,
        user: UserSimulator | None = None,
        settings_overrides: dict[str, object] | None = None,
        server_log_level: str = "INFO",
    ) -> None:
        self.__run_dir = run_dir.resolve()
        self.__run_dir.mkdir(parents=True, exist_ok=True)
        self.__llm_under_test = llm_under_test
        self.__validation_llm = validation_llm
        self.__template_home = template_home
        self.__settings_overrides = settings_overrides
        self.__server_log_level = server_log_level

        self.__workspace = SimulatedWorkspace(self.__run_dir / "workspace")
        self.__transcript = Transcript(self.__run_dir / "transcript.jsonl")
        self.__user: UserSimulator = user if user is not None else ScriptedUser()
        self.__server: ServerProcess | None = None
        self.__client: ValidatorClient | None = None

    @property
    def run_dir(self) -> Path:
        """The run's artifact directory."""
        return self.__run_dir

    @property
    def llm_under_test(self) -> str:
        """Local registry name of the model this run exercises."""
        return self.__llm_under_test

    @property
    def validation_llm(self) -> str:
        """Local registry name of the fixed model reserved for Phase 2."""
        return self.__validation_llm

    @property
    def workspace(self) -> SimulatedWorkspace:
        """The simulated workspace; add roots before (or after) :meth:`start`."""
        return self.__workspace

    @property
    def transcript(self) -> Transcript:
        """The run transcript (grows for the lifetime of the harness)."""
        return self.__transcript

    @property
    def client(self) -> ValidatorClient:
        """The live protocol client (available after :meth:`start`).

        Raises:
            RuntimeError: If the harness has not been started.
        """
        if self.__client is None:
            raise RuntimeError("Harness not started")
        return self.__client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, session_id: str | None = None) -> None:
        """Clone the home, start the server, connect, and sync the workspace.

        Args:
            session_id (str | None): Session to resume; a fresh one when None.
        """
        home_dir = self.__run_dir / "home"
        kodo_dir = clone_kodo_home(
            home_dir,
            self.__template_home,
            settings_overrides=self.__pin_llm_under_test(self.__settings_overrides),
        )
        self.__server = ServerProcess(home_dir, log_level=self.__server_log_level)
        await self.__server.start()

        self.__client = ValidatorClient(self.__server.ws_url, self.__transcript, self.__user)
        await self.__client.connect()
        ack = await self.__client.hello(session_id=session_id)
        self.__transcript.record(
            "note",
            "lifecycle",
            {
                "event": "llms",
                "llm_under_test": self.__llm_under_test,
                "validation_llm": self.__validation_llm,
            },
        )
        await self.__ensure_llms_installed(kodo_dir, ack)
        if self.__workspace.roots:
            await self.sync_workspace()
        _log.info("Validation run ready: %s (session %s)", self.__run_dir, self.session_id)

    def __pin_llm_under_test(self, overrides: dict[str, object] | None) -> dict[str, object]:
        """Force ``mode``/``models.local`` onto *overrides* for the LLM under test.

        Applied on top of any caller-supplied ``settings_overrides`` so the
        run always actually exercises ``llm_under_test`` (its whole point),
        regardless of what else a scenario pins.

        Args:
            overrides (dict[str, object] | None): Caller-supplied overrides.

        Returns:
            dict[str, object]: *overrides* with the model pin merged on top.
        """
        merged = dict(overrides or {})
        models = dict(cast(dict[str, object], merged.get("models") or {}))
        models["local"] = self.__llm_under_test
        merged["mode"] = "local"
        merged["models"] = models
        return merged

    async def __ensure_llms_installed(self, kodo_dir: Path, hello_ack: dict[str, object]) -> None:
        """Make sure both named LLMs are installed, downloading if needed.

        Args:
            kodo_dir (Path): The run's isolated ``.kodo``.
            hello_ack (dict[str, object]): The ``hello.ack`` payload (carries
                ``local_registry``).
        """
        local_registry = hello_ack.get("local_registry")
        registry = cast(
            list[dict[str, object]], local_registry if isinstance(local_registry, list) else []
        )
        await ensure_local_llms_installed(
            self.client,
            kodo_dir,
            registry,
            (self.__llm_under_test, self.__validation_llm),
        )

    @property
    def session_id(self) -> str | None:
        """The bound session id, once started."""
        return self.__client.session_id if self.__client is not None else None

    async def shutdown(self) -> None:
        """Disconnect, stop the server subprocess, and close the transcript."""
        if self.__client is not None:
            await self.__client.close()
            self.__client = None
        if self.__server is not None:
            await self.__server.stop()
            self.__server = None
        self.__transcript.record("note", "lifecycle", {"event": "shutdown"})
        self.__transcript.close()

    async def __aenter__(self) -> ValidationHarness:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.shutdown()

    # ------------------------------------------------------------------
    # Session configuration
    # ------------------------------------------------------------------

    async def sync_workspace(self) -> None:
        """Push the current simulated workspace shape (``workspace.folders``).

        Call again after adding roots mid-run — exactly like the extension
        re-pushing on ``onDidChangeWorkspaceFolders``.
        """
        await self.client.request(MSG_WORKSPACE_FOLDERS, self.__workspace.folders_payload())

    async def apply_modes(self, modes: Modes) -> None:
        """Set all four session toggles (they apply to the *next* prompt).

        Args:
            modes (Modes): The desired toggle values.
        """
        client = self.client
        await client.request(MSG_MODE_SET, autonomous=modes.autonomous)
        await client.request(MSG_WORKFLOW_SET, mode=modes.workflow)
        await client.request(MSG_EDIT_CONTROL_SET, edit_control=modes.edit_control)
        await client.request(MSG_COMMAND_CONTROL_SET, command_control=modes.command_control)
        self.__transcript.record("note", "lifecycle", {"event": "modes", **vars(modes)})

    async def bind_project(self, root_name: str, *, project_name: str | None = None) -> None:
        """Bind the session's project for Guided mode (``project.set``).

        Args:
            root_name (str): Name of a workspace root added via the workspace.
            project_name (str | None): Display name; defaults to *root_name*.
        """
        root = self.__workspace.root_path(root_name)
        await self.client.request(MSG_PROJECT_SET, root=str(root), name=project_name or root_name)

    # ------------------------------------------------------------------
    # Prompting
    # ------------------------------------------------------------------

    async def submit_prompt(
        self,
        text: str,
        *,
        turn_timeout: float = 900.0,
        settle_seconds: float = 2.0,
    ) -> TurnResult:
        """Submit one prompt and block until its turn finishes.

        Interactive gates raised during the turn are answered by the user
        simulator automatically; everything is recorded.

        Args:
            text (str): The prompt text.
            turn_timeout (float): Seconds to wait for the turn to finish.
            settle_seconds (float): Resting-phase stability window (see
                :meth:`ValidatorClient.wait_turn_end`).

        Returns:
            TurnResult: The turn's slice of the transcript, pre-digested.

        Raises:
            TimeoutError: If the turn does not finish in time.
            ProtocolError: If the server rejects the prompt.
        """
        client = self.client
        start_seq = len(self.__transcript.entries)
        client.begin_turn()
        await client.request(MSG_PROMPT_SUBMIT, text=text)
        final_phase = await client.wait_turn_end(
            timeout=turn_timeout, settle_seconds=settle_seconds
        )
        transcript = self.__transcript
        return TurnResult(
            prompt=text,
            final_phase=final_phase,
            assistant_text=transcript.assistant_text(start=start_seq),
            tool_calls=transcript.tool_calls(start=start_seq),
            interactions=transcript.interactions(start=start_seq),
            errors=transcript.errors(start=start_seq),
            entries=[e for e in transcript.entries if e.seq >= start_seq],
        )

    async def stop_turn(self) -> None:
        """Issue the global STOP for the in-flight turn."""
        await self.client.request(MSG_STOP)
