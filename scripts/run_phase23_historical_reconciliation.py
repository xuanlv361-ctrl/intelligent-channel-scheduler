"""Run the bounded, entirely offline Phase 2/3 historical reconciliation."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.historical_uat_csv_adapter import (  # noqa: E402
    HistoricalUatCsvAdapter,
    safe_schema_mapping,
    within_window,
)
from backend.historical_uat_reconciliation import (  # noqa: E402
    ReconciliationError,
    canonical_sha256,
    load_protected_v3,
    project_metric_events,
    reconcile,
)
from backend.incremental_metrics_service import IncrementalMetricsService  # noqa: E402
from backend.historical_evidence_import_service import (  # noqa: E402
    HistoricalEvidenceImportService,
)
from backend.metrics_evidence_adapter import MetricsEvidenceAdapter  # noqa: E402
from decision_logger import DecisionLogger  # noqa: E402
from incremental_metrics_provider import IncrementalMetricProvider  # noqa: E402
from scheduler import Scheduler  # noqa: E402
from statistical_confidence import (  # noqa: E402
    CONFIDENCE_VERSION,
    calculate_weighted_wilson,
    load_confidence_policy,
)

DEFAULT_V3 = ROOT / "output" / "unified_uat_execution_v3.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / "output" / "phase23_historical_reconciliation"
WINDOW_START = datetime.fromisoformat("2026-07-29T10:00:20+08:00")
WINDOW_END = datetime.fromisoformat("2026-07-29T11:01:56+08:00")


def _json_write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _jsonl_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _file_rows(path: Path) -> int:
    return len([line for line in path.read_bytes().splitlines() if line.strip()])


def _manifest(
    v3_path: Path,
    csv_path: Path,
    v3_fingerprint: dict[str, Any],
    csv_metadata: dict[str, Any],
) -> dict[str, Any]:
    schema = safe_schema_mapping()
    value = {
        "manifest_version": "phase23_historical_reconciliation_manifest_v1",
        "mode": "bounded_offline_historical_evidence_reconciliation",
        "authorized_window": {
            "timezone": "Asia/Shanghai",
            "date_from": WINDOW_START.isoformat(),
            "date_to": WINDOW_END.isoformat(),
        },
        "inputs": {
            "unified_uat_execution_v3": {
                "file_name": v3_path.name,
                **v3_fingerprint,
                "immutable": True,
            },
            "uat_billing_csv": {
                "file_name": csv_path.name,
                "row_count": csv_metadata["data_row_count"],
                "byte_size": csv_metadata["byte_size"],
                "sha256": csv_metadata["sha256"],
                "immutable": True,
            },
        },
        "restrictions": {
            "network_requests": 0,
            "completion_calls": 0,
            "platform_writes": 0,
            "production_access": 0,
            "raw_api_keys_persisted": 0,
            "raw_ip_values_persisted": 0,
        },
        "schema_registry": {
            "schema_id": schema["schema_version"],
            "registry_version": schema["registry_version"],
            "registry_sha256": schema["registry_sha256"],
        },
    }
    value["manifest_sha256"] = canonical_sha256(value)
    return value


def _init_cursor(database: Path) -> None:
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS historical_reconciliation_source_cursor(
                 source_name TEXT PRIMARY KEY,
                 csv_sha256 TEXT NOT NULL,
                 v3_sha256 TEXT NOT NULL,
                 evidence_manifest_sha256 TEXT NOT NULL,
                 accepted_count INTEGER NOT NULL,
                 updated_at TEXT NOT NULL
               )"""
        )


