"""Controlled, fail-closed eligibility interface for sticky routing.

The provider composes already-authoritative local gate providers.  It performs
no network access and does not infer an unavailable gate from catalog defaults.
Every gate result is evaluated in the immutable safety order before a sticky
lookup is permitted.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from sticky_routing import GATE_ORDER, GATE_PASS_STATES


GateProvider = Callable[..., Mapping[str, Any] | bool | str]


class ControlledStickyEligibilityProvider:
    def __init__(self, providers: Mapping[str, GateProvider]):
        unknown = sorted(set(providers) - set(GATE_ORDER))
        if unknown:
            raise ValueError(f"unknown sticky eligibility gates: {', '.join(unknown)}")
        self.providers = dict(providers)

    def evaluate(
        self, *, request: Any, candidates: Sequence[Mapping[str, Any]],
        metric_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        evidence_ids: list[str] = []
        metric_snapshot_id = None
        confidence_snapshot_id = None
        candidate_ids = {
            str(row.get("candidate_id") or "") for row in candidates
            if row.get("candidate_id")
        }
        eligible_by_all_gates = set(candidate_ids)
        for gate in GATE_ORDER:
            provider = self.providers.get(gate)
            if provider is None:
                result: Mapping[str, Any] | bool | str = {
                    "state": "unknown", "reason": f"{gate}_provider_unavailable"}
            else:
                try:
                    result = provider(
                        request=request, candidates=candidates,
                        metric_context=metric_context)
                except Exception:
                    result = {
                        "state": "unknown",
                        "reason": f"{gate}_provider_error",
                        "eligible_channel_ids": [],
                    }
            if isinstance(result, Mapping):
                scoped_ids = result.get("eligible_channel_ids")
                if not isinstance(scoped_ids, (list, tuple, set)):
                    result = {
                        **result, "state": "unknown",
                        "reason": f"{gate}_candidate_scope_missing",
                    }
                    scoped = set()
                else:
                    scoped = {str(item) for item in scoped_ids}
                    if not scoped.issubset(candidate_ids):
                        result = {
                            **result, "state": "unknown",
                            "reason": f"{gate}_candidate_scope_invalid",
                        }
                        scoped = set()
                eligible_by_all_gates &= scoped
                safe = {
                    "state": str(result.get("state", "unknown")),
                    "reason": str(result.get("reason") or "") or None,
                    "evidence_id": str(result.get("evidence_id") or "") or None,
                }
                results[gate] = {key: value for key, value in safe.items() if value is not None}
                if safe["evidence_id"] and safe["evidence_id"] not in evidence_ids:
                    evidence_ids.append(safe["evidence_id"])
                if result.get("metric_snapshot_id"):
                    metric_snapshot_id = str(result["metric_snapshot_id"])
                if result.get("confidence_snapshot_id"):
                    confidence_snapshot_id = str(result["confidence_snapshot_id"])
            else:
                # A scalar can describe a global state but cannot prove that a
                # specific bound channel passed this gate.
                eligible_by_all_gates.clear()
                results[gate] = result
        all_passed = all(
            (value is True)
            or (
                isinstance(value, str)
                and value.lower() in GATE_PASS_STATES[gate]
            )
            or (
                isinstance(value, Mapping)
                and str(value.get("state", "")).lower()
                in GATE_PASS_STATES[gate]
            )
            for gate, value in results.items()
        )
        eligible = []
        if all_passed:
            for row in candidates:
                candidate_id = str(row.get("candidate_id") or "")
                explicitly_eligible = (
                    candidate_id in eligible_by_all_gates
                    and row.get("availability_status") not in {"unavailable", "blocked"}
                )
                if candidate_id and explicitly_eligible:
                    eligible.append(candidate_id)
        return {
            "gate_results": results,
            "eligible_channel_ids": eligible,
            "evidence": {
                "metric_snapshot_id": metric_snapshot_id,
                "confidence_snapshot_id": confidence_snapshot_id,
                "evidence_ids": evidence_ids,
            },
            "network_called": False,
        }
