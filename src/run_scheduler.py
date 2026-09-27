"""Command-line entry point for Scheduler Runtime v1 (no real network access)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.sticky_routing_service import StickyRoutingService
from backend.circuit_breaker_service import CircuitBreakerService
from backend.capability_evidence_service import CapabilityEvidenceService
from backend.high_cost_test_governance_service import HighCostTestGovernanceService
from backend.exploration_governance_service import ExplorationGovernanceService
from backend.price_catalog_service import PriceCatalogService
from backend.probe_governance_service import ProbeGovernanceService
from backend.scheduler_governance_bridge import SchedulerGovernanceBridge
from backend.traffic_change_governance_service import TrafficChangeGovernanceService
from decision_logger import DecisionLogger
from incremental_metrics_provider import IncrementalMetricProvider
from request_router import RequestValidationError
from scheduler import RUNTIME_LOG_PATH, Scheduler
from scheduler_timing import SchedulerTimingRecorder
from sticky_eligibility import ControlledStickyEligibilityProvider
from sticky_routing import GATE_ORDER, load_policy


def parse_bool(value: str) -> bool:
    normalized = value.lower()
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return normalized == "true"


def build_payload(args: argparse.Namespace) -> dict:
    if args.request_file:
        with args.request_file.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("request file must contain one JSON object")
    else:
        payload = {
        "request_id": args.request_id, "requested_model": args.model,
        "stream": args.stream, "input_tokens": args.input_tokens,
        "output_tokens": args.output_tokens, "currency": args.currency,
        "strategy": args.strategy, "mode": args.mode,
        }
    if args.environment_id or args.request_profile_id:
        metadata = dict(payload.get("metadata") or {})
        if args.environment_id:
            metadata["environment_id"] = args.environment_id
        if args.request_profile_id:
            metadata["request_profile_id"] = args.request_profile_id
        payload["metadata"] = metadata
    return payload


def main(argv: list[str] | None = None) -> int:
    # Keep the machine-readable CLI contract stable when Windows pipes default
    # to a legacy code page while callers explicitly decode UTF-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--request-id")
    parser.add_argument("--model")
    parser.add_argument("--stream", type=parse_bool)
    parser.add_argument("--input-tokens", type=int)
    parser.add_argument("--output-tokens", type=int)
    parser.add_argument("--currency")
    parser.add_argument("--strategy")
    parser.add_argument("--mode")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--log-path", type=Path)
    parser.add_argument("--metrics-database", type=Path)
    parser.add_argument("--metrics-window", choices=("5m", "1h", "24h"), default="1h")
    parser.add_argument("--environment-id")
    parser.add_argument("--request-profile-id")
    parser.add_argument(
        "--confidence-version",
        default="weighted_wilson_v1",
        help="Required version when --metrics-database enables snapshot routing.",
    )
    parser.add_argument("--catalog-path", type=Path)
    parser.add_argument(
        "--sticky-policy", type=Path,
        default=ROOT / "config" / "sticky_routing_policy_v1.json")
    parser.add_argument("--sticky-database", type=Path)
    parser.add_argument(
        "--circuit-policy", type=Path,
        default=ROOT / "config" / "circuit_breaker_policy_v1.json")
    parser.add_argument("--circuit-database", type=Path)
    parser.add_argument("--timing-database", type=Path)
    parser.add_argument("--price-database", type=Path,
        help="Optional local versioned price catalog. When configured, missing/stale prices fail closed.")
    parser.add_argument("--governance-database", type=Path)
    parser.add_argument("--exploration-database", type=Path,
        help="Optional tenant-scoped shadow exploration state database; never authorizes execution.")
    parser.add_argument(
        "--sticky-eligibility-file", type=Path,
        help=(
            "Local, versioned gate snapshot. No network access is performed; "
            "missing providers fail closed when sticky routing is enabled."))
    args = parser.parse_args(argv)
    try:
        payload = build_payload(args)
        log_path = args.log_path or ((args.output_dir / RUNTIME_LOG_PATH.name) if args.output_dir else RUNTIME_LOG_PATH)
        metric_provider = (
            IncrementalMetricProvider(
                args.metrics_database,
                window=args.metrics_window,
                confidence_version=args.confidence_version,
            )
            if args.metrics_database
            else None
        )
        selected_strategy = str(payload.get("strategy") or "")
        catalog_path = args.catalog_path or (
            Path(__file__).resolve().parents[1]
            / "config"
            / "strategy_catalog_v3.json"
            if selected_strategy == "confidence_aware_v3"
            else None
        )
        sticky_policy = load_policy(args.sticky_policy)
        circuit_database = (
            args.circuit_database
            or ROOT / "data" / "scheduler_safety_governance.sqlite3")
        circuit_service = CircuitBreakerService(
            circuit_database, args.circuit_policy)
        console_database = ROOT / "data" / "routing_quality_console.sqlite3"
        timing_recorder = SchedulerTimingRecorder(
            args.timing_database or console_database)
        price_service = (PriceCatalogService(
            args.price_database,
            ROOT / "config" / "price_sync_policy_v1.json")
            if args.price_database else None)
        governance_database = args.governance_database or console_database
        exploration_service = ExplorationGovernanceService(
            args.exploration_database or governance_database,
            ROOT / "config" / "exploration_policy_v1.json")
        capability_service = CapabilityEvidenceService(
            governance_database,
            ROOT / "config" / "capability_evidence_policy_v1.json")
        governance_bridge = SchedulerGovernanceBridge(
            traffic_change=TrafficChangeGovernanceService(
                governance_database,
                ROOT / "config" / "traffic_change_governance_policy_v1.json"),
            probe=ProbeGovernanceService(
                governance_database,
                ROOT / "config" / "probe_governance_policy_v1.json"),
            high_cost_test=HighCostTestGovernanceService(
                governance_database,
                ROOT / "config" / "high_cost_test_governance_policy_v1.json"),
        )
        sticky_service = None
        sticky_eligibility = None
        if sticky_policy["enabled"] or args.sticky_database:
            sticky_database = (
                args.sticky_database
                or ROOT / "data" / "sticky_routing.sqlite3")
            sticky_service = StickyRoutingService(
                sticky_database, args.sticky_policy)
            providers = {}
            if args.sticky_eligibility_file:
                with args.sticky_eligibility_file.open(
                    encoding="utf-8-sig") as handle:
                    snapshot = json.load(handle)
                gate_results = snapshot.get("gate_results")
                if not isinstance(gate_results, dict):
                    raise ValueError(
                        "sticky eligibility file requires gate_results object")
                providers = {
                    gate: (
                        lambda *, _result=result, **_kwargs: dict(_result)
                    )
                    for gate, result in gate_results.items()
                    if gate in GATE_ORDER and isinstance(result, dict)
                }
            sticky_eligibility = ControlledStickyEligibilityProvider(providers)
        scheduler = Scheduler(
            logger=DecisionLogger(log_path),
            runtime_log_enabled=not args.dry_run,
            metric_provider=metric_provider,
            sticky_service=sticky_service,
            sticky_eligibility_provider=sticky_eligibility,
            circuit_breaker_service=circuit_service,
            capability_evidence_service=capability_service,
            exploration_service=exploration_service,
            price_catalog_service=price_service,
            governance_bridge=governance_bridge,
            timing_recorder=timing_recorder,
            **({"catalog_path": catalog_path} if catalog_path else {}),
        )
        result = scheduler.route(payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == "blocked" else 0
    except RequestValidationError as exc:
        print(json.dumps({"status": "error", **exc.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error_code": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
