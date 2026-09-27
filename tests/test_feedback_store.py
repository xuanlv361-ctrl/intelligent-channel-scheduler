import json
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import feedback_store as store  # noqa: E402


def feedback(feedback_id="FB-T-001", **updates):
    value = {
        "feedback_id": feedback_id,
        "request_id": "REQ-T-001",
        "task_type": "coding",
        "selected_model": "deepseek-reasoner",
        "selected_channel": "MOCK-DEEPSEEK-001",
        "success": True,
        "latency_ms": 800,
        "cost": 0.002,
        "user_rating": 5,
        "timestamp": "2026-07-24T14:00:00+08:00",
    }
    value.update(updates)
    return value


def test_feedback_recording_and_query():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "feedback.json"
        store.record_feedback(feedback(), path=path)
        rows = store.query_feedback(path=path, model_id="deepseek-reasoner")
        assert rows == [feedback()]
        assert path.read_bytes().startswith(b"{")
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_duplicate_and_invalid_feedback_rejected():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "feedback.json"
        store.record_feedback(feedback(), path=path)
        with pytest.raises(store.FeedbackValidationError, match="already"):
            store.record_feedback(feedback(), path=path)
        with pytest.raises(store.FeedbackValidationError, match="\\[1,5\\]"):
            store.record_feedback(feedback("FB-T-002", user_rating=6), path=path)


def test_aggregation_and_empty_handling():
    rows = [
        feedback("A", success=True, user_rating=5, latency_ms=800),
        feedback("B", success=False, user_rating=3, latency_ms=1000),
    ]
    aggregate = store.aggregate_feedback(rows)
    assert aggregate[0]["sample_count"] == 2
    assert aggregate[0]["success_rate"] == 0.5
    assert aggregate[0]["average_rating"] == 4
    assert aggregate[0]["average_latency"] == 900
    assert store.aggregate_feedback([]) == []


def test_query_filters_are_combined():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "feedback.json"
        store.record_feedback(feedback("A"), path=path)
        store.record_feedback(feedback(
            "B", task_type="writing", selected_model="qwen",
            selected_channel="MOCK-QWEN-001", success=False,
            timestamp="2026-07-24T14:01:00+08:00",
        ), path=path)
        assert len(store.query_feedback(path=path, task_type="writing", success=False)) == 1
        assert store.query_feedback(path=path, task_type="writing", success=True) == []
