"""Headless kodo — one prompt, no user, sandboxed: the ``kodo-headless`` command.

Runs a single autonomous turn of a top-level agent against one working
directory and reports everything on stdout (doc/HEADLESS.md). The server it
spawns judges every tool call with the sandbox security posture — mutation
confined to the working directory, git read-only. A local model runs only by
URL, on a ``kodo-llama-server`` it spawns or one the caller names (e.g. on the
host, when kodo runs in a Harbor task container); a cloud model
(``VENDOR/MODEL_ID``) takes its API key from the environment.

A pure client: imports ``kodo.common``, ``kodo.transport`` and the public
API of ``kodo.validator`` only. The server and llama-server are separate
processes spawned by module name, never imported, so protocol drift breaks
this loudly (the same discipline as ``kodo.validator``).
"""

from ._cli import main
from ._client import HEADLESS_ANSWERED_REQUESTS, HeadlessClient, RequestError
from ._credentials import bedrock_region, resolve_vendor_api_key, vendor_credential_env_names
from ._events import OUTPUT_FORMATS, EventSink
from ._home import build_headless_home
from ._model import LOCAL_MODEL_PREFIX, ModelSpec
from ._result import RESULT_SCHEMA_VERSION, RunOutcome, RunResult
from ._run import HeadlessOptions, HeadlessRun, install_signal_handlers

__all__ = [
    "HEADLESS_ANSWERED_REQUESTS",
    "LOCAL_MODEL_PREFIX",
    "OUTPUT_FORMATS",
    "RESULT_SCHEMA_VERSION",
    "EventSink",
    "HeadlessClient",
    "HeadlessOptions",
    "HeadlessRun",
    "ModelSpec",
    "RequestError",
    "RunOutcome",
    "RunResult",
    "bedrock_region",
    "build_headless_home",
    "install_signal_handlers",
    "main",
    "resolve_vendor_api_key",
    "vendor_credential_env_names",
]
