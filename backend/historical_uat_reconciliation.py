"""Deterministic, one-to-one reconciliation of immutable V3 and UAT CSV data."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.historical_uat_csv_adapter import (
    CHINA_ZONE,
    CsvEvidenceRow,
    REVIEWED_MODEL_ALIASES,
)
from backend.metrics_evidence_adapter import (
    PROTECTED_V3_BYTES,
    PROTECTED_V3_ROWS,
    PROTECTED_V3_SHA256,
)

TIMESTAMP_TOLERANCE_SECONDS = 2.0
LATENCY_TOLERANCE_MS = 1000.0


class ReconciliationError(ValueError):
    """Stable fail-closed reconciliation error."""


@dataclass(frozen=True)
class V3Evidence:
    item: dict[str, Any]
    started_china: str
    completed_china: str

    @property
    def plan_id(self) -> str:
        return str(self.item["plan_id"])


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def load_protected_v3(path: Path) -> tuple[list[V3Evidence], dict[str, Any]]:
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest().upper()
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    fingerprint = {
        "row_count": len(lines),
        "byte_size": len(raw),
        "sha256": digest,
    }
    if fingerprint != {
        "row_count": PROTECTED_V3_ROWS,
        "byte_size": PROTECTED_V3_BYTES,
        "sha256": PROTECTED_V3_SHA256,
    }:
        raise ReconciliationError("protected_v3_integrity_mismatch")
    rows: list[V3Evidence] = []
    seen: set[str] = set()
    for line in lines:
        item = json.loads(line)
        plan_id = str(item.get("plan_id") or "")
        if not plan_id or plan_id in seen:
            raise ReconciliationError("invalid_or_duplicate_v3_plan_id")
        seen.add(plan_id)
        try:
            started = datetime.fromisoformat(
                str(item["started_at"]).replace("Z", "+00:00")
            )
            completed = datetime.fromisoformat(
                str(item["completed_at"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            raise ReconciliationError("invalid_v3_timestamp") from exc
        if started.tzinfo is None or completed.tzinfo is None:
            raise ReconciliationError("v3_timestamp_timezone_missing")
        rows.append(
            V3Evidence(
                item=item,
                started_china=started.astimezone(CHINA_ZONE).isoformat(),
                completed_china=completed.astimezone(CHINA_ZONE).isoformat(),
            )
        )
    return rows, fingerprint


def _normalized_v3_model(item: dict[str, Any]) -> str | None:
    model = str(item.get("actual_model") or item.get("requested_model") or "").strip()
    return REVIEWED_MODEL_ALIASES.get(model)


def _edge(v3: V3Evidence, csv_row: CsvEvidenceRow) -> dict[str, Any] | None:
    item = v3.item
    expected_event_type = (
        "consumption"
        if isinstance(item.get("http_status"), int)
        and 200 <= int(item["http_status"]) < 400
        and not item.get("error_category")
        else "error"
    )
    if csv_row.event_type != expected_event_type:
        return None
    model = _normalized_v3_model(item)
    if not model or model != csv_row.normalized_model:
        return None
    if bool(item.get("stream")) != csv_row.stream:
        return None
    try:
        if int(item["input_tokens"]) != csv_row.input_tokens:
            return None
        if int(item["output_tokens"]) != csv_row.output_tokens:
            return None
        v3_latency = float(item["latency_ms"])
    except (KeyError, TypeError, ValueError):
        return None
    completed = datetime.fromisoformat(str(item["completed_at"]).replace("Z", "+00:00"))
    csv_time = datetime.fromisoformat(csv_row.timestamp_utc)
    timestamp_delta_ms = abs(
        (csv_time.astimezone(timezone.utc) - completed.astimezone(timezone.utc))
        .total_seconds()
        * 1000
    )
    latency_delta_ms = abs(csv_row.latency_ms - v3_latency)
    if timestamp_delta_ms > TIMESTAMP_TOLERANCE_SECONDS * 1000:
        return None
    if latency_delta_ms >= LATENCY_TOLERANCE_MS:
        return None
    return {
        "plan_id": v3.plan_id,
        "execution_id": item.get("execution_id"),
        "csv_row_number": csv_row.row_number,
        "timestamp_delta_ms": round(timestamp_delta_ms, 6),
        "latency_delta_ms": round(latency_delta_ms, 6),
        "timestamp_china": csv_row.timestamp_china,
        "normalized_model": csv_row.normalized_model,
        "stream": csv_row.stream,
        "input_tokens": csv_row.input_tokens,
        "output_tokens": csv_row.output_tokens,
        "completion_status": csv_row.event_type,
        "match_reason": "unique_full_signature_with_bounded_completion_timestamp",
        "score_components": {
            "timestamp_within_tolerance": True,
            "normalized_model_exact": True,
            "stream_exact": True,
            "input_tokens_exact": True,
            "output_tokens_exact": True,
            "completion_status_exact": True,
            "latency_within_tolerance": True,
        },
    }


def reconcile(
    v3_rows: list[V3Evidence], csv_rows: list[CsvEvidenceRow]
) -> dict[str, Any]:
    by_plan: dict[str, list[dict[str, Any]]] = {}
    by_csv: dict[int, list[dict[str, Any]]] = {}
    for v3 in v3_rows:
        for csv_row in csv_rows:
            candidate = _edge(v3, csv_row)
            if candidate:
                by_plan.setdefault(v3.plan_id, []).append(candidate)
                by_csv.setdefault(csv_row.row_number, []).append(candidate)
    matched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for v3 in v3_rows:
        edges = by_plan.get(v3.plan_id, [])
        if len(edges) == 1 and len(by_csv[edges[0]["csv_row_number"]]) == 1:
            matched.append(edges[0])
        elif not edges:
            unmatched.append(
                {"plan_id": v3.plan_id, "reason_code": "no_compatible_csv_row"}
            )
        else:
            ambiguous.append(
                {
                    "plan_id": v3.plan_id,
                    "reason_code": "ambiguous_compatible_csv_rows",
                    "candidate_csv_row_numbers": sorted(
                        edge["csv_row_number"] for edge in edges
                    ),
                }
            )
    matched_csv = {item["csv_row_number"] for item in matched}
    conflicted_csv = {
        number for number, edges in by_csv.items() if len(edges) > 1
    }
    extras = [
        {
            "csv_row_number": row.row_number,
            "timestamp_china": row.timestamp_china,
            "normalized_model": row.normalized_model,
            "raw_model": row.raw_model,
            "stream": row.stream,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "reason_code": (
                "ambiguous_association"
                if row.row_number in conflicted_csv
                else "not_attributable_to_v3"
            ),
        }
        for row in csv_rows
        if row.row_number not in matched_csv
    ]
    duplicate_association = (
        len({item["plan_id"] for item in matched}) != len(matched)
        or len(matched_csv) != len(matched)
    )
    accepted_deltas = [
        float(item["timestamp_delta_ms"]) for item in matched
    ]
    timestamp_semantic_proof = {
        "evaluated_v3_count": len(v3_rows),
        "accepted_pair_count": len(matched),
        "basis": "csv_time_matches_v3_completed_at_not_started_at",
        "maximum_completion_delta_ms": (
            max(accepted_deltas) if accepted_deltas else None
        ),
        "csv_precision": "one_second",
        "all_accepted_pairs_unique_inside_tolerance": (
            len(matched) == len({item["csv_row_number"] for item in matched})
            and not ambiguous
        ),
    }
    return {
        "status": (
            "matched"
            if len(matched) == len(v3_rows)
            and not ambiguous
            and not unmatched
            and not duplicate_association
            else "pending"
        ),
        "matching_policy": {
            "timestamp_basis": "CSV timestamp versus V3 completed_at",
            "timestamp_tolerance_seconds": TIMESTAMP_TOLERANCE_SECONDS,
            "timestamp_tolerance_reason": (
                "CSV exports seconds while V3 preserves sub-second completion time; "
                "bounded log-materialization skew is allowed and ambiguity is rejected"
            ),
            "required_exact_fields": [
                "normalized_model",
                "stream",
                "input_tokens",
                "output_tokens",
                "completion_status",
            ],
            "latency_tolerance_ms_exclusive": LATENCY_TOLERANCE_MS,
            "latency_tolerance_reason": "CSV total latency is exported in whole seconds",
            "nearest_candidate_forced": False,
            "ambiguity_action": "reject",
            "policy_version": "historical_uat_v3_csv_match_v2",
        },
        "timestamp_semantic_proof": timestamp_semantic_proof,
        "v3_count": len(v3_rows),
        "csv_candidate_count": len(csv_rows),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "ambiguous_count": len(ambiguous),
        "extra_count": len(extras),
        "candidate_raw_model_counts": dict(sorted(Counter(
            row.raw_model for row in csv_rows).items())),
        "candidate_normalized_model_counts": dict(sorted(Counter(
            row.normalized_model for row in csv_rows).items())),
        "extra_raw_model_counts": dict(sorted(Counter(
            row.raw_model for row in csv_rows
            if row.row_number not in matched_csv).items())),
        "duplicate_association": duplicate_association,
        "matched": sorted(matched, key=lambda item: item["plan_id"]),
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "extra_rows": extras,
    }


def project_metric_events(
    v3_rows: list[V3Evidence],
    csv_rows: list[CsvEvidenceRow],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    if result.get("status") != "matched":
        raise ReconciliationError("reconciliation_not_complete")
    v3_by_plan = {row.plan_id: row.item for row in v3_rows}
    csv_by_number = {row.row_number: row for row in csv_rows}
    events = []
    for match in sorted(result["matched"], key=lambda item: item["csv_row_number"]):
        v3 = v3_by_plan[match["plan_id"]]
        csv_row = csv_by_number[match["csv_row_number"]]
        events.append(
            {
                "evidence_id": (
                    f"historical_uat:{v3['execution_id']}:csv-{csv_row.row_number}"
                ),
                "environment_id": "china_uat",
                "requested_model": v3.get("requested_model"),
                "actual_model": v3.get("actual_model"),
                # The export and V3 evidence contain no authoritative channel.
                "actual_channel": None,
                "request_profile_id": v3.get("request_profile_id"),
                "observed_at": v3.get("completed_at"),
                "http_status": v3.get("http_status"),
                "latency_ms": v3.get("latency_ms"),
                "ttft_ms": v3.get("ttft_ms"),
                "stream": v3.get("stream"),
                "sse_complete": v3.get("sse_complete"),
                "actual_cost": csv_row.cost_cny,
                "currency": "CNY",
                "source_type": "measured_unified_uat",
                "source_reliability": 0.9,
                "reconciliation_provenance": {
                    "plan_id": v3.get("plan_id"),
                    "execution_id": v3.get("execution_id"),
                    "csv_row_number": csv_row.row_number,
                },
            }
        )
    return events
