"""Workflow ABC and execution context for Kōdo workflows."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path


class WorkflowContext:
    """Execution context passed to every workflow instance.

    Provides identity, filesystem access, and the decision primitive
    for requesting human input during a workflow run.
    """

    __id: str
    __workdir: Path
    __url: str
    __intake_path: str

    def __init__(
        self,
        workflow_id: str,
        workdir: Path,
        orchestrator_url: str,
        intake_path: str,
    ) -> None:
        """Initialise the context.

        Args:
            workflow_id (str): Runtime UUID assigned by the orchestrator.
            workdir (Path): Isolated working directory for this workflow.
            orchestrator_url (str): Base URL of the orchestrator server.
            intake_path (str): Path to the plain-text intake file.
        """
        self.__id = workflow_id
        self.__workdir = workdir
        self.__url = orchestrator_url
        self.__intake_path = intake_path

    @property
    def id(self) -> str:
        """Runtime UUID of this workflow."""
        return self.__id

    @property
    def workdir(self) -> Path:
        """Isolated working directory created by the orchestrator."""
        return self.__workdir

    @property
    def intake_path(self) -> str:
        """Filesystem path to the intake file that drove this workflow."""
        return self.__intake_path

    @property
    def intake(self) -> str:
        """Contents of the intake file."""
        return Path(self.__intake_path).read_text(encoding="utf-8")

    def decision(self, prompt: str, options: list[str], default: str) -> str:
        """Block until a human answers, the decision timeout fires, or the workflow is cancelled.

        Registers the pending decision with the orchestrator, then polls
        until a definitive answer is available.  In unattended / batched
        builds always supply a sensible ``default`` — the orchestrator will
        apply it automatically when the timeout elapses.

        Args:
            prompt (str): Question to present to the user.
            options (list[str]): Accepted response values.
            default (str): Value applied automatically on timeout.

        Returns:
            str: The chosen option.

        Raises:
            InterruptedError: If the workflow is cancelled while waiting.
        """
        params = urllib.parse.urlencode({
            "prompt": prompt,
            "options": json.dumps(options),
            "default": default,
        })
        reg_url = f"{self.__url}/internal/workflows/{self.__id}/decision/register?{params}"
        urllib.request.urlopen(reg_url, timeout=10).read()

        poll_url = f"{self.__url}/internal/workflows/{self.__id}/decision/answer"
        while True:
            with urllib.request.urlopen(poll_url, timeout=10) as resp:
                data = json.loads(resp.read())
            status = data["status"]
            if status == "answered":
                return data["data"]["answer"]
            if status == "cancelled":
                raise InterruptedError("Workflow cancelled while waiting for decision")
            time.sleep(1.0)


class Workflow(ABC):
    """Abstract base class for all Kōdo workflow implementations.

    A workflow module must export a module-level variable named ``workflow``
    pointing to a concrete subclass of this class.  The orchestrator will
    instantiate it and call :meth:`run`.
    """

    @abstractmethod
    def run(self, ctx: WorkflowContext) -> None:
        """Execute the workflow.

        On success write a ``SUCCESS`` marker to ``ctx.workdir``.  If this
        file is absent when ``run`` returns (or if ``run`` raises), the
        orchestrator treats the run as failed and discards the workdir.

        Args:
            ctx (WorkflowContext): Execution context for this run.
        """
