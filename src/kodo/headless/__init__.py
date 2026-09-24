"""Headless kodo — one prompt, no user, sandboxed: the ``kodo-headless`` command.

Runs a single autonomous turn of a top-level agent against one working
directory and reports everything on stdout (doc/HEADLESS.md). The server it
spawns judges every tool call with the sandbox security posture — mutation
confined to the working directory, git read-only — and does local inference
only by URL, against a ``kodo-llama-server`` it spawns or one the caller
names (e.g. on the host, when kodo runs in a Harbor task container).

A pure client: imports ``kodo.common``, ``kodo.transport`` and the public
API of ``kodo.validator`` only. The server and llama-server are separate
processes spawned by module name, never imported, so protocol drift breaks
this loudly (the same discipline as ``kodo.validator``).
"""

from ._cli import main
from ._client import HEADLESS_ANSWERED_REQUESTS, HeadlessClient, RequestError
from ._events import OUTPUT_FORMATS, EventSink
from ._home import build_headless_home
from ._result import RESULT_SCHEMA_VERSION, RunOutcome, RunResult
from ._run import HeadlessOptions, HeadlessRun, install_signal_handlers

__all__ = [
    "HEADLESS_ANSWERED_REQUESTS",
    "OUTPUT_FORMATS",
    "RESULT_SCHEMA_VERSION",
    "EventSink",
    "HeadlessClient",
    "HeadlessOptions",
    "HeadlessRun",
    "RequestError",
    "RunOutcome",
    "RunResult",
    "build_headless_home",
    "install_signal_handlers",
    "main",
]
