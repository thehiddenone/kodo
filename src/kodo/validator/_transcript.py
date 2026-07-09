"""Run transcript: every wire frame and simulated interaction, durably logged.

The transcript is the raw material the (phase-2) evaluator scores. It records
**everything** the harness sees or does — received events, streams, requests,
sent commands, simulated user answers, and lifecycle notes — as an in-memory
list plus an append-only JSONL file, in arrival order.

Entry shape (one JSON object per line):

```json
{"seq": 12, "ts": 1751970000.123, "direction": "recv" | "send" | "note",
 "kind": "<frame kind, or 'interaction'/'lifecycle'/'stream_assembled'>",
 "payload": { ... }, "correlation_id": "..." | null}
```
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal, cast

__all__ = ["Transcript", "TranscriptEntry"]

Direction = Literal["recv", "send", "note"]


@dataclass(frozen=True)
class TranscriptEntry:
    """One transcript record.

    Attributes:
        seq: Zero-based position in arrival order.
        ts: Unix timestamp at record time.
        direction: ``recv`` (server→harness), ``send`` (harness→server), or
            ``note`` (harness-internal: interactions, lifecycle, assembled
            streams).
        kind: The wire frame kind, or a note discriminator.
        payload: The frame payload / note body.
        correlation_id: The frame's correlation id, when present.
    """

    seq: int
    ts: float
    direction: Direction
    kind: str
    payload: dict[str, object]
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """This entry as a JSON-serializable dict.

        Returns:
            dict[str, object]: The JSONL line body.
        """
        return {
            "seq": self.seq,
            "ts": self.ts,
            "direction": self.direction,
            "kind": self.kind,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
        }


class Transcript:
    """Ordered, optionally file-backed record of one validation run.

    Args:
        path: JSONL file to append every entry to; in-memory only when omitted.
    """

    __entries: list[TranscriptEntry]
    __file: IO[str] | None

    def __init__(self, path: Path | None = None) -> None:
        self.__entries = []
        self.__file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.__file = open(path, "a", encoding="utf-8")  # noqa: SIM115 - long-lived

    @property
    def entries(self) -> list[TranscriptEntry]:
        """Copy of all entries in arrival order."""
        return list(self.__entries)

    def record(
        self,
        direction: Direction,
        kind: str,
        payload: dict[str, object],
        *,
        correlation_id: str | None = None,
    ) -> TranscriptEntry:
        """Append one entry (and flush it to the JSONL file, if any).

        Args:
            direction (Direction): ``recv`` / ``send`` / ``note``.
            kind (str): Frame kind or note discriminator.
            payload (dict[str, object]): Frame payload / note body.
            correlation_id (str | None): Frame correlation id, when present.

        Returns:
            TranscriptEntry: The recorded entry.
        """
        entry = TranscriptEntry(
            seq=len(self.__entries),
            ts=time.time(),
            direction=direction,
            kind=kind,
            payload=payload,
            correlation_id=correlation_id,
        )
        self.__entries.append(entry)
        if self.__file is not None:
            self.__file.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            self.__file.flush()
        return entry

    def record_interaction(
        self, interaction: str, request: dict[str, object], response: dict[str, object]
    ) -> TranscriptEntry:
        """Record one simulated user interaction (server request + our reply).

        Args:
            interaction (str): The request's payload type (e.g.
                ``prompt.question``, ``prompt.permission``, ``api_key.request``).
            request (dict[str, object]): The server request payload.
            response (dict[str, object]): The payload the simulator answered with.

        Returns:
            TranscriptEntry: The recorded ``interaction`` note.
        """
        return self.record(
            "note",
            "interaction",
            {"interaction": interaction, "request": request, "response": response},
        )

    def close(self) -> None:
        """Close the JSONL file (idempotent)."""
        if self.__file is not None:
            self.__file.close()
            self.__file = None

    # ------------------------------------------------------------------
    # Read-side helpers (used by TurnResult and, later, the evaluator)
    # ------------------------------------------------------------------

    def events(self, event_type: str | None = None, *, start: int = 0) -> list[TranscriptEntry]:
        """Received ``event`` frames, optionally filtered by payload type.

        Args:
            event_type (str | None): Exact ``payload.type`` to match; all when None.
            start (int): Only entries with ``seq >= start``.

        Returns:
            list[TranscriptEntry]: Matching entries in order.
        """
        return [
            e
            for e in self.__entries
            if e.seq >= start
            and e.direction == "recv"
            and e.kind == "event"
            and (event_type is None or e.payload.get("type") == event_type)
        ]

    def interactions(self, *, start: int = 0) -> list[TranscriptEntry]:
        """Simulated user interactions (``interaction`` notes).

        Args:
            start (int): Only entries with ``seq >= start``.

        Returns:
            list[TranscriptEntry]: Matching entries in order.
        """
        return [
            e
            for e in self.__entries
            if e.seq >= start and e.direction == "note" and e.kind == "interaction"
        ]

    def assistant_text(self, *, start: int = 0) -> str:
        """All streamed assistant text, concatenated in stream order.

        Args:
            start (int): Only entries with ``seq >= start``.

        Returns:
            str: The assembled assistant output (excluding thinking).
        """
        parts = [
            str(e.payload.get("text", ""))
            for e in self.__entries
            if e.seq >= start
            and e.direction == "note"
            and e.kind == "stream_assembled"
            and e.payload.get("stream") == "agent.tokens"
        ]
        return "".join(parts)

    def tool_calls(self, *, start: int = 0) -> list[dict[str, object]]:
        """Dispatched tool calls, each merging its prep + detail events.

        Args:
            start (int): Only entries with ``seq >= start``.

        Returns:
            list[dict[str, object]]: One dict per ``agent.tool_call_prep``,
            with the matching ``agent.tool_call_detail`` fields merged in
            under ``detail`` when it arrived.
        """
        details: dict[str, dict[str, object]] = {}
        for e in self.events("agent.tool_call_detail", start=start):
            details[str(e.payload.get("tool_call_id"))] = e.payload
        calls: list[dict[str, object]] = []
        for e in self.events("agent.tool_call_prep", start=start):
            call = dict(e.payload)
            call["detail"] = details.get(str(e.payload.get("tool_call_id")))
            calls.append(call)
        return calls

    def errors(self, *, start: int = 0) -> list[dict[str, object]]:
        """Payloads of every ``error`` event received.

        Args:
            start (int): Only entries with ``seq >= start``.

        Returns:
            list[dict[str, object]]: Error payloads in order.
        """
        return [e.payload for e in self.events("error", start=start)]

    def cumulative_usd(self) -> float | None:
        """Latest reported cumulative cost, if any ``usage.update`` arrived.

        Returns:
            float | None: ``cumulative_usd`` of the last usage event.
        """
        usage = self.events("usage.update")
        if not usage:
            return None
        return cast(float, usage[-1].payload.get("cumulative_usd", 0.0))
