"""Managed Skill bindings backed exclusively by existing persisted services."""

from __future__ import annotations

import statistics
import uuid
from collections import Counter
from typing import Any, Callable, Mapping


class FormalAgentSkillBusinessExecutor:
    def __init__(self, *, call_logs: Any, config_review: Any, circuit_breakers: Any,
                 traffic_governance: Any,
                 validate_uat: Callable[[dict[str, Any]], dict[str, Any]],
                 execute_uat: Callable[[dict[str, Any]], dict[str, Any]],
                 instruction_executor: Callable[..., dict[str, Any]] | None = None):
        self.call_logs = call_logs
        self.config_review = config_review
        self.circuit_breakers = circuit_breakers
        self.traffic_governance = traffic_governance
        self.validate_uat = validate_uat
        self.execute_uat = execute_uat
        self.instruction_executor = instruction_executor

    def __call__(self, *, skill_id: str, arguments: dict[str, Any],
                 principal: Any = None, skill: Mapping[str, Any] | None = None) -> dict[str, Any]:
        handler = getattr(self, skill_id.replace("-", "_"), None)
        if handler is None:
            if arguments.get("invocation_mode") == "lifecycle_validation":
                return {"status": "conditions_validated", "business_executed": False,
                        "execution_mode": "lifecycle_validation",
                        "message": "Skill调用条件验证通过，尚未执行业务任务。",
                        "execution_steps": ["版本已发布并安装", "Skill已启用", "权限与输入Schema校验通过"],
                        "network_called": False, "write_performed": False,
                        "data_source": "persisted_skill_version"}
            task = str(arguments.get("task") or "").strip()
            if not task:
                raise ValueError("instruction_skill_task_required")
            if self.instruction_executor is None:
                raise ValueError("execution_engine_not_configured")
            result = self.instruction_executor(
                skill_id=skill_id, skill=dict(skill or {}), arguments=arguments,
                principal=principal)
            if not isinstance(result, Mapping) or not str(
                    result.get("content") or result.get("result_summary") or "").strip():
                raise ValueError("instruction_skill_empty_result")
            return dict(result)
        return handler(arguments, principal)

    @staticmethod
    def _real_result(data: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        return {"status": "success", "data_source": "persisted_real_data",
                "network_called": False, "write_performed": False,
                **dict(data), **extra}

    def explain_routing_decision(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        decision = self.call_logs.routing_decision(str(args["decision_id"]))
        if decision is None:
            return {"status": "not_found", "data_source": "routing_decision_logs",
                    "network_called": False, "write_performed": False}
        if not args.get("include_metrics", True):
            decision.pop("metric_snapshot", None)
        if not args.get("include_execution", True):
            decision.pop("execution_result", None)
        return self._real_result(decision)

    def inspect_channel_health(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        rows = self.call_logs.channel_health_rows(str(args.get("environment_id") or "china_uat"))
        channel = str(args.get("channel_id") or "").strip()
        if channel:
            rows = [row for row in rows if str(row.get("channel_id")) == channel]
        if not rows:
            return {"status": "no_real_data", "data_source": "realtime_execution",
                    "network_called": False, "write_performed": False,
                    "message": "当前没有具备权威渠道证据的真实UAT日志。"}
        latencies = [float(row["total_latency_ms"]) for row in rows
                     if row.get("total_latency_ms") is not None]
        def percentile(fraction: float) -> float | None:
            if not latencies: return None
            values = sorted(latencies); position = (len(values)-1)*fraction
            lo = int(position); hi = min(lo+1, len(values)-1)
            return round(values[lo] + (values[hi]-values[lo])*(position-lo), 3)
        success = sum(row.get("request_status") == "SUCCESS" for row in rows)
        errors = [row for row in rows if row.get("error_category")]
        return self._real_result({
            "environment_id": args.get("environment_id") or "china_uat",
            "channel_id": channel or None, "request_count": len(rows),
            "success_rate": round(success/len(rows), 6),
            "p50_latency_ms": percentile(.5), "p95_latency_ms": percentile(.95),
            "p99_latency_ms": percentile(.99),
            "timeout_rate": round(sum(row.get("error_category") == "timeout" for row in rows)/len(rows), 6),
            "rate_limit_rate": round(sum(row.get("error_category") == "rate_limit" for row in rows)/len(rows), 6),
            "recent_errors": errors[-10:], "sample_count": len(rows),
            "confidence": "high" if len(rows) >= 30 else "medium" if len(rows) >= 10 else "low",
        })

    def analyze_model_cost(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        data = self.call_logs.analytics(
            environment_id=str(args.get("environment_id") or "china_uat"),
            model=str(args.get("model_id") or "") or None,
            occurred_from=args.get("occurred_from"), occurred_to=args.get("occurred_to"))
        if not data.get("request_count"):
            return {"status": "no_real_data", "data_source": "standardized_call_logs",
                    "network_called": False, "write_performed": False}
        return self._real_result({
            "call_count": data["request_count"], "input_tokens": data["input_tokens"],
            "output_tokens": data["output_tokens"], "cached_tokens": data["cached_input_tokens"],
            "total_tokens": data["input_tokens"] + data["output_tokens"],
            "total_cost": data["total_cost"], "average_cost": data["average_cost"],
            "currency": data["currency"], "price_version": "mixed_persisted_evidence",
            "calculation_basis": "去重后的统一历史与实时调用日志",
            "usage_missing_count": data["request_count"]-data["measurement_coverage"]["tokens"],
            "model_counts": data["model_counts"], "model_costs": data["model_costs"],
        })

    def classify_provider_error(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        request_id = str(args.get("request_id") or "").strip()
        error_id = str(args.get("error_id") or "").strip()
        if not request_id and not error_id:
            raise ValueError("request_id_or_error_id_required")
        rows = self.call_logs.list_records(limit=1000).get("items", [])
        row = next((item for item in rows if
                    (request_id and item.get("request_id") == request_id) or
                    (error_id and (item.get("record_id") == error_id or item.get("error_code") == error_id))), None)
        if row is None:
            return {"status": "not_found", "network_called": False,
                    "write_performed": False, "data_source": "standardized_call_logs"}
        category = row.get("error_category") or ("none" if row.get("request_status") == "SUCCESS" else "unknown")
        retryable = bool(row.get("retryable"))
        return self._real_result({
            "request_id": row.get("request_id"), "http_status": row.get("http_status"),
            "error_code": row.get("error_code"), "error_category": category,
            "error_owner": "provider" if category in {"channel_auth","rate_limit","timeout","upstream_5xx","protocol"} else "request",
            "retryable": retryable, "actually_retried": int(row.get("total_attempts") or 1) > 1,
            "fallback": int(row.get("total_attempts") or 1) > 1,
            "basis": "统一调用日志中的HTTP状态、错误分类和尝试记录",
        })

    def collect_acceptance_evidence(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        analytics = self.call_logs.analytics(environment_id=str(args.get("environment_id") or "china_uat"))
        decisions = self.call_logs.list_routing_decisions(limit=500)
        if not analytics.get("request_count"):
            return {"status": "no_real_data", "network_called": False,
                    "write_performed": False, "data_source": "standardized_call_logs"}
        return self._real_result({
            "acceptance_suite_id": args["acceptance_suite_id"],
            "request_count": analytics["request_count"],
            "reconstructable_decisions": decisions["total"],
            "decision_reconstruction_rate": round(decisions["total"]/analytics["request_count"], 6),
            "error_categories": analytics["error_categories"],
            "real_strategy_effect": "insufficient_evidence",
            "evidence_links": [{"request_id": item.get("request_id"),
                                "decision_id": item.get("decision_id")}
                               for item in decisions["items"][:50]],
            "missing_evidence": ["真人陌生用户交接"] if not args.get("human_handoff_id") else [],
        })

    def validate_uat_request(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        result = self.validate_uat(args)
        return self._real_result(result, data_source="uat_environment_and_catalog")

    def execute_uat_request(self, args: dict[str, Any], _principal: Any) -> dict[str, Any]:
        result = self.execute_uat(args)
        return {"status": "success" if result.get("execution_status") == "success" else "failed",
                "data_source": "live_domestic_uat", "network_called": True,
                "write_performed": False, **result}

    def update_routing_policy(self, args: dict[str, Any], principal: Any) -> dict[str, Any]:
        operator = getattr(principal, "principal_id", None) or "local-console-operator"
        if args.get("action") == "rollback":
            result = self.config_review.rollback(target_version=str(args["target_version"]),
                active_version=str(args["expected_version"]), created_by=operator,
                change_reason=str(args["reason"]))
        else:
            result = self.config_review.create_version(base_version=str(args["expected_version"]),
                patch=dict(args["changes"]), created_by=operator,
                change_reason=str(args["reason"]))
        return self._real_result(result, write_performed=True)

    def operate_circuit_breaker(self, args: dict[str, Any], principal: Any) -> dict[str, Any]:
        circuit_id = str(args["circuit_id"]); action = str(args["action"])
        before = self.circuit_breakers.get_state(circuit_id, principal=principal)
        event = f"SKILL-{uuid.uuid4()}"
        if action == "open":
            result = before
            for index in range(int(self.circuit_breakers.policy["failure_threshold"])):
                result = self.circuit_breakers.record_failure(
                    circuit_id, f"{event}-{index}", principal=principal)
        elif action == "run_probe":
            result = self.circuit_breakers.before_request(
                circuit_id, f"REQ-{uuid.uuid4()}", principal=principal)
        elif action in {"recover", "force_close"}:
            lease = str(args.get("probe_lease_id") or "")
            if not lease:
                raise ValueError("probe_lease_id_required")
            result = self.circuit_breakers.record_success(
                circuit_id, event, lease, principal=principal)
        elif action == "half_open":
            current = self.circuit_breakers.get_state(circuit_id, principal=principal)
            if current["state"] != "HALF_OPEN":
                raise ValueError("circuit_transition_not_permitted_before_cooldown")
            result = current
        else:
            raise ValueError("circuit_action_invalid")
        return self._real_result({"before": before, "after": result,
            "history": self.circuit_breakers.list_transition_audits(circuit_id, principal=principal)},
            write_performed=True)

    def propose_traffic_switch(self, args: dict[str, Any], principal: Any) -> dict[str, Any]:
        result = self.traffic_governance.propose_policy_switch(
            environment_id=str(args["environment"]),
            source_policy_version=str(args["source_policy_version"]),
            target_policy_version=str(args["target_policy_version"]),
            reason=str(args["reason"]), rollback_condition=str(args["rollback_condition"]),
            proposer_id=getattr(principal, "principal_id", None) or "local-console-operator",
            requested_percentage=float(args["requested_percentage"]), principal=principal)
        return self._real_result({**result, "production_change_allowed": False},
                                 write_performed=True)
