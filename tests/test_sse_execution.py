import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution_protocol import CancellationToken, ExecutionCancelled  # noqa: E402
from sse_execution import SSEDecoder, SSEProtocolError, parse_sse_stream  # noqa: E402


def test_fragmented_sse_requires_done_and_preserves_events():
    result = parse_sse_stream([
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n',
        b'\ndata: {"choices":[{"delta":{"content":"b"}}]}\n\n',
        b'data: [DONE]\n\n',
    ])
    assert result["status"] == "complete"
    assert result["done_received"] is True
    assert result["event_count"] == 2
    assert result["first_byte_emitted"] is True


def test_missing_done_is_incomplete_not_success():
    with pytest.raises(SSEProtocolError, match="sse_missing_done"):
        parse_sse_stream([b'data: {"chunk":1}\n\n'])


def test_malformed_json_and_data_after_done_fail_closed():
    with pytest.raises(SSEProtocolError, match="sse_malformed_json"):
        parse_sse_stream([b"data: not-json\n\ndata: [DONE]\n\n"])
    decoder = SSEDecoder()
    decoder.feed(b"data: [DONE]\n\n")
    with pytest.raises(SSEProtocolError, match="sse_data_after_done"):
        decoder.feed(b'data: {"late":true}\n\n')
    with pytest.raises(SSEProtocolError, match="sse_data_after_done"):
        parse_sse_stream([
            b'data: [DONE]\n\ndata: {"late":true}\n\n'])


def test_cancelled_stream_emits_no_later_events():
    token = CancellationToken()
    decoder = SSEDecoder(cancellation_token=token)
    assert len(decoder.feed(b'data: {"chunk":1}\n\n')) == 1
    token.cancel("client_disconnected")
    with pytest.raises(ExecutionCancelled, match="client_disconnected"):
        decoder.feed(b'data: {"chunk":2}\n\n')
    assert len(decoder.events) == 1


def test_stream_and_event_byte_limits_are_enforced():
    with pytest.raises(SSEProtocolError, match="sse_stream_too_large"):
        SSEDecoder(max_stream_bytes=3).feed(b"four")
    with pytest.raises(SSEProtocolError, match="sse_event_too_large"):
        SSEDecoder(max_event_bytes=4).feed(b"data: 1\n\n")
