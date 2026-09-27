"""Minimal local Request -> Strategy -> Adapter -> Decision Log scheduler."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import strategy_engine
from channel_adapter import MockChannelAdapter
from candidate_resolver import CandidateResolver
from decision_logger import DecisionLogger
from error_taxonomy import fallback_allowed_categories, fallback_blocked_categories, load_taxonomy
from execution_engine import ExecutionEngine
from execution_guard import ExecutionGuard, GuardDecision
from request_router import Request, RequestRouter, RuntimeRequest
from retry_policy import RetryPolicy

try:
    from backend.security.authorization import AuthorizationService, AuthorizationError
    from backend.security.principal import PrincipalContext, PrincipalType
except ImportError:  # pragma: no cover - standalone source compatibility
    AuthorizationService = Any  # type: ignore[misc,assignment]
    PrincipalContext = Any  # type: ignore[misc,assignment]
    PrincipalType = None  # type: ignore[assignment]
    AuthorizationError = PermissionError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = ROOT / "output" / "scheduler_decisions_v1.jsonl"
RUNTIME_LOG_PATH = ROOT / "output" / "scheduler_decision_logs_v1.jsonl"
RUNTIME_CONFIG_PATH = ROOT / "config" / "scheduler_runtime_v1.json"
OFFLINE_COMPONENT_HOOKS = (
    "dynamic_metrics", "capability_evidence", "circuit_preselection",
    "sticky_routing", "strategy_ranking", "exploration_shadow",
    "decision_logging", "fallback", "sticky_interruption",
    "circuit_admission", "circuit_outcome", "attribution",
    "execution_guard", "mock_candidate_isolation",
)


def local_decision_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Scheduler:
    def __init__(
        self,
        *,
        candidates_path: str | Path = strategy_engine.CANDIDATES_PATH,
        catalog_path: str | Path = strategy_engine.CATALOG_PATH,
        policy_path: str | Path = strategy_engine.POLICY_PATH,
        adapter: MockChannelAdapter | None = None,
        logger: DecisionLogger | None = None,
        clock: Callable[[], str] = local_decision_time,
        random_seed: int = 42,
        resolver: CandidateResolver | None = None,
        runtime_config_path: str | Path = RUNTIME_CONFIG_PATH,
        id_generator: Callable[[RuntimeRequest, int], str] | None = None,
        runtime_log_enabled: bool = True,
        metric_provider: Any | None = None,
        sticky_service: Any | None = None,
        sticky_eligibility_provider: Any | None = None,
        attribution_store: Any | None = None,
        circuit_breaker_service: Any | None = None,
        exploration_service: Any | None = None,
        capability_evidence_service: Any | None = None,
        price_catalog_service: Any | None = None,
        governance_bridge: Any | None = None,
        timing_recorder: Any | None = None,
        offline_component_overrides: Mapping[str, bool] | None = None,
        offline_ablation_id: str | None = None,
        authorization_service: AuthorizationService | None = None,
    ):
        self.candidates_path = Path(candidates_path)
        self.catalog = strategy_engine.load_json(Path(catalog_path))
        self.policy = strategy_engine.load_json(Path(policy_path))
        self.candidates = strategy_engine.load_csv(self.candidates_path)
        self.router = RequestRouter(self.catalog["supported_strategies"])
        self.adapter = adapter or MockChannelAdapter()
        self.logger = logger or DecisionLogger(DEFAULT_LOG_PATH)
        self.clock = clock
        self.random_seed = random_seed
        self.request_index = 0
        self.resolver = resolver or CandidateResolver()
        self.runtime_config = strategy_engine.load_json(Path(runtime_config_path))
        self.guard = ExecutionGuard(self.runtime_config)
        self.id_generator = id_generator or self._runtime_decision_id
        self.runtime_log_enabled = runtime_log_enabled
        self.metric_provider = metric_provider
        self.sticky_service = sticky_service
        self.sticky_eligibility_provider = sticky_eligibility_provider
        self.attribution_store = attribution_store
        self.circuit_breaker_service = circuit_breaker_service
        self.exploration_service = exploration_service
        self.capability_evidence_service = capability_evidence_service
        self.price_catalog_service = price_catalog_service
        self.governance_bridge = governance_bridge
        self.timing_recorder = timing_recorder
        unknown_hooks = set(offline_component_overrides or {}) - set(OFFLINE_COMPONENT_HOOKS)
        if unknown_hooks:
            raise ValueError(f"unknown offline component hooks: {', '.join(sorted(unknown_hooks))}")
        if offline_component_overrides is not None and not isinstance(self.adapter, MockChannelAdapter):
            raise ValueError("offline component overrides require MockChannelAdapter")
        self.offline_component_hooks = {name: True for name in OFFLINE_COMPONENT_HOOKS}
        self.offline_component_hooks.update(offline_component_overrides or {})
        self.offline_ablation_id = offline_ablation_id
        self.authorization_service = authorization_service
        self._offline_hook_counts = {name: 0 for name in OFFLINE_COMPONENT_HOOKS}
        if self.sticky_service is not None and hasattr(
            self.sticky_service, "set_eligibility_provider_configured"
        ):
            self.sticky_service.set_eligibility_provider_configured(
                self.sticky_eligibility_provider is not None)
        self.error_taxonomy = load_taxonomy()
        self.fallback_policy = self._build_fallback_policy()

    def _authorize_scheduler(
        self, principal: PrincipalContext | None, permission: str
    ) -> PrincipalContext | None:
        """Authorize only a verified Scheduler service context.

        Request payload identity fields are deliberately never consulted.  A
        missing authorization service is the explicit legacy-development
        compatibility mode used by offline tests and local demos.
        """
        if self.authorization_service is None:
            return None
        verified = self.authorization_service.authorize(
            principal, permission, principal_type=PrincipalType.SERVICE)
        if "scheduler_service" not in verified.roles:
            raise AuthorizationError("scheduler_service_identity_required")
        return verified

    @staticmethod
    def _call_scoped(method: Callable[..., Any], *args: Any,
                     principal: PrincipalContext | None, **kwargs: Any) -> Any:
        """Pass identity only to enterprise-aware collaborators.

        This keeps old local fakes working while updated persistence services
        receive the verified context.  A collaborator cannot manufacture the
        value because it is supplied from this server-side call frame.
        """
        import inspect
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "principal" in parameters:
            kwargs["principal"] = principal
        return method(*args, **kwargs)

    def _component_hook(self, name: str) -> bool:
        self._offline_hook_counts[name] += 1
        return self.offline_component_hooks[name]

    def _build_fallback_policy(self) -> RetryPolicy:
        """Derive the mock_execute fallback policy from the single canonical
        error taxonomy (data/error_taxonomy_runtime_v1.json), so the Scheduler
        and the Console's classify() can never disagree about which errors are
        allowed to advance to the next candidate. When fallback_enabled is
        False in config, this collapses to a single attempt regardless of
        max_attempts, matching ExecutionGuard's own invariant."""
        fallback_enabled = bool(self.runtime_config.get("fallback_enabled"))
        max_attempts = int(self.runtime_config["max_attempts"]) if fallback_enabled else 1
        return RetryPolicy({
            "policy_version": f"scheduler-fallback-derived-{self.error_taxonomy['taxonomy_version']}",
            "max_attempts": max_attempts,
            "retryable_errors": fallback_allowed_categories(self.error_taxonomy),
            "non_retryable_errors": fallback_blocked_categories(self.error_taxonomy),
            "automatic_real_execution_authorized": False,
        })

    def schedule(self, payload: Request | Mapping[str, Any], *,
                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        principal = self._authorize_scheduler(principal, "scheduler.decide")
        request = self.router.route(payload)
        index = self.request_index
        self.request_index += 1
        strategy_result = strategy_engine.strategy_decision(
            request_id=request.request_id,
            requested_model=request.requested_model,
            stream_required=request.stream_required,
            input_tokens=request.input_tokens,
            output_tokens=request.output_tokens,
            currency=request.currency,
            decision_time=self.clock(),
            candidates=self.candidates,
            strategy_name=request.strategy,
            random_seed=self.random_seed,
            request_index=index,
            catalog=self.catalog,
            policy=self.policy,
            experiment_metadata={
                "experiment_id": "SCHEDULER-MVP-W4-V1",
                "run_id": "local-mock-scheduler",
                "generated_at": "runtime",
                "data_source": "local_scheduler_mvp_mock",
            },
        )
        selected = strategy_result["selected_candidate"]
        adapter_receipt = (
            self.adapter.dispatch(request_id=request.request_id, candidate_id=selected).to_dict()
            if selected else None
        )
        decision_id = self._decision_id(request, index)
        decision = {
            "decision_id": decision_id,
            "request_id": request.request_id,
            "strategy": request.strategy,
            "outcome": strategy_result["outcome"],
            "selected_candidate": selected,
            "selection_reason": strategy_result["selection_reason"],
            "eligible_count": len(strategy_result["ranked_candidates"]),
            "excluded_count": len(strategy_result["excluded_candidates"]),
            "request_index": index,
            "catalog_version": strategy_result["catalog_version"],
            "policy_version": strategy_result["policy_version"],
            "candidate_snapshot_version": strategy_result["candidate_snapshot_version"],
            "adapter_receipt": adapter_receipt,
            "is_mock": True,
            "data_source": "local_scheduler_mvp_mock",
            "real_api_called": False,
        }
        self.logger.log(decision)
        return decision

    def route(self, payload: RuntimeRequest | Mapping[str, Any], *,
              principal: PrincipalContext | None = None) -> dict[str, Any]:
        route_started = time.perf_counter()
        timing_values: dict[str, float] = {}
        request = self.router.route_runtime(payload)
        required_permission = (
            "scheduler.execute" if request.mode == "mock_execute"
            else "scheduler.decide"
        )
        principal = self._authorize_scheduler(principal, required_permission)
        if self.offline_ablation_id is not None and request.mode not in {"simulation", "mock_execute"}:
            raise ValueError("offline ablation hooks only permit simulation or mock_execute")
        hook_counts_before = dict(self._offline_hook_counts)
        index = self.request_index
        self.request_index += 1
        decision_id = self.id_generator(request, index)
        stage_started = time.perf_counter()
        resolved = self.resolver.resolve(request.mode)
        timing_values["candidate_loading"] = (
            time.perf_counter() - stage_started
        ) * 1000.0
        candidates = resolved.candidates
        metric_context = {
            "provider": "catalog_fixed_metrics",
            "applied_count": 0,
            "snapshot_ids": [],
            "raw_evidence_scanned": False,
        }
        stage_started = time.perf_counter()
        if self.metric_provider is not None and self._component_hook("dynamic_metrics"):
            metadata = request.metadata or {}
            environment_id = str(metadata.get("environment_id") or "")
            request_profile_id = str(metadata.get("request_profile_id") or "")
            if not environment_id or not request_profile_id:
                candidates = [dict(row) for row in candidates]
                for candidate in candidates:
                    candidate["availability_status"] = "unavailable"
                    candidate["dynamic_metrics_state"] = "unknown"
                    candidate["dynamic_metrics_block_reason"] = (
                        "dynamic_metric_scope_required"
                    )
                metric_context = {
                    "provider": "incremental_metric_snapshot",
                    "applied_count": 0,
                    "snapshot_ids": [],
                    "raw_evidence_scanned": False,
                    "block_reason": "dynamic_metric_scope_required",
                }
            else:
                candidates,metric_context=self._call_scoped(self.metric_provider.apply,
                    candidates,environment_id=environment_id,
                    requested_model=request.requested_model,stream=request.stream,
                    request_profile_id=request_profile_id, principal=principal)
        timing_values["metric_lookup"] = (
            time.perf_counter() - stage_started
        ) * 1000.0
        price_context: dict[str, Any] = {
            "provider": "candidate_catalog_embedded_prices",
            "status": "not_configured",
            "catalog_versions": [],
            "resolved_count": 0,
            "blocked_count": 0,
            "network_called": False,
        }
        if self.price_catalog_service is not None:
            # Prices are resolved by the exact environment/model/channel/currency
            # identity.  The Scheduler never guesses or falls back to embedded
            # prices when the authoritative catalog is configured.
            metadata = request.metadata or {}
            environment_id = str(metadata.get("environment_id") or "")
            priced_candidates: list[dict[str, Any]] = []
            versions: set[str] = set()
            resolved_count = 0
            blocked_count = 0
            for row in candidates:
                source = dict(row)
                if not environment_id:
                    price = {
                        "status": "unavailable", "eligible": False,
                        "reason": "price_environment_scope_required",
                        "network_called": False,
                    }
                else:
                    price = self._call_scoped(self.price_catalog_service.resolve,
                        environment_id=environment_id,
                        model_id=request.requested_model,
                        channel_id=str(source.get("channel_id") or
                                       source.get("candidate_id") or ""),
                        currency=request.currency, principal=principal,
                    )
                if price.get("eligible") is True:
                    if price.get("billing_unit") != "per_1m_tokens":
                        price = {**price, "eligible": False,
                                 "status": "unavailable",
                                 "reason": "unsupported_price_billing_unit"}
                    else:
                        source["input_price_per_1m"] = str(
                            price["input_unit_price"])
                        source["output_price_per_1m"] = str(
                            price["output_unit_price"])
                        source["currency"] = str(price["currency"])
                        source["price_catalog_version"] = str(
                            price["catalog_version"])
                        source["price_record_sha256"] = str(
                            price["record_sha256"])
                        source["price_freshness"] = "fresh"
                        versions.add(str(price["catalog_version"]))
                        resolved_count += 1
                if price.get("eligible") is not True:
                    source.update({
                        "availability_status": "unavailable",
                        "routing_eligible": "FALSE",
                        "routing_block_reason": str(
                            price.get("reason") or "price_unavailable"),
                        "price_freshness": str(
                            price.get("status") or "unavailable"),
                    })
                    blocked_count += 1
                priced_candidates.append(source)
            candidates = priced_candidates
            price_context = {
                "provider": "versioned_price_catalog",
                "status": "fresh" if resolved_count and not blocked_count
                else "partial" if resolved_count else "blocked",
                "catalog_versions": sorted(versions),
                "resolved_count": resolved_count,
                "blocked_count": blocked_count,
                "network_called": False,
            }
        safety_context: dict[str, Any] = {
            "circuit_breakers": {}, "capability": None,
            "exploration": None, "execution_governance": [],
            "network_called": False,
        }
        request_metadata = request.metadata or {}
        safety_environment = str(request_metadata.get("environment_id") or "")
        if (self.capability_evidence_service is not None and request.capability_scope
                and self._component_hook("capability_evidence")):
            version = str(request_metadata.get("capability_subject_version") or "")
            subject_map = request_metadata.get("capability_subjects")
            scenario_id = str(request_metadata.get("capability_scenario_id") or "") or None
            raw_constraints = request_metadata.get("capability_request_constraints")
            constraints = dict(raw_constraints) if isinstance(raw_constraints, Mapping) else {}
            modalities = set(request.capability_scope["required_modalities"])
            requirements: dict[str, bool] = {
                "text": "text" in modalities,
                "stream": bool(request.capability_scope.get("stream")),
                "image_in": "image" in modalities and scenario_id != "image_generation",
                "image_out": "image" in modalities and scenario_id == "image_generation",
                "audio": "audio" in modalities,
                "video_in": "video" in modalities,
                "tool": bool(request.capability_scope.get("requires_tools")),
                "context": scenario_id == "long_context" or "context" in constraints,
            }
            unsupported_scope = (
                bool(request.capability_scope.get("requires_structured_output"))
                or ("image" in modalities and scenario_id not in {
                    "image_understanding", "image_generation"})
                or ("audio" in modalities and "audio" not in constraints)
                or ("video" in modalities and "video_in" not in constraints)
                or ("image" in modalities and not any(
                    key in constraints for key in ("image_in", "image_out")))
                or (scenario_id == "long_context" and "context" not in constraints)
            )
            if subject_map is not None and not isinstance(subject_map, Mapping):
                capability = {
                    "allowed": False, "state": "unknown", "fail_closed": True,
                    "reason": "candidate_capability_subject_map_invalid",
                    "candidate_results": {},
                }
                candidates = [
                    {**row, "availability_status": "unavailable",
                     "routing_eligible": "FALSE",
                     "routing_block_reason": "required_capability_unconfirmed"}
                    for row in candidates
                ]
            elif isinstance(subject_map, Mapping) and safety_environment and not unsupported_scope:
                assessed_candidates: list[dict[str, Any]] = []
                candidate_results: dict[str, Any] = {}
                for row in candidates:
                    candidate_id = str(row.get("candidate_id") or "")
                    raw_subject = subject_map.get(candidate_id)
                    subject = dict(raw_subject) if isinstance(raw_subject, Mapping) else {}
                    subject_id = str(subject.get("subject_id") or "")
                    subject_version = str(subject.get("subject_version") or "")
                    if not subject_id or not subject_version:
                        result = {
                            "allowed": False, "state": "unknown", "fail_closed": True,
                            "reason": "candidate_capability_subject_missing",
                            "subject_id": None, "subject_version": None,
                        }
                    else:
                        result = self._call_scoped(
                            self.capability_evidence_service.assess_requirements,
                            environment_id=safety_environment,
                            subject_id=subject_id, subject_version=subject_version,
                            requirements=requirements,
                            decision_source=("live" if request.mode.startswith("real_") else "local"),
                            scenario_id=scenario_id,
                            request_constraints=constraints, principal=principal,
                        )
                        result = {**result, "subject_id": subject_id,
                                  "subject_version": subject_version}
                    candidate_results[candidate_id] = result
                    assessed_candidates.append({
                        **row,
                        **({"availability_status": "unavailable",
                            "routing_eligible": "FALSE",
                            "routing_block_reason": "required_capability_unconfirmed"}
                           if not result.get("allowed") else {}),
                    })
                candidates = assessed_candidates
                allowed_count = sum(
                    1 for item in candidate_results.values() if item.get("allowed") is True)
                capability = {
                    "allowed": allowed_count > 0,
                    "state": "supported" if allowed_count == len(candidate_results)
                    else "partial" if allowed_count else "unknown",
                    "fail_closed": allowed_count == 0,
                    "assessment_scope": "candidate_scoped",
                    "allowed_candidate_count": allowed_count,
                    "candidate_count": len(candidate_results),
                    "candidate_results": candidate_results,
                }
            elif not safety_environment or not version or unsupported_scope:
                capability = {
                    "allowed": False, "state": "unknown", "fail_closed": True,
                    "reason": "capability_scope_or_version_unconfirmed",
                }
            else:
                capability = self._call_scoped(
                    self.capability_evidence_service.assess_requirements,
                    environment_id=safety_environment,
                    subject_id=request.requested_model, subject_version=version,
                    requirements=requirements,
                    decision_source=("live" if request.mode.startswith("real_") else "local"),
                    scenario_id=scenario_id,
                    request_constraints=constraints, principal=principal,
                )
            safety_context["capability"] = capability
            if (capability.get("assessment_scope") != "candidate_scoped"
                    and not capability.get("allowed")):
                candidates = [
                    {**row, "availability_status": "unavailable",
                     "routing_eligible": "FALSE",
                     "routing_block_reason": "required_capability_unconfirmed"}
                    for row in candidates
                ]
        if (self.circuit_breaker_service is not None
                and self._component_hook("circuit_preselection")):
            checked = []
            probe_authorized = bool(request_metadata.get("authorize_half_open_probe"))
            for row in candidates:
                candidate_id = str(row.get("candidate_id") or "")
                circuit_id = f"{safety_environment or 'unknown'}:{candidate_id}:{request.requested_model}"
                state = self._call_scoped(
                    self.circuit_breaker_service.get_state, circuit_id,
                    principal=principal)
                safety_context["circuit_breakers"][candidate_id] = {
                    **state, "circuit_id": circuit_id,
                }
                blocked = state["state"] == "OPEN" or (
                    state["state"] == "HALF_OPEN" and not probe_authorized)
                checked.append({
                    **row,
                    **({"availability_status": "unavailable",
                        "routing_eligible": "FALSE",
                        "routing_block_reason": (
                            "circuit_open" if state["state"] == "OPEN"
                            else "half_open_probe_authorization_required")}
                       if blocked else {}),
                })
            candidates = checked
        if self.governance_bridge is not None:
            governed: list[dict[str, Any]] = []
            for row in candidates:
                admission = self._call_scoped(
                    self.governance_bridge.evaluate,
                    request=request, candidate=row, principal=principal)
                safety_context["execution_governance"].append({
                    "candidate_id": str(row.get("candidate_id") or ""),
                    **admission,
                })
                if admission.get("allowed") is not True:
                    governed.append({
                        **row,
                        "availability_status": "unavailable",
                        "routing_eligible": "FALSE",
                        "routing_block_reason": str(
                            admission.get("reason") or "governance_denied"),
                    })
                else:
                    governed.append(dict(row))
            candidates = governed
        sticky_result: dict[str, Any] | None = None
        sticky_evidence: dict[str, Any] = {}
        sticky_environment = str((request.metadata or {}).get("environment_id") or "")
        if self.sticky_service is not None and self._component_hook("sticky_routing"):
            eligibility: Mapping[str, Any] = {}
            if self.sticky_eligibility_provider is not None:
                if hasattr(self.sticky_eligibility_provider, "evaluate"):
                    eligibility = self.sticky_eligibility_provider.evaluate(
                        request=request, candidates=candidates,
                        metric_context=metric_context)
                else:
                    eligibility = self.sticky_eligibility_provider(
                        request=request, candidates=candidates,
                        metric_context=metric_context)
            sticky_evidence = dict(eligibility.get("evidence") or {})
            sticky_result = self._call_scoped(self.sticky_service.resolve,
                decision_id=decision_id,
                affinity=request.sticky_affinity_key,
                environment_id=sticky_environment,
                requested_model=request.requested_model,
                capability_scope=request.capability_scope,
                stream=request.stream,
                mode=request.mode,
                gate_results=eligibility.get("gate_results"),
                eligible_channel_ids=eligibility.get("eligible_channel_ids", []),
                evidence=sticky_evidence,
                is_mock=request.mode in {"simulation", "mock_execute"},
                principal=principal,
            )
        decision_time = max((row["metrics_updated_at"] for row in candidates), default="2026-07-23 10:10:28")
        stage_started = time.perf_counter()
        engine_result = strategy_engine.strategy_decision(
            request_id=request.request_id, requested_model=request.requested_model,
            stream_required=request.stream, input_tokens=request.input_tokens,
            output_tokens=request.output_tokens, currency=request.currency,
            decision_time=decision_time, candidates=candidates,
            strategy_name=request.strategy, random_seed=self.random_seed,
            request_index=index, catalog=self.catalog, policy=self.policy,
            experiment_metadata={
                "experiment_id": "SCHEDULER-RUNTIME-V1",
                "run_id": f"runtime-v1-{request.request_id}",
                "generated_at": decision_time,
                "data_source": "scheduler_runtime_v1",
            },
        )
        timing_values["eligibility_and_scoring"] = (
            time.perf_counter() - stage_started
        ) * 1000.0
        source_by_id = {row["candidate_id"]: row for row in candidates}
        details = list(engine_result["candidate_details"])
        details.sort(key=lambda item: (item["rank"] is None, item["rank"] or 0, str(item["channel_id"])))
        ranking = []
        for item in details:
            source = source_by_id[item["candidate_id"]]
            ranking_item = {
                "rank": item["rank"], "candidate_id": item["candidate_id"],
                "channel_id": item["channel_id"], "eligible": item["eligible"],
                "exclusion_reason": item["exclusion_reason"],
                "latency_ms": item["latency_ms"], "estimated_cost": item["estimated_cost"],
                "raw_failure_risk": item["raw_failure_risk"],
                "bayesian_failure_risk": item["bayesian_failure_risk"],
                "final_score": item["final_score"],
                "source_type": source.get("source_type"),
                "is_mock": source["is_mock"] == "TRUE",
                "routing_eligible": source.get("routing_eligible") == "TRUE",
                "routing_block_reason": source.get("routing_block_reason"),
                "dynamic_metrics_state": source.get("dynamic_metrics_state"),
                "dynamic_metric_snapshot_id": source.get(
                    "dynamic_metric_snapshot_id"),
                "dynamic_metrics_block_reason": source.get(
                    "dynamic_metrics_block_reason"),
                "price_freshness": source.get("price_freshness"),
                "price_catalog_version": source.get("price_catalog_version"),
                "price_record_sha256": source.get("price_record_sha256"),
            }
            if source.get("statistical_confidence_version"):
                ranking_item.update({
                    "statistical_confidence_state": source.get(
                        "statistical_confidence_state"),
                    "statistical_confidence_label": source.get(
                        "statistical_confidence_label"),
                    "statistical_confidence_version": source.get(
                        "statistical_confidence_version"),
                    "statistical_confidence_snapshot_id": source.get(
                        "statistical_confidence_snapshot_id"),
                    "confidence_policy_version": source.get(
                        "confidence_policy_version"),
                    "confidence_effective_sample_size": source.get(
                        "confidence_effective_sample_size"),
                    "confidence_interval_lower": source.get(
                        "confidence_interval_lower"),
                    "confidence_interval_upper": source.get(
                        "confidence_interval_upper"),
                    "confidence_adjusted_success_score": source.get(
                        "confidence_adjusted_success_score"),
                    "confidence_adjustment_reason_codes": source.get(
                        "confidence_adjustment_reason_codes"),
                    "confidence_block_reason": source.get(
                        "confidence_block_reason"),
                })
            if request.strategy == "confidence_aware_v3":
                ranking_item["confidence_adjusted_failure_risk"] = item.get(
                    "confidence_adjusted_failure_risk")
            ranking.append(ranking_item)
        eligible = [row for row in ranking if row["eligible"]]
        if self._component_hook("strategy_ranking"):
            recommended = engine_result["selected_candidate"]
        else:
            recommended = eligible[-1]["candidate_id"] if eligible else None
        sticky_prerequisite_blocked = bool(
            sticky_result
            and (
                sticky_result.get("routing_blocked")
                or any(
                    not bool(item.get("passed"))
                    for item in sticky_result.get("gate_trace", [])
                )
            )
        )
        if sticky_prerequisite_blocked:
            # Strategy scores remain available as counterfactual explanation,
            # but an earlier eligibility denial prevents channel selection or
            # execution. Sticky reuse never downgrades a security gate.
            recommended = None
        if sticky_result and sticky_result.get("outcome") == "HIT":
            sticky_candidate = str(sticky_result.get("selected_channel") or "")
            if any(row["candidate_id"] == sticky_candidate for row in eligible):
                recommended = sticky_candidate
            else:
                sticky_result = {
                    **sticky_result,
                    "outcome": "INTERRUPTED",
                    "reason": "bound_channel_rejected_by_strategy_eligibility",
                }
                binding = sticky_result.get("binding") or {}
                if binding.get("sticky_binding_id"):
                    self._call_scoped(self.sticky_service.interrupt,
                        binding["sticky_binding_id"], decision_id=decision_id,
                        reason="bound_channel_rejected_by_strategy_eligibility",
                        principal=principal)
        recommended_source = source_by_id.get(recommended) if recommended else None
        fallback_order = [row["candidate_id"] for row in eligible if row["candidate_id"] != recommended]
        if (self.exploration_service is not None and recommended_source is not None
                and self._component_hook("exploration_shadow")):
            def exploration_health(source: Mapping[str, Any]) -> dict[str, Any]:
                return {
                    "state": "healthy" if source.get("availability_status") == "available" else "unknown",
                    "samples": int(source.get("sample_size") or 0),
                    "failure_rate": float(source.get("raw_failure_risk") or
                                          (1.0 - float(source.get("success_rate") or 0.0))),
                    "latency_ms": float(source.get("latency_ms") or 0.0),
                }
            exploration_candidates = []
            for row in eligible:
                source = source_by_id[row["candidate_id"]]
                circuit = safety_context["circuit_breakers"].get(row["candidate_id"]) or {}
                exploration_candidates.append({
                    "id": row["candidate_id"], "channel_id": str(source.get("channel_id") or ""),
                    "model_id": request.requested_model,
                    "mean_reward": 1.0 - float(row.get("raw_failure_risk") or 1.0),
                    "trials": int(source.get("sample_size") or 0),
                    "health": exploration_health(source),
                    "circuit_state": str(circuit.get("state") or "UNKNOWN"),
                })
            recommended_circuit = safety_context["circuit_breakers"].get(recommended) or {}
            recommended_cost = next(
                (row.get("estimated_cost") or 0.0 for row in eligible
                 if row["candidate_id"] == recommended), 0.0)
            try:
                exploration_result = self._call_scoped(
                    self.exploration_service.reserve_recommendation,
                    idempotency_key=f"explore:{decision_id}:{request.request_id}",
                    candidates=exploration_candidates, request_id=request.request_id,
                    run_id=str(request_metadata.get("run_id") or f"runtime-v1-{request.request_id}"),
                    environment_id=safety_environment,
                    channel_id=str(recommended_source.get("channel_id") or recommended),
                    model_id=request.requested_model,
                    health=exploration_health(recommended_source),
                    circuit_state=str(recommended_circuit.get("state") or "UNKNOWN"),
                    cost=float(recommended_cost), principal=principal,
                )
            except Exception as exc:
                exploration_result = {
                    "allowed": False, "selected": None,
                    "mode": "shadow_only", "execution_authorized": False,
                    "reasons": [f"reservation_blocked:{type(exc).__name__}"],
                    "network_called": False,
                }
            exploration_result["execution_authorized"] = False
            safety_context["exploration"] = exploration_result
            # The governance service is intentionally advisory in Stage 1.
            # Its selection is never copied into ``recommended`` or execution.
        isolation_enabled = self._component_hook("mock_candidate_isolation")
        guard_source = recommended_source
        if guard_source is not None and not isolation_enabled:
            guard_source = {**guard_source, "is_mock": "TRUE"}
        guard = (
            self.guard.check(request.mode, guard_source, self.adapter)
            if self._component_hook("execution_guard")
            else GuardDecision("allowed", True, False, None)
        )
        if (
            sticky_result and sticky_result.get("outcome") == "HIT"
            and guard.status == "blocked"
        ):
            binding = sticky_result.get("binding") or {}
            if binding.get("sticky_binding_id"):
                updated = self._call_scoped(self.sticky_service.interrupt,
                    binding["sticky_binding_id"], decision_id=decision_id,
                    reason="execution_guard_denied", principal=principal)
                sticky_result = {
                    **sticky_result, "outcome": "INTERRUPTED",
                    "reason": "execution_guard_denied", "binding": updated,
                }
        if (
            sticky_result and sticky_result.get("outcome") == "MISS"
            and recommended and request.sticky_affinity_key
            and guard.status in {"allowed", "decision_only"}
        ):
            sticky_result = self._call_scoped(self.sticky_service.create_binding,
                decision_id=decision_id, affinity=request.sticky_affinity_key,
                environment_id=sticky_environment,
                requested_model=request.requested_model,
                capability_scope=request.capability_scope, stream=request.stream,
                selected_channel=recommended, evidence=sticky_evidence,
                is_mock=request.mode in {"simulation", "mock_execute"},
                principal=principal,
            )
            if sticky_result.get("outcome") == "HIT":
                concurrent_winner = str(
                    (sticky_result.get("binding") or {}).get(
                        "selected_channel") or "")
                if concurrent_winner and any(
                    row["candidate_id"] == concurrent_winner for row in eligible
                ):
                    recommended = concurrent_winner
                    recommended_source = source_by_id.get(recommended)
                    fallback_order = [
                        row["candidate_id"] for row in eligible
                        if row["candidate_id"] != recommended
                    ]
                    guard_source = recommended_source
                    if guard_source is not None and not isolation_enabled:
                        guard_source = {**guard_source, "is_mock": "TRUE"}
                    guard = (
                        self.guard.check(request.mode, guard_source, self.adapter)
                        if self.offline_component_hooks["execution_guard"]
                        else GuardDecision("allowed", True, False, None)
                    )
                else:
                    sticky_result = {
                        **sticky_result, "outcome": "BLOCKED",
                        "reason": "concurrent_binding_winner_not_eligible",
                    }
        adapter_result = None
        execution_attempted = False
        routing_allowed = guard.routing_allowed
        error_category = guard.error_category
        attempts: list[dict[str, Any]] = []
        fallback_executed = False
        fallback_trace: list[str] = []
        stopped_reason = None
        if request.mode == "mock_execute" and guard.status == "allowed" and recommended_source:
            execution_attempted = True
            circuit_admissions: dict[str, dict[str, Any]] = {}
            def execute_candidate(req: Any, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
                candidate_id = str(candidate["candidate_id"])
                admission = None
                if (self.circuit_breaker_service is not None
                        and self._component_hook("circuit_admission")):
                    circuit = safety_context["circuit_breakers"].get(candidate_id) or {}
                    circuit_id = str(circuit.get("circuit_id") or
                                     f"{safety_environment or 'unknown'}:{candidate_id}:{request.requested_model}")
                    admission = self._call_scoped(
                        self.circuit_breaker_service.before_request,
                        circuit_id, f"{decision_id}:{candidate_id}",
                        principal=principal)
                    circuit_admissions[candidate_id] = {**admission, "circuit_id": circuit_id}
                    if not admission["allowed"]:
                        return {"status": "circuit_open", "error_category": "circuit_open",
                                "is_mock": True, "network_called": False,
                                "adapter_type": None}
                raw = dict(self.adapter.send(req, candidate))
                if (self.circuit_breaker_service is not None and admission is not None
                        and self._component_hook("circuit_outcome")):
                    circuit_id = circuit_admissions[candidate_id]["circuit_id"]
                    event_id = f"{decision_id}:outcome:{candidate_id}"
                    lease = admission.get("probe_lease_id")
                    success = str(raw.get("status")) in {"success", "ok", "mock_success"}
                    updated = (
                        self._call_scoped(
                            self.circuit_breaker_service.record_success,
                            circuit_id, event_id, probe_lease_id=lease,
                            principal=principal)
                        if success else self._call_scoped(
                            self.circuit_breaker_service.record_failure,
                            circuit_id, event_id, probe_lease_id=lease,
                            principal=principal)
                    )
                    safety_context["circuit_breakers"][candidate_id] = {
                        **updated, "circuit_id": circuit_id,
                    }
                return raw
            def before_fallback(attempt: Mapping[str, Any], _next: str) -> None:
                nonlocal sticky_result
                binding = (sticky_result or {}).get("binding") or {}
                binding_id = binding.get("sticky_binding_id")
                if binding_id and self._component_hook("sticky_interruption"):
                    error = str(attempt.get("error") or "unknown")
                    categories = set(self.sticky_service.policy.get(
                        "interruption_error_categories", []))
                    if (
                        error not in categories
                        and "fallback_requires_reassignment" not in categories
                    ):
                        return
                    reason = f"fallback_requires_reassignment:{error}"
                    updated = self._call_scoped(self.sticky_service.interrupt,
                        binding_id, decision_id=decision_id, reason=reason,
                        principal=principal)
                    sticky_result = {
                        **(sticky_result or {}), "outcome": "INTERRUPTED",
                        "reason": reason, "binding": updated,
                    }
            fallback_policy = self.fallback_policy
            if not self._component_hook("fallback"):
                fallback_policy = RetryPolicy({
                    "policy_version": "offline-ablation-no-fallback-v1",
                    "max_attempts": 1,
                    "retryable_errors": self.fallback_policy.retryable_errors,
                    "non_retryable_errors": self.fallback_policy.non_retryable_errors,
                    "automatic_real_execution_authorized": False,
                })
            engine = ExecutionEngine(
                execute_candidate,
                fallback_policy,
                before_fallback=before_fallback if self.sticky_service is not None else None,
                authorization_service=self.authorization_service,
            )
            recovery = engine.execute(
                request=request,
                decision={"request_id": request.request_id, "recommended_candidate": recommended, "fallback_order": fallback_order},
                candidates=source_by_id,
                principal=principal,
            )
            if (
                self.sticky_service is not None
                and recovery["final_status"] != "success"
                and recovery["attempts"]
                and (sticky_result or {}).get("outcome") in {"HIT", "CREATED"}
            ):
                terminal_error = str(
                    recovery["attempts"][-1].get("error") or "unknown")
                categories = set(self.sticky_service.policy.get(
                    "interruption_error_categories", []))
                if (
                    terminal_error in categories
                    or "retry_requires_reassignment" in categories
                ):
                    binding_id = (
                        ((sticky_result or {}).get("binding") or {})
                        .get("sticky_binding_id")
                    )
                    if (binding_id
                            and self._component_hook("sticky_interruption")):
                        reason = (
                            f"retry_requires_reassignment:{terminal_error}")
                        updated = self._call_scoped(
                            self.sticky_service.interrupt, binding_id,
                            decision_id=decision_id, reason=reason,
                            principal=principal)
                        sticky_result = {
                            **(sticky_result or {}),
                            "outcome": "INTERRUPTED",
                            "reason": reason,
                            "binding": updated,
                        }
            for entry in recovery["attempts"]:
                raw = entry["raw_result"]
                attempts.append({
                    "attempt_number": entry["attempt_number"], "candidate_id": entry["channel"],
                    "channel_id": source_by_id[entry["channel"]].get("channel_id"),
                    "result": entry["result"], "error_category": entry["error"],
                    "is_mock": entry["is_mock"], "network_called": entry["network_called"],
                    "latency_ms": raw.get("latency_ms"), "adapter_status": raw.get("status"),
                })
            fallback_executed = recovery["fallback_executed"]
            fallback_trace = recovery["fallback_trace"]
            stopped_reason = recovery["stopped_reason"]
            last_raw = recovery["attempts"][-1]["raw_result"] if recovery["attempts"] else None
            adapter_result = last_raw
            execution_status = last_raw["status"] if last_raw else "not_executed_unroutable"
            error_category = None if recovery["final_status"] == "success" else (last_raw["error_category"] if last_raw else None)
        elif request.mode == "real_execute":
            execution_status = "blocked"
        elif not recommended:
            execution_status = "not_executed_unroutable"
        else:
            execution_status = "decision_only"
        outcome = (
            "blocked" if request.mode == "real_execute" or sticky_prerequisite_blocked
            else engine_result["outcome"])
        if request.mode == "mock_execute":
            limitations = [
                f"mock_execute按config/scheduler_runtime_v1.json的max_attempts（{self.runtime_config['max_attempts']}）和fallback_enabled在候选间执行有限故障切换，全部为Mock，从不调用网络。",
                "Runtime v1不调用真实API，network_called始终为FALSE。",
            ]
        else:
            limitations = [
                "fallback_order仅为排序建议，此模式下未执行重试或故障切换。",
                "Runtime v1不调用真实API，network_called始终为FALSE。",
            ]
        if request.mode == "real_shadow":
            limitations.append("真实渠道推荐仅限当前小样本UAT影子评估。")
        if self.metric_provider is not None:
            limitations.append(
                "候选指标只来自持久化动态快照；缺失、陈旧或已阻止快照不会回退到固定健康值。")
        decision = {
            "decision_id": decision_id,
            "runtime_version": self.runtime_config["runtime_version"],
            "request_id": request.request_id, "mode": request.mode,
            "strategy": request.strategy, "catalog_version": resolved.catalog_version,
            "catalog_sha256": resolved.catalog_sha256, "requested_model": request.requested_model,
            "stream": request.stream, "candidate_count": resolved.raw_candidate_count,
            "eligible_count": len(eligible), "excluded_count": len(ranking) - len(eligible),
            "excluded_candidates": [row for row in ranking if not row["eligible"]],
            "candidate_ranking": ranking, "ranking": ranking,
            "recommended_candidate": recommended,
            "fallback_order": fallback_order,
            "outcome": outcome,
            "status": (
                "blocked" if request.mode == "real_execute"
                or sticky_prerequisite_blocked else "ok"),
            "routing_allowed": routing_allowed, "execution_attempted": execution_attempted,
            "execution_status": execution_status,
            "adapter_type": adapter_result["adapter_type"] if adapter_result else None,
            "adapter_result": adapter_result, "network_called": False,
            "error_category": error_category, "limitations": limitations,
            "recommendation_scope": "offline_shadow_recommendation_only" if request.mode == "real_shadow" else "runtime_decision_only" if request.mode == "simulation" else "mock_execution" if request.mode == "mock_execute" else "blocked",
            "data_source": resolved.source_type, "is_mock": request.mode in {"simulation", "mock_execute"},
            "metadata": request.metadata or {},
            "attempts": attempts, "fallback_executed": fallback_executed,
            "fallback_trace": fallback_trace, "stopped_reason": stopped_reason,
            "max_attempts": self.fallback_policy.max_attempts,
            "metric_context": metric_context,
            "price_context": price_context,
            "safety_governance": safety_context,
            "principal": (
                {
                    "principal_id": principal.principal_id,
                    "principal_type": principal.principal_type.value,
                    "tenant_id": principal.tenant_id,
                    "workspace_id": principal.workspace_id,
                    "correlation_id": principal.correlation_id,
                }
                if principal is not None else {
                    "principal_id": "legacy-local-development",
                    "principal_type": "service",
                    "tenant_id": "tenant_local_dev_v1",
                    "workspace_id": "workspace_local_dev_v1",
                    "correlation_id": f"legacy:{request.request_id}",
                }
            ),
            # Scheduler intent, local execution and platform-observed execution
            # are deliberately separate.  A selected candidate is never used
            # to manufacture authoritative platform attribution.
            "run_id": str((request.metadata or {}).get("run_id") or
                          f"runtime-v1-{request.request_id}"),
            "decision_policy_version": engine_result.get("policy_version"),
            "selected_target_id": recommended,
            "selected_channel_id": (
                recommended_source.get("channel_id")
                if recommended_source else None),
            "metric_snapshot_ids": sorted({
                str(value) for value in metric_context.get("snapshot_ids", [])
                if value
            }),
            "confidence_snapshot_ids": sorted({
                str(row.get("statistical_confidence_snapshot_id"))
                for row in ranking
                if row.get("statistical_confidence_snapshot_id")
            }),
            "request_constraints": {
                "stream": request.stream,
                "input_tokens": request.input_tokens,
                "output_tokens": request.output_tokens,
                "currency": request.currency,
                "max_latency_ms": request.max_latency_ms,
                "max_estimated_cost": request.max_estimated_cost,
                "capability_scope": request.capability_scope,
                "capability_scenario_id": request_metadata.get(
                    "capability_scenario_id"),
                "capability_request_constraints": request_metadata.get(
                    "capability_request_constraints"),
            },
            "capability_evidence_ids": sorted({
                str(evidence_id)
                for result in (
                    (safety_context.get("capability") or {}).get(
                        "candidate_results", {}).values()
                    if isinstance((safety_context.get("capability") or {}).get(
                        "candidate_results"), Mapping)
                    else [safety_context.get("capability") or {}]
                )
                for requirement in (result.get("requirements") or {}).values()
                for evidence_id in requirement.get("evidence_ids", [])
                if evidence_id
            }),
            "price_record_sha256s": sorted({
                str(row.get("price_record_sha256")) for row in ranking
                if row.get("price_record_sha256")
            }),
            "governance_assertions": {
                "capability_fail_closed": not bool(
                    safety_context.get("capability")) or bool(
                    (safety_context.get("capability") or {}).get("allowed")),
                "candidate_scores_finite": all(
                    row.get("final_score") is None
                    or isinstance(row.get("final_score"), (int, float))
                    for row in ranking),
                "execution_network_forbidden": True,
                "authoritative_channel_not_inferred": True,
            },
            "acceptance_assertion": (
                {
                    "expected_candidate_id": str(
                        request_metadata.get("expected_candidate_id")),
                    "actual_candidate_id": recommended,
                    "passed": str(request_metadata.get("expected_candidate_id"))
                    == str(recommended),
                }
                if request_metadata.get("expected_candidate_id") is not None
                else None
            ),
            "downstream_request_correlation_id": (
                str((request.metadata or {}).get(
                    "downstream_request_correlation_id"))
                if (request.metadata or {}).get(
                    "downstream_request_correlation_id") else None),
            "executed_candidate_id": (
                attempts[-1]["candidate_id"] if attempts else None),
            "executed_channel_id": (
                attempts[-1]["channel_id"] if attempts else None),
            "authoritative_actual_channel": None,
            "attribution_status": (
                "mock_execution_not_authoritative"
                if attempts else "authoritative_execution_attribution_missing"),
        }
        if sticky_result is not None:
            decision["sticky_routing"] = sticky_result
            if sticky_result.get("outcome") == "HIT":
                decision["selection_reason"] = (
                    "All safety and eligibility gates passed; reused the active "
                    "policy-aware sticky binding before strategy fallback.")
            elif sticky_result.get("outcome") in {"BLOCKED", "INTERRUPTED"}:
                decision["sticky_routing_block_reason"] = sticky_result.get("reason")
        logging_enabled = self._component_hook("decision_logging")
        attribution_enabled = (
            self._component_hook("attribution")
            if self.attribution_store is not None else True
        )
        if self.offline_ablation_id is not None:
            decision["offline_component_trace"] = {
                "ablation_id": self.offline_ablation_id,
                "changed_hooks": sorted(
                    name for name, enabled in self.offline_component_hooks.items()
                    if not enabled),
                "branch_counts": {
                    name: self._offline_hook_counts[name] - hook_counts_before[name]
                    for name in OFFLINE_COMPONENT_HOOKS
                },
                "offline_only": True,
                "network_called": False,
            }
        run_id = str(decision["run_id"])
        decision["scheduler_timing"] = {
            "schema_version": "scheduler_timing_v1",
            "stages_ms": {key: round(value, 6) for key, value in timing_values.items()},
            "provider_latency_included": False,
        }
        if (self.runtime_log_enabled and self.runtime_config["decision_log_enabled"]
                and logging_enabled):
            stage_started = time.perf_counter()
            self.logger.log_runtime(decision)
            timing_values["audit_emission"] = (
                time.perf_counter() - stage_started
            ) * 1000.0
        if self.attribution_store is not None and attribution_enabled:
            stage_started = time.perf_counter()
            self._call_scoped(
                self.attribution_store.record_scheduler_decision, decision,
                principal=principal)
            timing_values["decision_persistence"] = (
                time.perf_counter() - stage_started
            ) * 1000.0
        timing_values["scheduler_total"] = (
            time.perf_counter() - route_started
        ) * 1000.0
        decision["scheduler_timing"]["stages_ms"] = {
            key: round(value, 6) for key, value in timing_values.items()
        }
        if self.timing_recorder is not None:
            timing_errors: list[str] = []
            for stage, duration_ms in timing_values.items():
                try:
                    self.timing_recorder.record(
                        decision_id=decision_id,
                        run_id=run_id,
                        stage=stage,
                        duration_ms=duration_ms,
                    )
                except Exception:
                    # Telemetry is observational. A persistence conflict or
                    # unavailable telemetry store must never change a routing
                    # outcome; expose only a bounded stage code.
                    timing_errors.append(stage)
            decision["scheduler_timing"]["persistence_status"] = (
                "ready" if not timing_errors else "degraded")
            decision["scheduler_timing"]["failed_stages"] = timing_errors
        return decision

    @staticmethod
    def _decision_id(request: Request, request_index: int) -> str:
        canonical = json.dumps(
            {**request.to_dict(), "request_index": request_index},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "DEC-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16].upper()

    @staticmethod
    def _runtime_decision_id(request: RuntimeRequest, request_index: int) -> str:
        canonical = json.dumps({**request.to_dict(), "request_index": request_index}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "RUNTIME-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20].upper()