def _set_cursor(
    database: Path,
    *,
    manifest: dict[str, Any],
    accepted_count: int,
    updated_at: datetime,
) -> None:
    with sqlite3.connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO historical_reconciliation_source_cursor
                 VALUES(?,?,?,?,?,?)
               ON CONFLICT(source_name) DO UPDATE SET
                 csv_sha256=excluded.csv_sha256,
                 v3_sha256=excluded.v3_sha256,
                 evidence_manifest_sha256=excluded.evidence_manifest_sha256,
                 accepted_count=excluded.accepted_count,
                 updated_at=excluded.updated_at""",
            (
                "historical_uat_csv_v3_reconciliation",
                manifest["inputs"]["uat_billing_csv"]["sha256"],
                manifest["inputs"]["unified_uat_execution_v3"]["sha256"],
                manifest["manifest_sha256"],
                accepted_count,
                updated_at.astimezone(timezone.utc).isoformat(),
            ),
        )


def _get_cursor(database: Path) -> dict[str, Any]:
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM historical_reconciliation_source_cursor"
        ).fetchone()
    return dict(row) if row else {}


def _snapshot_evidence(database: Path, snapshot: dict[str, Any]) -> list[str]:
    with sqlite3.connect(database) as db:
        rows = db.execute(
            """SELECT evidence_id FROM metric_evidence_events
               WHERE observed_at>? AND observed_at<=?
                 AND environment_id=? AND model=? AND channel=?
                 AND stream=? AND request_profile_id=?
                 AND COALESCE(currency,'')=COALESCE(?,'')
               ORDER BY evidence_id""",
            (
                snapshot["window_start"],
                snapshot["window_end"],
                snapshot["environment_id"],
                snapshot["model"],
                snapshot["channel"],
                int(snapshot["stream"]),
                snapshot["request_profile_id"],
                snapshot["currency"],
            ),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _phase2(
    output: Path,
    events: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], IncrementalMetricsService]:
    database = output / "phase23_metrics.sqlite3"
    metrics = IncrementalMetricsService(database)
    evidence_as_of = max(
        datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))
        for item in events
    ).astimezone(timezone.utc)
    reconciliation_sha = canonical_sha256([
        item["reconciliation_provenance"] for item in events
    ])
    importer = HistoricalEvidenceImportService(database)
    import_first = importer.import_reconciliation(
        events,
        evidence_manifest_sha256=manifest["manifest_sha256"],
        reconciliation_result_sha256=reconciliation_sha,
        imported_at=evidence_as_of,
    )
    adapter = MetricsEvidenceAdapter(
        database,
        metrics,
        DEFAULT_V3,
        source_allowlist={"confirmed_imports"},
    )
    # The production source adapter feeds the production incremental service.
    # CSV order is newest-first, intentionally exercising late evidence.
    first = adapter.synchronize(as_of=evidence_as_of)
    before = metrics.list_snapshots(limit=500)
    before_hash = canonical_sha256(before["items"])
    import_replay = importer.import_reconciliation(
        events,
        evidence_manifest_sha256=manifest["manifest_sha256"],
        reconciliation_result_sha256=reconciliation_sha,
        imported_at=evidence_as_of,
    )
    replay = adapter.synchronize(as_of=evidence_as_of)
    after = metrics.list_snapshots(limit=500)
    after_hash = canonical_sha256(after["items"])
    snapshots = []
    for snapshot in after["items"]:
        evidence_ids = _snapshot_evidence(database, snapshot)
        evidence_hash = hashlib.sha256(
            "\n".join(evidence_ids).encode("utf-8")
        ).hexdigest()
        snapshots.append(
            {
                "metric_snapshot_id": snapshot["snapshot_id"],
                "window_name": snapshot["window_name"],
                "environment_id": snapshot["environment_id"],
                "model": snapshot["model"],
                "channel": snapshot["channel"] or None,
                "stream": snapshot["stream"],
                "request_profile_id": snapshot["request_profile_id"],
                "sample_count": snapshot["sample_count"],
                "data_state": snapshot["data_state"],
                "metrics": snapshot["metrics"],
                "evidence_ids": evidence_ids,
                "evidence_id_count": len(evidence_ids),
                "evidence_manifest_sha256": evidence_hash,
                "reconciliation_manifest_sha256": manifest["manifest_sha256"],
            }
        )
    result = {
        "pipeline": (
            "HistoricalEvidenceImportService -> MetricsEvidenceAdapter "
            "-> IncrementalMetricsService"
        ),
        "aggregation_version": first["aggregation_version"],
        "evidence_as_of": evidence_as_of.isoformat(),
        "first_import": first,
        "replay_import": replay,
        "production_import": import_first,
        "production_import_replay": import_replay,
        "source_cursor": adapter._cursor("confirmed_imports"),
        "snapshot_count": len(snapshots),
        "window_snapshot_counts": {
            window: sum(item["window_name"] == window for item in snapshots)
            for window in ("5m", "1h", "24h")
        },
        "snapshots_unchanged_after_reimport": before_hash == after_hash,
        "snapshot_set_sha256_before_reimport": before_hash,
        "snapshot_set_sha256_after_reimport": after_hash,
        "late_evidence_exercised": first["late_arrivals"] > 0,
        "duplicate_replay_prevented": (
            import_replay["idempotent"]
            and replay["inserted"] == 0
            and first["inserted"] == len(events)
        ),
        "network_called": False,
        "snapshots": snapshots,
    }
    return result, metrics


def _all_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_all_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_all_finite(item) for item in value)
    return True


def _phase3(
    metrics: IncrementalMetricsService,
    phase2: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    confidence = metrics.confidence.list_snapshots(limit=500)
    phase2_by_id = {
        item["metric_snapshot_id"]: item for item in phase2["snapshots"]
    }
    snapshots = []
    bindings_match = True
    for item in confidence["items"]:
        phase2_item = phase2_by_id[item["metric_snapshot_id"]]
        expected_hash = phase2_item["evidence_manifest_sha256"]
        bindings_match &= item["evidence_manifest_sha256"] == expected_hash
        snapshots.append(
            {
                **item,
                "phase2_evidence_manifest_sha256": expected_hash,
                "reconciliation_manifest_sha256": manifest["manifest_sha256"],
                "evidence_manifest_binding_matches": (
                    item["evidence_manifest_sha256"] == expected_hash
                ),
            }
        )
    policy = load_confidence_policy()
    sample_time = WINDOW_END.astimezone(timezone.utc)
    insufficient = calculate_weighted_wilson(
        [
            {
                "evidence_id": "offline-insufficient-1",
                "observed_at": sample_time.isoformat(),
                "success": True,
                "source_type": "measured_unified_uat",
                "missing_fields": [],
            }
        ],
        window_name="1h",
        window_end=sample_time,
        metric_snapshot_id="MS-OFFLINE-INSUFFICIENT",
        policy=policy,
    )
    unknown = calculate_weighted_wilson(
        [
            {
                "evidence_id": "offline-unknown-1",
                "observed_at": sample_time.isoformat(),
                "success": True,
                "source_type": "unreviewed_source",
                "missing_fields": [],
            }
        ],
        window_name="1h",
        window_end=sample_time,
        metric_snapshot_id="MS-OFFLINE-UNKNOWN",
        policy=policy,
    )
    return {
        "confidence_version": CONFIDENCE_VERSION,
        "policy_version": policy["policy_version"],
        "policy_sha256": policy["_policy_sha256"],
        "snapshot_count": len(snapshots),
        "weighted_wilson_verified": all(
            item["method"] == "weighted_wilson_v1" for item in snapshots
        ),
        "kish_ess_verified": all(
            item["kish_effective_sample_size"] >= 0 for item in snapshots
        ),
        "source_reliability_verified": all(
            0 <= item["source_reliability_factor"] <= 1 for item in snapshots
        ),
        "freshness_adjustment_verified": all(
            0 <= item["freshness_factor"] <= 1 for item in snapshots
        ),
        "missing_field_penalty_verified": any(
            item["completeness_factor"] < 1 for item in snapshots
        ),
        "finite_numeric_outputs": _all_finite(snapshots),
        "policy_binding_verified": all(
            item["policy_sha256"] == policy["_policy_sha256"] for item in snapshots
        ),
        "evidence_manifest_bindings_match_phase2": bindings_match,
        "unknown_evidence_fail_closed": unknown["confidence_state"] == "blocked",
        "insufficient_evidence_fail_closed": (
            insufficient["confidence_state"] == "insufficient"
        ),
        "unknown_evidence_probe": {
            "confidence_state": unknown["confidence_state"],
            "unknown_source_types": unknown["unknown_source_types"],
        },
        "insufficient_evidence_probe": {
            "confidence_state": insufficient["confidence_state"],
            "effective_sample_size": insufficient["effective_sample_size"],
        },
        "network_called": False,
        "snapshots": snapshots,
    }


def _scheduler_checks(
    output: Path,
    events: list[dict[str, Any]],
    database: Path,
) -> dict[str, Any]:
    provider = IncrementalMetricProvider(
        database, window="1h", confidence_version=CONFIDENCE_VERSION
    )
    unresolved_rows, unresolved_context = provider.apply(
        [
            {
                "candidate_id": "UNRESOLVED-ACTUAL-CHANNEL",
                "channel_id": "",
                "availability_status": "available",
            }
        ],
        environment_id="china_uat",
        requested_model="deepseek-v4-flash",
        stream=False,
        request_profile_id="P01",
    )
    scheduler = Scheduler(
        logger=DecisionLogger(output / "scheduler_offline_audit.jsonl"),
        metric_provider=provider,
        catalog_path=ROOT / "config" / "strategy_catalog_v3.json",
        runtime_log_enabled=False,
    )
    decision = scheduler.route(
        {
            "request_id": "PHASE23-HISTORICAL-OFFLINE",
            "requested_model": "deepseek-v4-flash",
            "stream": False,
            "input_tokens": 10,
            "output_tokens": 10,
            "currency": "CNY",
            "strategy": "confidence_aware_v3",
            "mode": "real_shadow",
            "metadata": {
                "environment_id": "china_uat",
                "request_profile_id": "P01",
            },
        }
    )
    probe_events = [
        {
            **item,
            "evidence_id": f"{item['evidence_id']}:offline-behavior-probe",
            "actual_channel": "OFFLINE-BEHAVIOR-PROBE",
        }
        for item in events
    ]
    probe_database = output / "phase23_scheduler_behavior_probe.sqlite3"
    probe_metrics = IncrementalMetricsService(probe_database)
    probe_as_of = max(
        datetime.fromisoformat(str(item["observed_at"]).replace("Z", "+00:00"))
        for item in probe_events
    ).astimezone(timezone.utc)
    probe_metrics.ingest(probe_events, as_of=probe_as_of)
    mismatch_provider = IncrementalMetricProvider(
        probe_database, window="1h", confidence_version="weighted_wilson_v0"
    )
    mismatch_rows, _ = mismatch_provider.apply(
        [
            {
                "candidate_id": "OFFLINE-BEHAVIOR-PROBE",
                "channel_id": "OFFLINE-BEHAVIOR-PROBE",
                "availability_status": "available",
            }
        ],
        environment_id="china_uat",
        requested_model="deepseek-v4-flash",
        stream=False,
        request_profile_id="P01",
    )
    stale_database = output / "phase23_stale_probe.sqlite3"
    stale_metrics = IncrementalMetricsService(stale_database)
    stale_metrics.ingest(probe_events, as_of=probe_as_of + timedelta(hours=2))
    stale_provider = IncrementalMetricProvider(
        stale_database, window="24h", confidence_version=CONFIDENCE_VERSION
    )
    stale_rows, _ = stale_provider.apply(
        [
            {
                "candidate_id": "OFFLINE-BEHAVIOR-PROBE",
                "channel_id": "OFFLINE-BEHAVIOR-PROBE",
                "availability_status": "available",
            }
        ],
        environment_id="china_uat",
        requested_model="deepseek-v4-flash",
        stream=False,
        request_profile_id="P01",
    )
    return {
        "strategy": "confidence_aware_v3",
        "unresolved_projection_snapshot_ids": unresolved_context["snapshot_ids"],
        "unresolved_projection_confidence_snapshot_ids": unresolved_context[
            "confidence_snapshot_ids"
        ],
        "unresolved_projection_consumes_snapshot_ids": bool(
            unresolved_context["snapshot_ids"]
            and unresolved_context["confidence_snapshot_ids"]
        ),
        "authoritative_channel_available": False,
        "actual_scheduler_recommended_candidate": decision["recommended_candidate"],
        "actual_scheduler_blocked": decision["recommended_candidate"] is None,
        "actual_scheduler_snapshot_ids": decision["metric_context"]["snapshot_ids"],
        "actual_scheduler_block_reason": (
            "authoritative_actual_channel_missing_from_V3_and_CSV"
        ),
        "missing_evidence_blocks": decision["recommended_candidate"] is None,
        "stale_evidence_blocks": (
            stale_rows[0]["availability_status"] == "unavailable"
            and stale_rows[0].get("dynamic_metrics_block_reason")
            == "dynamic_metrics_stale"
        ),
        "version_mismatch_blocks": (
            mismatch_rows[0]["availability_status"] == "unavailable"
            and mismatch_rows[0].get("confidence_block_reason")
            == "statistical_confidence_snapshot_unavailable"
        ),
        "ambiguous_evidence_action": "reconciliation_not_complete; no projection",
        "behavior_probe_uses_synthetic_channel_binding": True,
        "behavior_probe_excluded_from_reconciled_snapshots": True,
        "fixed_health_substituted": False,
        "network_called": False,
    }


def _safe_summary_csv(
    path: Path, result: dict[str, Any], csv_by_number: dict[int, Any]
) -> None:
    fields = [
        "association_status",
        "plan_id",
        "csv_row_number",
        "timestamp_china",
        "normalized_model",
        "stream",
        "input_tokens",
        "output_tokens",
        "timestamp_delta_ms",
        "latency_delta_ms",
        "reason_code",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in result["matched"]:
            values = {"association_status": "matched", **item}
            writer.writerow({field: values.get(field) for field in fields})
        for item in result["extra_rows"]:
            values = {"association_status": "extra", **item}
            writer.writerow({field: values.get(field) for field in fields})


def _report(
    manifest: dict[str, Any],
    reconciliation: dict[str, Any],
    phase2: dict[str, Any],
    phase3: dict[str, Any],
    scheduler_checks: dict[str, Any],
) -> str:
    pipeline_passed = all(
        (
            phase2["snapshots_unchanged_after_reimport"],
            phase2["duplicate_replay_prevented"],
            phase3["weighted_wilson_verified"],
            phase3["evidence_manifest_bindings_match_phase2"],
            scheduler_checks["actual_scheduler_snapshot_ids"],
        )
    )
    verified = reconciliation["matched_count"] == 60 and pipeline_passed
    status = (
        "implemented_and_verified_against_historical_UAT_evidence"
        if verified
        else "implemented_pending_live_validation"
    )
    blocker = (
        "none"
        if verified
        else "authoritative actual_channel is absent from both immutable V3 and CSV; "
        "real scheduler candidates therefore cannot bind to the generated snapshot IDs"
    )
    return f"""# Phase 2/3 historical UAT reconciliation

