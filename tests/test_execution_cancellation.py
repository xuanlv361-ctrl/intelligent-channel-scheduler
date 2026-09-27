import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution_engine import ExecutionEngine  # noqa: E402
from execution_protocol import CancellationToken  # noqa: E402
from retry_policy import RetryPolicy  # noqa: E402


def policy():
    return RetryPolicy(json.loads(
        (ROOT / "config" / "retry_policy_v2.json").read_text(encoding="utf-8")))


class CancellingExecutor:
    def __init__(self):
        self.calls = 0

    def execute_attempt(self, _request, _candidate, context):
        self.calls += 1
        context.cancellation_token.cancel("client_disconnected")
        context.raise_if_cancelled()
        raise AssertionError("unreachable")


def test_cancellation_stops_fallback_and_releases_once():
    executor = CancellingExecutor()
    released = []
    token = CancellationToken()
    engine = ExecutionEngine(executor, policy())
    result = engine.execute(
        request=type("Request", (), {"request_id": "cancel"})(),
        decision={"recommended_candidate": "19", "fallback_order": ["48"]},
        candidates={"19": {"candidate_id": "19"},
                    "48": {"candidate_id": "48"}},
        cancellation_token=token,
        release_callback=lambda summary: released.append(dict(summary)))
    assert result["final_status"] == "cancelled"
    assert result["stopped_reason"] == "cancelled"
    assert len(result["attempts"]) == executor.calls == 1
    assert released == [{
        "request_id": "cancel", "idempotency_key": None,
        "attempt_count": 1, "final_status": "cancelled",
        "stopped_reason": "cancelled",
    }]


def test_pre_cancelled_request_does_not_invoke_executor():
    token = CancellationToken()
    assert token.cancel("user_cancelled") is True
    assert token.cancel("duplicate") is False
    called = []
    result = ExecutionEngine(
        lambda *_args: called.append(True), policy()).execute(
            request=type("Request", (), {"request_id": "pre"})(),
            decision={"recommended_candidate": "19", "fallback_order": []},
            candidates={"19": {"candidate_id": "19"}},
            cancellation_token=token)
    assert called == [] and result["attempts"] == []
    assert result["final_status"] == "cancelled"


def test_cancellation_callbacks_are_idempotent():
    token = CancellationToken()
    observed = []
    token.add_callback(observed.append)
    token.cancel("disconnect")
    token.add_callback(observed.append)
    token.cancel("again")
    assert observed == ["disconnect", "disconnect"]
