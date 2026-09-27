from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import strategy_engine  # noqa: E402
from execution_engine import ExecutionEngine, ScriptedMockExecutor  # noqa: E402
from retry_policy import RetryPolicy  # noqa: E402


SCENARIOS = ROOT / "data" / "scheduler_acceptance_scenarios_v1.json"


def load_contract():
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def candidates(document, rows):
    result = []
    for row in rows:
        value = dict(document["candidate_templates"][row["template"]])
        value.update(row.get("overrides", {}))
        result.append(value)
    return result


def test_thirteen_scenarios_cover_mandatory_boundaries():
    document = load_contract()
    rows = document["scenarios"]
    assert len(rows) >= 12
    titles = " ".join(row["title"] for row in rows)
    for required in ("正常", "同分", "无可用", "限流", "超时", "5xx", "不可重试", "fallback", "SSE", "取消"):
        assert required.casefold() in titles.casefold()
    assert document["network_called"] is False


def test_every_decision_matches_human_worked_answer_exactly():
    document = load_contract()
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    policy = strategy_engine.load_json(strategy_engine.POLICY_PATH)
    checked = 0
    for row in document["scenarios"]:
        if row["kind"] != "decision":
            continue
        request = {
            "request_id": row["id"], "requested_model": "model-x",
            "currency": "CNY", "decision_time": "2026-07-22 00:00:00",
            **row["request"],
        }
        result = strategy_engine.strategy_decision(
            **request, candidates=candidates(document, row["candidates"]),
            strategy_name=row["strategy"], catalog=catalog, policy=policy,
        )
        assert result["outcome"] == row["expected"]["outcome"], row["id"]
        assert result["selected_candidate"] == row["expected"]["selected_candidate"], row["id"]
        assert result["selection_reason"] == row["expected"]["selection_reason"], row["id"]
        excluded = {item["candidate_id"]: item["exclusion_reason"]
                    for item in result["excluded_candidates"]}
        assert all(excluded.get(key) == value
                   for key, value in row["expected"].get("excluded", {}).items()), row["id"]
        checked += 1
    assert checked == 7


def test_execution_contracts_match_bounded_offline_engine():
    document = load_contract()
    retryable = {"rate_limited", "upstream_timeout", "upstream_5xx", "sse_incomplete"}
    policy = RetryPolicy({
        "policy_version": "acceptance-trace-v1", "max_attempts": 2,
        "retryable_errors": sorted(retryable),
        "non_retryable_errors": ["user_parameter_error", "cancelled"],
        "retry_same_channel": False, "automatic_real_execution_authorized": False,
        "validation_scope": "offline_contract_only",
    })
    checked = 0
    for row in document["scenarios"]:
        if row["kind"] != "execution_contract":
            continue
        executor = ScriptedMockExecutor({
            "PRIMARY": {"status": "failed", "error": row["input_event"]},
            "BACKUP": {"status": "success"},
        })
        trace = ExecutionEngine(executor, policy).execute(
            request=type("Request", (), {"request_id": row["id"]})(),
            decision={"request_id": row["id"], "recommended_candidate": "PRIMARY",
                      "fallback_order": ["BACKUP"]},
            candidates={"PRIMARY": {"candidate_id": "PRIMARY"},
                        "BACKUP": {"candidate_id": "BACKUP"}},
        )
        actual_attempts = [{key: attempt[key] for key in ("channel", "result", "error")}
                           for attempt in trace["attempts"]]
        expected = row["expected_trace"]
        assert actual_attempts == expected["attempts"], row["id"]
        assert trace["fallback_executed"] is expected["fallback_executed"], row["id"]
        assert trace["stopped_reason"] == expected["stopped_reason"], row["id"]
        assert trace["network_called"] is False
        checked += 1
    assert checked == 6