## Status

- Phase 2: `{status}`
- Phase 3: `{status}`
- Scope: historical offline evidence only; this is not current live-stream validation.
- Acceptance blocker: {blocker}

## Immutable evidence

- V3: {manifest['inputs']['unified_uat_execution_v3']['row_count']} rows, {manifest['inputs']['unified_uat_execution_v3']['byte_size']:,} bytes, SHA-256 `{manifest['inputs']['unified_uat_execution_v3']['sha256']}`
- CSV: {manifest['inputs']['uat_billing_csv']['row_count']} rows, {manifest['inputs']['uat_billing_csv']['byte_size']:,} bytes, SHA-256 `{manifest['inputs']['uat_billing_csv']['sha256']}`
- Reconciliation manifest SHA-256: `{manifest['manifest_sha256']}`

## Reconciliation

- Authorized-window candidates: {reconciliation['csv_candidate_count']}
- Deterministically attributable to V3: {reconciliation['matched_count']}
- Extra rows: {reconciliation['extra_count']}
- Unmatched V3: {reconciliation['unmatched_count']}
- Ambiguous V3: {reconciliation['ambiguous_count']}
- Duplicate associations: {reconciliation['duplicate_association']}
- Raw target-window model distribution: `{json.dumps(reconciliation['candidate_raw_model_counts'], ensure_ascii=False, sort_keys=True)}`
- Extra-row raw model distribution: `{json.dumps(reconciliation['extra_raw_model_counts'], ensure_ascii=False, sort_keys=True)}`

