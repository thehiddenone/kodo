"""Everything a headless run shows, written to stdout as it happens.

Two renderings of one event stream: ``jsonl`` (the default — one JSON object
per line, for machines) and ``text`` (for people). Every event carries
``ts`` and ``type``; most carry ``agent`` and ``subsession_id`` (``null`` for
the top-level agent). The event types are listed in doc/HEADLESS.md §"stdout".
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import TextIO

__all__ = ["OUTPUT_FORMATS", "EventSink"]

OUTPUT_FORMATS = ("jsonl", "text")

_TEXT_PREVIEW_TYPES = frozenset({"text", "thinking", "text.delta", "thinking.delta"})


class EventSink:
    """Writes headless-run events to a stream, flushed per event."""

    __format: str
    __stream: TextIO

    def __init__(self, output_format: str = "jsonl", stream: TextIO | None = None) -> None:
        """Choose the rendering and destination.

        Args:
            output_format (str): ``"jsonl"`` or ``"text"``.
            stream (TextIO | None): Destination; defaults to ``sys.stdout``.

        Raises:
            ValueError: Unknown *output_format*.
        """
        if output_format not in OUTPUT_FORMATS:
            raise ValueError(f"Unknown output format {output_format!r}")
        self.__format = output_format
        self.__stream = stream if stream is not None else sys.stdout

    def emit(self, event_type: str, **fields: object) -> None:
        """Write one event.

        Args:
            event_type (str): The event's ``type``.
            **fields (object): Event fields (JSON-serializable).
        """
        event: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "type": event_type,
            **fields,
        }
        if self.__format == "jsonl":
            line = json.dumps(event, ensure_ascii=False, default=str)
        else:
            line = self.__render_text(event)
        self.__stream.write(line + "\n")
        self.__stream.flush()

    def write_line(self, line: str) -> None:
        """Write one raw line (the trailing ``KODO-RESULT`` summary).

        Args:
            line (str): The text, without a newline.
        """
        self.__stream.write(line + "\n")
        self.__stream.flush()

    @staticmethod
    def __render_text(event: dict[str, object]) -> str:
        kind = str(event["type"])
        who = event.get("agent") or ""
        scope = f"[{who}] " if who else ""
        if kind in _TEXT_PREVIEW_TYPES:
            label = "thinking" if kind.startswith("thinking") else "assistant"
            return f"{scope}{label}: {event.get('text', '')}"
        if kind == "tool.call":
            document = str(event.get("document") or "").strip()
            return f"{scope}tool {event.get('tool')} ({event.get('tool_call_id')}):\n{document}"
        if kind == "tool.denied":
            return f"{scope}DENIED {event.get('tool')}: {event.get('reason', '')}"
        if kind == "run.result":
            body = {k: v for k, v in event.items() if k not in ("ts", "type")}
            return "result: " + json.dumps(body, indent=2, default=str)
        rest = {k: v for k, v in event.items() if k not in ("ts", "type", "agent")}
        details = " ".join(f"{k}={v}" for k, v in rest.items() if v not in (None, "", {}, []))
        return f"{scope}{kind} {details}".rstrip()
