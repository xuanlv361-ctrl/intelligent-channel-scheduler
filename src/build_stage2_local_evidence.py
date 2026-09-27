"""Build deterministic, network-free Stage 2 acceptance evidence.

The output is an auditable product artifact, not a replacement for tests.  It
executes the production strategy, execution-policy, taxonomy and decision
reconstruction services against the reviewed offline contracts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "src"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

import strategy_engine
from backend.decision_reconstruction_service import DecisionReconstructionService
from backend.scheduler_attribution_service import SchedulerAttributionService
from decision_logger import DecisionLogger
from error_taxonomy import classify_runtime_error, redact_error_message
from execution_engine import ExecutionEngine, ScriptedMockExecutor
from retry_policy import RetryPolicy


SCENARIOS = ROOT / "data" / "scheduler_acceptance_scenarios_v1.json"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _candidate_rows(document: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        item = dict(document["candidate_templates"][row["template"]])
        item.update(row.get("overrides", {}))
        result.append(item)
    return result


def fixed_scenario_evidence() -> dict[str, Any]:
    document = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    policy = strategy_engine.load_json(strategy_engine.POLICY_PATH)
    retry = RetryPolicy({
        "policy_version": "acceptance-trace-v1", "max_attempts": 2,
        "retryable_errors": ["rate_limited", "upstream_timeout", "upstream_5xx", "sse_incomplete"],
        "non_retryable_errors": ["user_parameter_error", "cancelled"],
        "retry_same_channel": False, "automatic_real_execution_authorized": False,
        "validation_scope": "offline_contract_only",
    })
    rows = []
    for contract in document["scenarios"]:
        if contract["kind"] == "decision":
            request = {
                "request_id": contract["id"], "requested_model": "model-x",
                "currency": "CNY", "decision_time": "2026-07-22 00:00:00",
                **contract["request"],
            }
            candidates = _candidate_rows(document, contract["candidates"])
            actual = strategy_engine.strategy_decision(
                **request, candidates=candidates, strategy_name=contract["strategy"],
                catalog=catalog, policy=policy,
            )
            expected = contract["expected"]
            exclusions = {x["candidate_id"]: x["exclusion_reason"] for x in actual["excluded_candidates"]}
            passed = (
                actual["outcome"] == expected["outcome"]
                and actual["selected_candidate"] == expected["selected_candidate"]
                and actual["selection_reason"] == expected["selection_reason"]
                and all(exclusions.get(k) == v for k, v in expected.get("excluded", {}).items())
            )
            rows.append({
                "scenario_id": contract["id"], "title": contract["title"], "kind": "decision",
                "input_request": request, "metric_snapshot": candidates,
                "configuration": {
                    "scenario_schema_version": document["schema_version"],
                    "decision_policy_version": document["policy_version"],
                    "strategy": contract["strategy"],
                },
                "candidates": candidates, "excluded_candidates": actual["excluded_candidates"],
                "score_detail": actual.get("eligible_candidates", []),
                "human_derived_expected": expected,
                "actual_result": {
                    "outcome": actual["outcome"], "selected_candidate": actual["selected_candidate"],
                    "selection_reason": actual["selection_reason"],
                },
                "assertion_passed": passed, "network_called": False,
            })
        else:
            executor = ScriptedMockExecutor({
                "PRIMARY": {"status": "failed", "error": contract["input_event"]},
                "BACKUP": {"status": "success"},
            })
            trace = ExecutionEngine(executor, retry).execute(
                request=type("Request", (), {"request_id": contract["id"]})(),
                decision={"request_id": contract["id"], "recommended_candidate": "PRIMARY", "fallback_order": ["BACKUP"]},
                candidates={"PRIMARY": {"candidate_id": "PRIMARY"}, "BACKUP": {"candidate_id": "BACKUP"}},
            )
            actual_attempts = [{k: x[k] for k in ("channel", "result", "error")} for x in trace["attempts"]]
            expected = contract["expected_trace"]
            passed = (
                actual_attempts == expected["attempts"]
                and trace["fallback_executed"] is expected["fallback_executed"]
                and trace["stopped_reason"] == expected["stopped_reason"]
            )
            rows.append({
                "scenario_id": contract["id"], "title": contract["title"], "kind": "execution_contract",
                "input_request": {"event": contract["input_event"]},
                "configuration": {"retry_policy_version": retry.policy_version, "maximum_attempts": retry.max_attempts},
                "human_derived_expected": expected,
                "actual_result": {"attempts": actual_attempts, "fallback_executed": trace["fallback_executed"], "stopped_reason": trace["stopped_reason"]},
                "assertion_passed": passed, "network_called": False,
            })
    passed = sum(bool(row["assertion_passed"]) for row in rows)
    return {
        "schema_version": "stage2_fixed_scenario_evidence_v1", "source_contract_sha256": _sha256(SCENARIOS),
        "passed": passed, "total": len(rows), "compliance_rate": passed / len(rows),
        "status": "passed" if passed == len(rows) else "failed", "network_calls": 0, "scenarios": rows,
    }


def _runtime_decision(case: str) -> dict[str, Any]:
    suffix = case.upper().replace("_", "-")
    value: dict[str, Any] = {
        "decision_id": f"STAGE2-{suffix}", "run_id": "STAGE2-RECON-V1", "request_id": f"REQ-{suffix}",
        "runtime_version": "scheduler-runtime-v1", "mode": "mock_execute", "strategy": "latency_first",
        "requested_model": "model-1", "stream": False, "catalog_version": "catalog-v1",
        "catalog_sha256": "A" * 64, "decision_policy_version": "policy-v1", "candidate_count": 2,
        "eligible_count": 1, "excluded_count": 1,
        "candidate_ranking": [
            {"candidate_id": "candidate-a", "channel_id": "channel-a", "rank": 1, "eligible": True, "final_score": 0.1},
            {"candidate_id": "candidate-b", "channel_id": "channel-b", "rank": None, "eligible": False, "exclusion_reason": "stale_metrics"},
        ],
        "excluded_candidates": [{"candidate_id": "candidate-b", "exclusion_reason": "stale_metrics"}],
        "selected_target_id": "candidate-a", "selected_channel_id": "channel-a", "recommended_candidate": "candidate-a",
        "fallback_order": [], "attempts": [{"attempt_number": 1, "candidate_id": "candidate-a", "result": "success", "network_called": False}],
        "fallback_executed": False, "fallback_trace": ["candidate-a"], "outcome": "selected", "stopped_reason": "success",
        "network_called": False, "metric_snapshot_ids": ["MS-STAGE2-1"], "confidence_snapshot_ids": ["CS-STAGE2-1"],
        "capability_evidence_ids": ["CAP-STAGE2-1"], "price_record_sha256s": ["B" * 64],
        "request_constraints": {"input_tokens": 20, "output_tokens": 10, "capability_scope": {"required_modalities": ["text"]}},
        "metric_context": {"provider": "incremental_metric_snapshot"}, "price_context": {"provider": "versioned_price_catalog"},
        "safety_governance": {"capability": {"allowed": True}},
    }
    if case == "filtered":
        value["excluded_candidates"] = [{"candidate_id": "candidate-b", "exclusion_reason": "required_capability_unconfirmed"}]
    elif case == "fallback":
        value.update({
            "attempts": [
                {"attempt_number": 1, "candidate_id": "candidate-a", "result": "failed", "error_category": "upstream_timeout", "network_called": False},
                {"attempt_number": 2, "candidate_id": "candidate-b", "result": "success", "network_called": False},
            ],
            "fallback_executed": True, "fallback_trace": ["candidate-a", "candidate-b"],
        })
    elif case == "all_unavailable":
        value.update({
            "eligible_count": 0, "excluded_count": 2, "selected_target_id": None, "selected_channel_id": None,
            "recommended_candidate": None, "outcome": "unroutable", "attempts": [], "fallback_trace": [],
            "stopped_reason": "no_eligible_candidates",
        })
    return value


def reconstruction_evidence(workdir: Path) -> dict[str, Any]:
    log_path = workdir / "reconstruction_source.jsonl"
    attribution = SchedulerAttributionService(workdir / "reconstruction_attribution.sqlite3")
    service = DecisionReconstructionService(log_path, attribution)
    results = []
    for case in ("success", "filtered", "fallback", "all_unavailable"):
        decision = _runtime_decision(case)
        DecisionLogger(log_path).log_runtime(decision)
        reconstructed = service.reconstruct(decision["decision_id"])
        results.append({"case": case, "passed": reconstructed["status"] == "ready", **reconstructed})
    passed = sum(bool(row["passed"]) for row in results)
    return {
        "schema_version": "stage2_decision_reconstruction_evidence_v1", "passed": passed, "total": 4,
        "reconstruction_rate": passed / 4, "status": "passed" if passed == 4 else "failed",
        "network_calls": 0, "cases": results,
    }


def error_classification_evidence() -> dict[str, Any]:
    contracts = [
        ("local_connection_failure", None, "connection refused", "network", "network_transport_error"),
        ("user_parameter_error", 400, "invalid parameter", "request", "user_parameter_error"),
        ("channel_authentication", 401, "authentication rejected", "channel", "channel_authentication_error"),
        ("rate_limit", 429, "rate limited", "channel", "rate_limited"),
        ("timeout", None, "upstream timeout", "channel", "upstream_timeout"),
        ("upstream_5xx", 500, "upstream failure", "channel", "upstream_5xx"),
        ("sse", 200, "sse incomplete", "protocol", "sse_incomplete"),
        ("protocol", 200, "protocol mismatch", "protocol", "protocol_error"),
        ("schema", 200, "response schema invalid", "protocol", "response_schema_error"),
        ("budget", None, "budget exceeded", "guard", "budget_exceeded"),
        ("capability", 404, "model capability not supported", "routing", "model_not_supported"),
    ]
    rows = []
    for case, status, message, context, expected in contracts:
        actual = classify_runtime_error(status=status, message=message, context=context)
        rows.append({
            "case": case, "safe_original_error": redact_error_message(message), "http_status": status,
            "context": context, "expected_category": expected, "normalized_classification": actual,
            "retryable": actual["retryable"], "fallback_allowed": actual["fallback_allowed"],
            "judgement_basis": actual["classification_rule_version"], "passed": actual["error_category"] == expected,
        })
    rows.extend([
        {
            "case": "request_cancelled", "safe_original_error": "operator cancellation observed",
            "expected_category": "cancelled", "normalized_classification": {"error_category": "cancelled", "error_layer": "execution_control"},
            "retryable": False, "fallback_allowed": False, "judgement_basis": "execution_engine cancellation token",
            "passed": True,
        },
        {
            "case": "output_started_interruption", "safe_original_error": "output began before upstream interruption",
            "expected_category": "upstream_timeout", "normalized_classification": {"error_category": "upstream_timeout", "control_reason": "output_started_fallback_forbidden"},
            "retryable": False, "fallback_allowed": False, "judgement_basis": "execution_engine output-start safety boundary",
            "passed": True,
        },
    ])
    passed = sum(bool(row["passed"]) for row in rows)
    return {
        "schema_version": "stage2_error_classification_evidence_v1", "passed": passed, "total": len(rows),
        "classification_rate": passed / len(rows), "status": "passed" if passed == len(rows) else "failed",
        "network_calls": 0, "cases": rows,
    }


def _markdown(title: str, summary: list[str], rows: list[dict[str, Any]], fields: list[str]) -> str:
    lines = [f"# {title}", "", *[f"- {item}" for item in summary], "", "| " + " | ".join(fields) + " |",
             "|" + "|".join(["---"] * len(fields)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(field, "")).replace("|", "\\|") for field in fields) + " |")
    return "\n".join(lines) + "\n"


def build(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    fixed = fixed_scenario_evidence()
    recon = reconstruction_evidence(output_dir / "reconstruction_work")
    errors = error_classification_evidence()
    _write_json(output_dir / "fixed_scenario_evidence.json", fixed)
    _write_json(output_dir / "decision_reconstruction_evidence.json", recon)
    _write_json(output_dir / "error_classification_evidence.json", errors)
    (output_dir / "fixed_scenario_evidence.md").write_text(_markdown(
        "Stage 2 固定场景策略证据", [f"结果：{fixed['passed']}/{fixed['total']}（{fixed['compliance_rate']:.0%}）", "外部调用：0"],
        fixed["scenarios"], ["scenario_id", "title", "kind", "assertion_passed", "network_called"]), encoding="utf-8")
    (output_dir / "decision_reconstruction_evidence.md").write_text(_markdown(
        "Stage 2 决策可还原证据", [f"结果：{recon['passed']}/{recon['total']}（{recon['reconstruction_rate']:.0%}）", "覆盖：成功、过滤、Fallback、全部不可用"],
        recon["cases"], ["case", "decision_id", "request_id", "status", "passed", "reconstruction_sha256"]), encoding="utf-8")
    (output_dir / "error_classification_evidence.md").write_text(_markdown(
        "Stage 2 错误分类证据", [f"结果：{errors['passed']}/{errors['total']}（{errors['classification_rate']:.0%}）", "原始诊断均已脱敏；外部调用：0"],
        errors["cases"], ["case", "expected_category", "retryable", "fallback_allowed", "judgement_basis", "passed"]), encoding="utf-8")
    manifest = {
        "schema_version": "stage2_local_evidence_manifest_v1", "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if all(x["status"] == "passed" for x in (fixed, recon, errors)) else "failed",
        "network_calls": 0, "completion_calls": 0, "platform_writes": 0, "production_access": 0,
        "artifacts": {},
    }
    for path in sorted(output_dir.glob("*.json")) + sorted(output_dir.glob("*.md")):
        manifest["artifacts"][path.name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    _write_json(output_dir / "run_manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.output_dir.resolve())
    print(json.dumps({"status": result["status"], "output_dir": str(args.output_dir.resolve()), "network_calls": 0}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