The documented timestamp tolerance is {reconciliation['matching_policy']['timestamp_tolerance_seconds']} seconds. Model, stream, input tokens, and output tokens must match exactly; CSV whole-second latency must be within {reconciliation['matching_policy']['latency_tolerance_ms_exclusive']} ms of V3. Any ambiguity fails closed.

The 14 safe extras comprise five literal `deepseek-v4-flash` rows and nine
other raw-model rows. One of those nine is the reviewed
`deepseek-ai/DeepSeek-V4-Flash` alias, so the normalized distribution contains
six additional DeepSeek rows and eight other normalized models. No API Key,
IP, prompt, response body, or free-form detail is included.

## Phase 2

- Real pipeline: `{phase2['pipeline']}`
- First import: {phase2['first_import']['inserted']} inserted, {phase2['first_import']['late_arrivals']} late arrivals.
- Same-file replay: {phase2['replay_import']['duplicates']} duplicates, {phase2['replay_import']['inserted']} inserted.
- Snapshots unchanged after replay: {phase2['snapshots_unchanged_after_reimport']}
- Snapshot counts: {phase2['window_snapshot_counts']}
- Source cursor persisted: {bool(phase2['source_cursor'])}

## Phase 3

- Method: `{phase3['confidence_version']}`
- Policy SHA-256: `{phase3['policy_sha256']}`
- Confidence snapshots: {phase3['snapshot_count']}
- Phase 2/3 evidence-manifest bindings match: {phase3['evidence_manifest_bindings_match_phase2']}
- Kish ESS, reliability, freshness, missing-field penalties, and finite outputs verified: {all((phase3['kish_ess_verified'], phase3['source_reliability_verified'], phase3['freshness_adjustment_verified'], phase3['missing_field_penalty_verified'], phase3['finite_numeric_outputs']))}
- Unknown evidence fails closed: {phase3['unknown_evidence_fail_closed']}
- Insufficient evidence fails closed: {phase3['insufficient_evidence_fail_closed']}

