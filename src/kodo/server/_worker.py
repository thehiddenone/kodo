"""Subprocess entry point executed inside each worker process."""

from __future__ import annotations

import importlib
from pathlib import Path


def worker_main(
    module_path: str,
    intake_path: str,
    workflow_id: str,
    workdir: str,
    orchestrator_url: str,
) -> None:
    """Import the workflow module and execute it.

    This function runs inside a fresh subprocess spawned by the orchestrator.
    It is deliberately free of async machinery; the workflow itself is synchronous.

    Args:
        module_path (str): Dotted import path of the workflow module (e.g. ``kodo.claude``).
        intake_path (str): Filesystem path to the plain-text intake file.
        workflow_id (str): Runtime UUID assigned by the orchestrator.
        workdir (str): Path to the isolated working directory.
        orchestrator_url (str): Base URL of the orchestrator server for IPC calls.
    """
    from kodo.workflow import WorkflowContext

    ctx = WorkflowContext(
        workflow_id=workflow_id,
        workdir=Path(workdir),
        orchestrator_url=orchestrator_url,
        intake_path=intake_path,
    )

    from kodo.workflow import Workflow

    mod = importlib.import_module(module_path)

    candidates = [
        obj
        for obj in mod.__dict__.values()
        if isinstance(obj, type) and issubclass(obj, Workflow) and obj is not Workflow
    ]

    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        raise ValueError(
            f"Workflow module {module_path!r} defines more than one Workflow subclass "
            f"({names}). A workflow module must contain exactly one."
        )
    if not candidates:
        raise ValueError(f"Workflow module {module_path!r} contains no Workflow subclass.")

    instance = candidates[0]()
    try:
        instance.setup(ctx)
        instance.run()
    finally:
        instance.teardown()
