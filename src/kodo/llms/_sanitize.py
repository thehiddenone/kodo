"""Stripping of one-way user-notification tags from LLM-bound context.

The four kodo callout tags (``<kodo_info>``, ``<kodo_warn>``, ``<kodo_crit>``,
``<kodo>``, see ``subagents/preamble_performance.md``) are direct, one-way
messages an agent sends to the human user and are rendered specially by the
WebView. The model has no use for its own past notifications played back to
it as context, so their content is removed from any text built for an
outbound LLM call. Persisted history (session/subsession logs, which the
WebView renders) keeps the tags verbatim — only the wire-format builders
that assemble a request to a model call this.
"""

from __future__ import annotations

import re

__all__ = ["strip_kodo_callouts"]

_CALLOUT_RE = re.compile(r"<(kodo_info|kodo_warn|kodo_crit|kodo)>.*?</\1>", re.DOTALL)


def strip_kodo_callouts(text: str) -> str:
    """Remove kodo callout tags and their content from assistant-authored text."""
    return _CALLOUT_RE.sub("", text)
