import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from execution_engine import (  # noqa: E402
    ExecutionEngine, ExecutionEngineError, ScriptedMockExecutor,
)
from execution_protocol import CancellationToken  # noqa: E402
from retry_policy import RetryPolicy, RetryPolicyError  # noqa: E402


def policy(**updates):
    config = json.loads(
        (ROOT / "config" / "retry_policy_v1.json").read_text(encoding="utf-8")
    )
    config.update(updates)
    return RetryPolicy(config)


def policy_v2(**updates):
    config = json.loads(
        (ROOT / "config" / "retry_policy_v2.json").read_text(encoding="utf-8")
    )
    config.update(updates)
    return RetryPolicy(config)


def run(results, *, retry_policy=None, fallbacks=("48", "50"), logger=None):
    engine = ExecutionEngine(
        ScriptedMockExecutor(results), retry_policy or policy(),
        attempt_logger=logger,
    )
    return engine.execute(
        request=type("Request", (), {"request_id": "001"})(),
        decision={
            "request_id": "001", "recommended_candidate": "19",
            "fallback_order": list(fallbacks),
        },
        candidates={
            "19": {"candidate_id": "19"},
            "48": {"candidate_id": "48"},
            "50": {"candidate_id": "50"},
        },
    )


def test_retry_decision_and_fallback_order():
    trace = run({
        "19": {"status": "failed", "error": "timeout"},
        "48": {"status": "success"},
    })
    assert trace["fallback_trace"] == ["19", "48"]
    assert trace["final_channel"] == "48"
    assert trace["fallback_executed"] is True


def test_max_attempts_respected():
    trace = run({
        "19": {"status": "failed", "error": "timeout"},
        "48": {"status": "failed", "error": "503"},
        "50": {"status": "success"},
    })
    assert len(trace["attempts"]) == 2
    assert trace["final_channel"] is None


def test_non_retryable_error_stops_without_fallback():
    trace = run({
        "19": {"status": "failed", "error": "invalid_parameters"},
        "48": {"status": "success"},
    })
    assert trace["fallback_trace"] == ["19"]
    assert trace["fallback_executed"] is False


def test_attempt_logging_is_complete():
    logged = []
    trace = run({
        "19": {"status": "failed", "error": "429"},
        "48": {"status": "success"},
    }, logger=lambda row: logged.append(dict(row)))
    assert logged == trace["attempts"]
    assert all(
        {"attempt_number", "channel", "result", "error", "network_called"}
        <= row.keys() for row in logged
    )


def test_retry_policy_classification():
    item = policy()
    assert item.should_retry("timeout", 1)
    assert item.should_retry("503", 1)
    assert not item.should_retry("authentication_failed", 1)
    assert not item.should_retry("timeout", 2)
    assert item.automatic_real_execution_authorized is False


def test_retry_policy_rejects_more_than_safe_attempt_ceiling():
    try:
        policy(max_attempts=3)
    except RetryPolicyError as exc:
        assert "between 1 and 2" in str(exc)
    else:
        raise AssertionError("unsafe retry ceiling accepted")


class AdvancingExecutor:
    def __init__(self, clock, advances, results):
        self.clock, self.advances, self.results = clock, advances, results
        self.contexts = []

    def execute_attempt(self, request, candidate, context):
        self.contexts.append(context)
        self.clock[0] += self.advances[len(self.contexts) - 1]
        return {**self.results[len(self.contexts) - 1],
                "is_mock": True, "network_called": False}


def test_attempt_timeout_and_total_deadline_are_enforced():
    clock = [100.0]
    executor = AdvancingExecutor(
        clock, [6.0, 1.0],
        [{"status": "success"}, {"status": "success"}])
    engine = ExecutionEngine(
        executor, policy_v2(per_attempt_timeout_seconds=5,
                            total_deadline_seconds=6.5),
        clock=lambda: clock[0], sleeper=lambda _seconds: None)
    result = engine.execute(
        request=type("Request", (), {"request_id": "deadline"})(),
        decision={"recommended_candidate": "19", "fallback_order": ["48"]},
        candidates={"19": {"candidate_id": "19"},
                    "48": {"candidate_id": "48"}})
    assert result["attempts"][0]["error"] == "attempt_timeout"
    assert result["attempts"][1]["attempt_timeout_seconds"] == .5
    assert result["attempts"][1]["error"] == "attempt_timeout"
    assert result["stopped_reason"] == "non_retryable_error_or_attempt_limit"


