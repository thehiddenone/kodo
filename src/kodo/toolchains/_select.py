"""Select the active toolchain from the Tech Stack artifact.

The Tech Stack document names the product's primary programming language (see
Narrative Author's Tech Stack structure). This module maps that declaration to a
concrete :class:`~kodo.toolchains._interface.ToolchainPlugin`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ._interface import ToolchainPlugin
from .node._plugin import NodePlugin
from .python._plugin import PythonPlugin

__all__ = ["select_toolchain"]

_log = logging.getLogger(__name__)

_NODE_KEYWORDS = ("typescript", "javascript", "node")
_PYTHON_KEYWORDS = ("python",)


def select_toolchain(tech_stack_content: str, project_root: Path) -> ToolchainPlugin:
    """Return the toolchain implied by a Tech Stack document.

    Matches the **Primary programming language** entry when present (falling
    back to the whole document), then maps language keywords to a plugin.
    Defaults to Python when nothing matches.

    Args:
        tech_stack_content (str): Full text of the accepted Tech Stack artifact.
        project_root (Path): Root directory of the Kodo project.

    Returns:
        ToolchainPlugin: The toolchain for the declared language.
    """
    target = tech_stack_content
    for line in tech_stack_content.splitlines():
        if "primary programming language" in line.lower():
            target = line
            break

    text = target.lower()
    if any(kw in text for kw in _NODE_KEYWORDS):
        return NodePlugin(project_root)
    if any(kw in text for kw in _PYTHON_KEYWORDS):
        return PythonPlugin(project_root)

    _log.warning("Tech Stack names no recognized language; defaulting to Python.")
    return PythonPlugin(project_root)
