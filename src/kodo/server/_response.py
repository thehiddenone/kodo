"""HTTP response envelope helpers for the orchestrator REST API."""

from __future__ import annotations

import traceback as tb
from typing import Any, Optional

from aiohttp import web


def ok(data: Any = None, message: str = "OK") -> web.Response:
    """Build a successful ``status: ok`` response.

    Args:
        data (Any): Optional payload to include under ``data``.
        message (str): Human-readable summary.

    Returns:
        web.Response: JSON response with ``status: ok``.
    """
    body: dict[str, Any] = {"status": "ok", "message": message}
    if data is not None:
        body["data"] = data
    return web.json_response(body)


def err(
    message: str,
    exc: Optional[BaseException] = None,
    *,
    debug: bool = False,
) -> web.Response:
    """Build an ``status: error`` response.

    Args:
        message (str): Human-readable error summary.
        exc (BaseException | None): Exception to include in the ``error`` field.
        debug (bool): When ``True``, include a traceback in the response.

    Returns:
        web.Response: JSON response with ``status: error``.
    """
    body: dict[str, Any] = {"status": "error", "message": message}
    if exc is not None:
        e: dict[str, str] = {"type": type(exc).__name__, "message": str(exc)}
        if debug:
            e["traceback"] = tb.format_exc()
        body["error"] = e
    return web.json_response(body)


def status_resp(status: str, message: str, data: Any = None) -> web.Response:
    """Build a response with an arbitrary status value.

    Args:
        status (str): Status string (e.g. ``pending``, ``timeout``, ``cancelled``).
        message (str): Human-readable summary.
        data (Any): Optional payload.

    Returns:
        web.Response: JSON response envelope.
    """
    body: dict[str, Any] = {"status": status, "message": message}
    if data is not None:
        body["data"] = data
    return web.json_response(body)