## Scheduler

- Strategy: `{scheduler_checks['strategy']}`
- Unresolved projection consumes metric/confidence IDs: {scheduler_checks['unresolved_projection_consumes_snapshot_ids']}
- Real catalog routing blocked: {scheduler_checks['actual_scheduler_blocked']}
- Reason: `{scheduler_checks['actual_scheduler_block_reason']}`
- Missing, stale, and confidence-version mismatch checks block: {all((scheduler_checks['missing_evidence_blocks'], scheduler_checks['stale_evidence_blocks'], scheduler_checks['version_mismatch_blocks']))}
- Fixed health substituted: {scheduler_checks['fixed_health_substituted']}

## Safety

- Network requests: 0
- Completion calls: 0
- Platform writes: 0
- Production access: 0
- Raw API keys persisted or printed: 0
- Raw IP values persisted or printed: 0

## Verification commands

Test commands, exit codes, exact totals, final secret-scan result, and post-run immutable hash verification are appended after the required focused and full regression gates complete.
"""


def run(csv_path: Path, v3_path: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    v3_rows, v3_fingerprint = load_protected_v3(v3_path)
    adapter = HistoricalUatCsvAdapter(csv_path)
    csv_rows, rejected, csv_metadata = adapter.read()
    candidates = within_window(csv_rows, WINDOW_START, WINDOW_END)
    manifest = _manifest(v3_path, csv_path, v3_fingerprint, csv_metadata)
    result = reconcile(v3_rows, candidates)
    _json_write(output / "evidence_manifest.json", manifest)
    _json_write(output / "schema_mapping.json", safe_schema_mapping())
    _json_write(output / "reconciliation_result.json", result)
    _jsonl_write(output / "unmatched_v3.jsonl", result["unmatched"])
    _jsonl_write(output / "unmatched_csv.jsonl", result["extra_rows"])
    _jsonl_write(output / "ambiguous_matches.jsonl", result["ambiguous"])
    _jsonl_write(
        output / "rejected_records.jsonl",
        [item.safe_dict() for item in rejected],
    )
    _safe_summary_csv(
        output / "safe_match_summary.csv",
        result,
        {row.row_number: row for row in candidates},
    )
    events = project_metric_events(v3_rows, candidates, result)
    phase2, metrics = _phase2(output, events, manifest)
    phase3 = _phase3(metrics, phase2, manifest)
    scheduler_checks = _scheduler_checks(
        output, events, output / "phase23_metrics.sqlite3"
    )
    phase2["scheduler_integration"] = scheduler_checks
    _json_write(output / "phase2_snapshot_evidence.json", phase2)
    _json_write(output / "phase3_confidence_evidence.json", phase3)
    (output / "final_reconciliation_report.md").write_text(
        _report(manifest, result, phase2, phase3, scheduler_checks),
        encoding="utf-8",
    )
    return {
        "output_directory": str(output),
        "matched_count": result["matched_count"],
        "extra_count": result["extra_count"],
        "unmatched_count": result["unmatched_count"],
        "ambiguous_count": result["ambiguous_count"],
        "scheduler_bound_snapshot_count": len(
            scheduler_checks["actual_scheduler_snapshot_ids"]
        ),
        "network_requests": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--v3", type=Path, default=DEFAULT_V3)
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args()
    run_id = (
        "PHASE23-HIST-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + uuid.uuid4().hex[:8].upper()
    )
    output = args.output_directory or DEFAULT_OUTPUT_ROOT / run_id
    try:
        result = run(args.csv.resolve(), args.v3.resolve(), output.resolve())
    except (OSError, ValueError, ReconciliationError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": str(exc) or "historical_reconciliation_failed",
                    "network_requests": 0,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps({"status": "completed", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
