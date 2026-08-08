"""Launch args kodo owns, which a user-defined profile may never set.

Two groups, both stripped from every profile write by
:func:`strip_reserved_llama_args` (see
:func:`~kodo.llms.local_registry._profiles.add_profile`/
:func:`~kodo.llms.local_registry._profiles.update_profile`):

- **Server management** — which GGUF, which address, where the log goes.
  Decided per launch by :class:`~kodo.llms.llamacpp.LlamaServerConfig` from
  the registry entry and the settings, so a profile-supplied copy would
  either be silently overridden or break process management outright (kodo
  finds and adopts a surviving llama-server by the port it expects).
- **Per-session reasoning budget**
  (:data:`~kodo.llms.local_registry.RESERVED_REASONING_CAP_ARGS`) — set per
  session by the Thinking Level control. A profile that pinned it would
  silently defeat that control, with no indication in the UI that it had.

Lives in its own module (rather than beside the argument catalog in
:mod:`kodo.llms._arg_catalog`) so that both this package's profile CRUD and
that catalog can share one definition without an import cycle: the catalog
imports it through this package's public surface, and nothing here imports
the catalog.
"""

from __future__ import annotations

import logging

from ._thinking import RESERVED_REASONING_CAP_ARGS

__all__ = [
    "RESERVED_LLAMA_ARGS",
    "strip_reserved_llama_args",
]

_log = logging.getLogger(__name__)

#: Flags :class:`~kodo.llms.llamacpp.LlamaServerConfig` sets itself on every
#: launch. ``-m`` is listed alongside ``--model`` because it is the one short
#: alias a user is genuinely likely to paste from a llama.cpp command line.
_SERVER_MANAGED_ARGS: tuple[str, ...] = (
    "--model",
    "-m",
    "--host",
    "--port",
    "--alias",
    "--log-file",
    "--log-timestamps",
)

#: Every flag a user-defined profile may not carry — see the module docstring.
RESERVED_LLAMA_ARGS: frozenset[str] = frozenset(_SERVER_MANAGED_ARGS + RESERVED_REASONING_CAP_ARGS)


def strip_reserved_llama_args(llama_args: dict[str, str]) -> dict[str, str]:
    """Drop every :data:`RESERVED_LLAMA_ARGS` key from *llama_args*, logging what went.

    Silent-by-default from the user's point of view (the profile editor never
    offers these flags, so reaching this with a non-empty drop list means
    either a pasted command line or a stale client), but always logged so the
    reason a pasted ``--port`` had no effect is discoverable.

    Returns:
        dict[str, str]: A new dict; the input is not mutated.
    """
    stripped = {k: v for k, v in llama_args.items() if k not in RESERVED_LLAMA_ARGS}
    if len(stripped) != len(llama_args):
        dropped = sorted(set(llama_args) - set(stripped))
        _log.warning(
            "Dropped reserved llama-server arg(s) %s from profile llama_args — these are set "
            "by kodo per launch, not by a profile",
            dropped,
        )
    return stripped
