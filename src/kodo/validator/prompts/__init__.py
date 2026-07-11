"""Reusable validation prompt text, loaded by name from ``.md`` files.

Scenarios (``kodo.validator.scenarios``) stay one-``.py``-per-file, but their
task / user-proxy / result-validation prompt *text* lives here so it can be
shared across several LLMs-under-test. Import the package singleton and pull a
prompt by its ``/``-separated name::

    from kodo.validator.prompts import PROMPTS

    PROMPTS.get("tictactoe/detailed_task")
    PROMPTS.get("tictactoe/rvp")

See :mod:`kodo.validator.prompts._registry` for the naming convention.
"""

from ._registry import PROMPTS, PromptNotFoundError, PromptRegistry

__all__ = ["PROMPTS", "PromptNotFoundError", "PromptRegistry"]
