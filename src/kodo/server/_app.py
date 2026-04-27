"""aiohttp application factory, route handlers, and server entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from aiohttp import web

from ._orchestrator import Orchestrator, run_mode
from ._response import err, ok, status_resp

PORT = 8042
DEFAULT_MAX_WORKFLOWS: int = os.cpu_count() or 4
DEFAULT_DECISION_TIMEOUT: float = 300.0


#
# Public route handlers
#


async def _status(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    return ok(
        data={
            "run_mode": run_mode(),
            "python_version": sys.version,
            "active_workflows": orch.active_count,
            "max_workflows": request.app["max_workflows"],
        },
        message="Server running",
    )


async def _list_workflows(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    records = orch.list_all()
    data = [
        {
            "id": r.id,
            "state": r.state,
            "parent_id": r.parent_id,
            "is_child": r.is_child,
            "children": r.children,
            "started_at": r.started_at.isoformat(),
        }
        for r in records
    ]
    return ok(data, message=f"{len(data)} workflow(s)")


async def _create_workflow(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    q = request.rel_url.query
    intake = q.get("intake")
    module = q.get("module")

    if not intake or not module:
        return err("Required query parameters: intake (file path), module (dotted path)")

    try:
        record = await orch.create_workflow(module=module, intake_path=intake)
    except RuntimeError as exc:
        return status_resp("error", str(exc))
    except Exception as exc:
        return err(f"Failed to create workflow: {exc}", exc)

    return ok({"id": record.id, "state": record.state}, message="Workflow created")


async def _create_child_workflow(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    parent_id = request.match_info["id"]
    q = request.rel_url.query
    intake = q.get("intake")
    module = q.get("module")

    if not intake or not module:
        return err("Required query parameters: intake, module")

    try:
        record = await orch.create_workflow(module=module, intake_path=intake, parent_id=parent_id)
    except (RuntimeError, ValueError) as exc:
        return status_resp("error", str(exc))
    except Exception as exc:
        return err(f"Failed to create child workflow: {exc}", exc)

    return ok(
        {"id": record.id, "state": record.state, "parent_id": parent_id},
        message="Child workflow created",
    )


async def _cancel_workflow(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    wid = request.match_info["id"]

    try:
        await orch.cancel(wid)
    except KeyError:
        return status_resp("error", f"Workflow {wid!r} not found")
    except Exception as exc:
        return err(f"Cancel failed: {exc}", exc)

    return ok(message=f"Workflow {wid} cancelled")


async def _poll_decision(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    wid = request.match_info["id"]

    record = orch.get(wid)
    if record is None:
        return status_resp("error", f"Workflow {wid!r} not found")

    decision = record.decision
    if decision is None:
        return status_resp("pending", "No decision pending")

    if decision.status == "pending":
        return ok(
            data={"prompt": decision.prompt, "options": decision.options},
            message="Decision awaiting input",
        )
    if decision.status == "answered":
        return ok(data={"answer": decision.answer}, message="Decision answered")
    if decision.status == "timeout":
        return status_resp("timeout", "Decision timed out; default was applied")
    if decision.status == "cancelled":
        return status_resp("cancelled", "Workflow was cancelled")

    return status_resp("pending", "Decision status unknown")


async def _submit_decision(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    wid = request.match_info["id"]
    q = request.rel_url.query
    choice = q.get("choice", "")
    message = q.get("message")

    valid = ("accepted", "rejected", "feedback")
    if choice not in valid:
        return err(f"choice must be one of: {', '.join(valid)}")
    if choice == "feedback" and not message:
        return err("message is required when choice=feedback")

    try:
        await orch.submit_decision(wid, choice, message)
    except KeyError:
        return status_resp("error", f"Workflow {wid!r} not found")
    except ValueError as exc:
        return status_resp("error", str(exc))

    return ok(message="Decision submitted")


async def _graceful_shutdown(orch: Orchestrator) -> None:
    while orch.active_count > 0:
        await asyncio.sleep(0.5)
    asyncio.get_event_loop().stop()


async def _shutdown(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    graceful = request.rel_url.query.get("graceful") == "1"

    if graceful:
        asyncio.create_task(_graceful_shutdown(orch))
        return ok(message="Server shutting down gracefully; waiting for active workflows to finish")

    await orch.cancel_all()
    asyncio.get_event_loop().call_soon(asyncio.get_event_loop().stop)
    return ok(message="Server shutting down; all workflows cancelled")


#
# Internal route handlers — worker subprocess → orchestrator IPC only
#


async def _internal_register_decision(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    wid = request.match_info["id"]
    q = request.rel_url.query
    prompt = q.get("prompt", "")
    default = q.get("default", "")
    try:
        options: list[str] = json.loads(q.get("options", "[]"))
    except json.JSONDecodeError:
        options = []

    try:
        await orch.register_decision(wid, prompt, options, default)
    except KeyError:
        return status_resp("error", f"Workflow {wid!r} not found")

    return ok(message="Decision registered")


async def _internal_decision_answer(request: web.Request) -> web.Response:
    orch: Orchestrator = request.app["orchestrator"]
    wid = request.match_info["id"]
    result = orch.get_decision_answer(wid)
    return web.json_response(
        {
            "status": result["status"],
            "message": result["status"],
            "data": result.get("data"),
        }
    )


#
# App factory and entry point
#


def create_app(
    max_workflows: int = DEFAULT_MAX_WORKFLOWS,
    decision_timeout: float = DEFAULT_DECISION_TIMEOUT,
) -> web.Application:
    """Create and configure the aiohttp application.

    Args:
        max_workflows (int): Maximum number of concurrently active workflows.
        decision_timeout (float): Seconds before a pending decision is auto-resolved.

    Returns:
        web.Application: Configured aiohttp application ready to serve.
    """
    app = web.Application()
    base_url = f"http://127.0.0.1:{PORT}"
    app["orchestrator"] = Orchestrator(
        max_workflows=max_workflows,
        decision_timeout=decision_timeout,
        base_url=base_url,
    )
    app["max_workflows"] = max_workflows

    app.router.add_get("/status", _status)
    app.router.add_get("/workflows/list", _list_workflows)
    app.router.add_get("/workflows/create", _create_workflow)
    app.router.add_get("/workflows/{id}/create", _create_child_workflow)
    app.router.add_get("/workflows/{id}/cancel", _cancel_workflow)
    app.router.add_get("/workflows/{id}/decision", _poll_decision)
    app.router.add_get("/workflows/{id}/decision/submit", _submit_decision)
    app.router.add_get("/shutdown", _shutdown)
    app.router.add_get("/internal/workflows/{id}/decision/register", _internal_register_decision)
    app.router.add_get("/internal/workflows/{id}/decision/answer", _internal_decision_answer)

    return app
