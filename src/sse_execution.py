"""Strict, local Server-Sent Events decoding for execution adapters.

This parser is intentionally transport-agnostic and does not enable UAT
streaming.  A successful model stream must terminate with ``data: [DONE]``.
"""

from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any, Iterable

from execution_protocol import CancellationToken, ExecutionCancelled


class SSEProtocolError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: Any
    raw_data: str
    event: str | None = None
    event_id: str | None = None


class SSEDecoder:
    def __init__(self, *, cancellation_token: CancellationToken | None = None,
                 max_stream_bytes: int = 1_048_576,
                 max_event_bytes: int = 262_144) -> None:
        if max_stream_bytes <= 0 or max_event_bytes <= 0:
            raise ValueError("SSE byte limits must be positive")
        self.token = cancellation_token or CancellationToken()
        self.max_stream_bytes = int(max_stream_bytes)
        self.max_event_bytes = int(max_event_bytes)
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._buffer = ""
        self._total_bytes = 0
        self._done = False
        self._first_byte_emitted = False
        self._events: list[SSEEvent] = []

    @property
    def first_byte_emitted(self) -> bool:
        return self._first_byte_emitted

    @property
    def done(self) -> bool:
        return self._done

    @property
    def events(self) -> tuple[SSEEvent, ...]:
        return tuple(self._events)

    def feed(self, chunk: bytes | str) -> list[SSEEvent]:
        self.token.raise_if_cancelled()
        if self._done:
            if chunk not in (b"", ""):
                raise SSEProtocolError("sse_data_after_done")
            return []
        raw = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
        self._total_bytes += len(raw)
        if self._total_bytes > self.max_stream_bytes:
            raise SSEProtocolError("sse_stream_too_large")
        try:
            self._buffer += self._decoder.decode(raw, final=False)
        except UnicodeDecodeError as exc:
            raise SSEProtocolError("sse_invalid_utf8") from exc
        return self._drain(allow_partial=False)

    def _drain(self, *, allow_partial: bool) -> list[SSEEvent]:
        normalized = self._buffer.replace("\r\n", "\n").replace("\r", "\n")
        blocks = normalized.split("\n\n")
        self._buffer = blocks.pop() if not allow_partial else ""
        if allow_partial and blocks == [] and normalized:
            blocks = [normalized]
        emitted: list[SSEEvent] = []
        for block in blocks:
            if not block.strip():
                continue
            if self._done:
                raise SSEProtocolError("sse_data_after_done")
            self.token.raise_if_cancelled()
            if len(block.encode("utf-8")) > self.max_event_bytes:
                raise SSEProtocolError("sse_event_too_large")
            data_lines: list[str] = []
            event = event_id = None
            for line in block.split("\n"):
                if not line or line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data_lines.append(value)
                elif field == "event":
                    event = value
                elif field == "id" and "\x00" not in value:
                    event_id = value
            if not data_lines:
                continue
            raw_data = "\n".join(data_lines)
            if raw_data == "[DONE]":
                self._done = True
                continue
            self._first_byte_emitted = True
            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError as exc:
                raise SSEProtocolError("sse_malformed_json") from exc
            item = SSEEvent(payload, raw_data, event, event_id)
            self._events.append(item)
            emitted.append(item)
        return emitted

    def finish(self) -> dict[str, Any]:
        self.token.raise_if_cancelled()
        try:
            self._buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise SSEProtocolError("sse_invalid_utf8") from exc
        if self._buffer.strip():
            self._drain(allow_partial=True)
        if not self._done:
            raise SSEProtocolError("sse_missing_done")
        return {
            "status": "complete", "done_received": True,
            "event_count": len(self._events),
            "first_byte_emitted": self._first_byte_emitted,
            "events": [item.data for item in self._events],
        }


def parse_sse_stream(chunks: Iterable[bytes | str], *,
                     cancellation_token: CancellationToken | None = None,
                     max_stream_bytes: int = 1_048_576,
                     max_event_bytes: int = 262_144) -> dict[str, Any]:
    decoder = SSEDecoder(
        cancellation_token=cancellation_token,
        max_stream_bytes=max_stream_bytes, max_event_bytes=max_event_bytes)
    try:
        for chunk in chunks:
            decoder.feed(chunk)
        return decoder.finish()
    except ExecutionCancelled:
        raise
