"""Portable third-party util manager (uv, ripgrep, fd) under ``~/.kodo/bin/``.

See :mod:`kodo.binutils._utils` for the manifest convention shared with the VS
Code extension and the install/lookup API.  These are called **utils** (not
"tools") to avoid colliding with the agent-facing tool catalog in
:mod:`kodo.toolspecs`.
"""

from ._utils import (
    UTIL_SPECS,
    UtilInstall,
    UtilSpec,
    ensure_all_utils,
    ensure_util,
    find_util,
)

__all__ = [
    "UTIL_SPECS",
    "UtilInstall",
    "UtilSpec",
    "ensure_all_utils",
    "ensure_util",
    "find_util",
]