def test_retry_after_is_bounded_and_never_sleeps_when_rejected():
    slept = []
    engine = ExecutionEngine(
        ScriptedMockExecutor({
            "19": {"status": "failed", "error": "429",
                   "retry_after_seconds": 30},
            "48": {"status": "success"},
        }), policy(max_retry_after_seconds=2), sleeper=slept.append)
    result = engine.execute(
        request=type("Request", (), {"request_id": "rate"})(),
        decision={"recommended_candidate": "19", "fallback_order": ["48"]},
        candidates={"19": {"candidate_id": "19"},
                    "48": {"candidate_id": "48"}})
    assert len(result["attempts"]) == 1
    assert result["stopped_reason"] == "retry_after_out_of_bounds"
    assert slept == []


def test_output_started_forbids_cross_channel_fallback():
    result = run({
        "19": {"status": "failed", "error": "timeout",
               "first_byte_emitted": True},
        "48": {"status": "success"},
    })
    assert len(result["attempts"]) == 1
    assert result["stopped_reason"] == "output_started_fallback_forbidden"


def test_idempotency_replays_result_and_rejects_scope_conflict():
    calls = []
    engine = ExecutionEngine(
        lambda _request, candidate: calls.append(candidate["candidate_id"]) or {
            "status": "success", "is_mock": True, "network_called": False},
        policy())
    request = type("Request", (), {"request_id": "idem"})()
    decision = {"recommended_candidate": "19", "fallback_order": ["48"]}
    candidates = {"19": {"candidate_id": "19"}, "48": {"candidate_id": "48"}}
    first = engine.execute(request=request, decision=decision,
                           candidates=candidates, idempotency_key="IK-1")
    replay = engine.execute(request=request, decision=decision,
                            candidates=candidates, idempotency_key="IK-1")
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True and calls == ["19"]
    try:
        engine.execute(
            request=request,
            decision={"recommended_candidate": "48", "fallback_order": []},
            candidates=candidates, idempotency_key="IK-1")
    except ExecutionEngineError as exc:
        assert str(exc) == "idempotency_key_scope_conflict"
    else:
        raise AssertionError("idempotency scope conflict accepted")


def test_adapter_timeout_exception_is_safely_classified_and_bounded():
    def timeout_executor(_request, _candidate):
        raise TimeoutError("upstream timeout Authorization: Bearer hidden-secret")

    engine = ExecutionEngine(timeout_executor, policy_v2(), sleeper=lambda _seconds: None)
    result = engine.execute(
        request=type("Request", (), {"request_id": "timeout-exception"})(),
        decision={"recommended_candidate": "19", "fallback_order": ["48"]},
        candidates={"19": {"candidate_id": "19"}, "48": {"candidate_id": "48"}},
    )
    assert len(result["attempts"]) == 2
    first = result["attempts"][0]
    assert first["error"] == "timeout"
    assert first["error_category"] == "upstream_timeout"
    assert first["error_classification"]["classification_source"] == "adapter_explicit_canonical_category"
    assert "hidden-secret" not in str(first)


def test_programming_errors_are_not_hidden_as_transport_failures():
    def broken_executor(_request, _candidate):
        raise TypeError("adapter contract bug")

    engine = ExecutionEngine(broken_executor, policy_v2())
    with pytest.raises(TypeError, match="adapter contract bug"):
        engine.execute(
            request=type("Request", (), {"request_id": "programming-error"})(),
            decision={"recommended_candidate": "19", "fallback_order": []},
            candidates={"19": {"candidate_id": "19"}},
        )
