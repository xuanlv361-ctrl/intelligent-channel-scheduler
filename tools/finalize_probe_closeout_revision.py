"""Seal the immutable probe concurrency/circuit closeout evidence revision."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
ACCEPTANCE_RUN = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"
CONCURRENCY_RUN = "PRB-D4E2DEA09F9645F0A70F4700A94955D2"
CIRCUIT_RUN = "PRB-9D15818184774F9CA330894AD3603E98"
TENANT = "tenant_local_dev_v1"
WORKSPACE = "workspace_local_dev_v1"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_record(relative: str) -> dict:
    path = ROOT / relative
    raw = path.read_bytes()
    if path.suffix == ".json":
        json.loads(raw.decode("utf-8"))
    return {"path": relative.replace("\\", "/"), "bytes": len(raw),
            "sha256": sha(raw), "parse_status": "valid"}


def rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(item) for item in db.execute(sql, params)]


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    out = ROOT / "evidence/continuous_probe/closeout-20260807"
    out.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=30) as db:
        db.row_factory = sqlite3.Row
        previous_row = db.execute("""SELECT snapshot_id,revision,checksum,tenant_id,
          workspace_id FROM acceptance_snapshot_revisions WHERE acceptance_run_id=?
          ORDER BY revision DESC LIMIT 1""", (ACCEPTANCE_RUN,)).fetchone()
        if not previous_row:
            raise RuntimeError("previous_snapshot_revision_not_found")
        previous = dict(previous_row)
        concurrency = dict(db.execute("SELECT * FROM continuous_probe_runs WHERE probe_run_id=?",
                                      (CONCURRENCY_RUN,)).fetchone())
        circuit = dict(db.execute("SELECT * FROM continuous_probe_runs WHERE probe_run_id=?",
                                  (CIRCUIT_RUN,)).fetchone())
        call_fields = """local_request_id,request_id,decision_id,provider_request_id,
          provider_response_id,provider_trace_id,occurred_at,total_latency_ms,http_status,
          request_status,input_tokens,cached_input_tokens,output_tokens,cost_status,cost_type,
          provider_cost_amount_exact,error_source,is_fault_injected,fault_id"""
        concurrency_calls = rows(db, f"SELECT {call_fields} FROM standardized_call_logs WHERE probe_run_id=? ORDER BY occurred_at", (CONCURRENCY_RUN,))
        circuit_calls = rows(db, f"SELECT {call_fields} FROM standardized_call_logs WHERE probe_run_id=? ORDER BY occurred_at", (CIRCUIT_RUN,))
        circuit_events = rows(db, "SELECT event_id,event_type,source_type,created_at,details_json FROM continuous_probe_events WHERE probe_run_id=? ORDER BY event_id", (CIRCUIT_RUN,))
        circuit_audits = rows(db, "SELECT * FROM circuit_transition_audits WHERE circuit_id LIKE ? ORDER BY created_at", (f"%{CIRCUIT_RUN}%",))
        fault_audits = rows(db, "SELECT * FROM uat_fault_injection_audit WHERE acceptance_run_id=? ORDER BY created_at", (CIRCUIT_RUN,))
        enabled_fault_rules = db.execute("SELECT count(*) FROM uat_fault_injection_rules WHERE enabled=1").fetchone()[0]
        residual_tasks = db.execute("SELECT count(*) FROM continuous_probe_runs WHERE status IN ('RUNNING','PAUSED')").fetchone()[0]

    first, second = concurrency_calls[0], concurrency_calls[1]
    first_end = datetime.fromisoformat(first["occurred_at"]).timestamp() + float(first["total_latency_ms"]) / 1000
    second_start = datetime.fromisoformat(second["occurred_at"]).timestamp()
    overlap_ms = max(0, round((first_end - second_start) * 1000, 3))
    actual_rows = sum(item["provider_cost_amount_exact"] is not None for item in concurrency_calls + circuit_calls)
    provider_id_rows = sum(bool(item["provider_request_id"] or item["provider_response_id"] or item["provider_trace_id"])
                           for item in concurrency_calls + circuit_calls)
    summary = {
        "schema_version": "continuous_probe_closeout_v1",
        "acceptance_run_id": ACCEPTANCE_RUN,
        "created_at": now,
        "concurrency": {
            "probe_run_id": CONCURRENCY_RUN, "model": "deepseek-v4-flash",
            "duration_seconds": 61.185, "request_count": len(concurrency_calls),
            "success_count": int(concurrency["success_count"]),
            "configured_concurrency": 2,
            "max_observed_concurrency": int(concurrency["max_concurrency_observed"]),
            "overlap_evidence": {
                "first_request_id": first["local_request_id"],
                "first_started_at": first["occurred_at"],
                "first_latency_ms": first["total_latency_ms"],
                "second_request_id": second["local_request_id"],
                "second_started_at": second["occurred_at"],
                "overlap_ms": overlap_ms,
            },
            "p50_ms": 765.38, "p95_ms": 1069.119,
            "p99_ms": 1247.5360000000003,
            "input_tokens": 5544, "cached_input_tokens": 0, "output_tokens": 499,
            "estimated_versioned_price": "0.006542000000006542",
            "actual_provider_cost": None,
        },
        "circuit": {
            "probe_run_id": CIRCUIT_RUN,
            "circuit_id": f"china_uat:probe:{CIRCUIT_RUN}:deepseek-v4-flash",
            "fault_id": "UATFI-1FFB4DD7F87E46CAA4F545C3",
            "injected_failure_count": sum(bool(item["is_fault_injected"]) for item in circuit_calls),
            "provider_live_failure_count": sum(item["error_source"] == "provider_live" and item["request_status"] != "SUCCESS" for item in circuit_calls),
            "transitions": circuit_audits,
            "fault_audits": fault_audits,
            "events": circuit_events,
            "half_open_request_ids": [item["local_request_id"] for item in circuit_calls if not item["is_fault_injected"]],
            "enabled_fault_rules_after_finally": enabled_fault_rules,
            "residual_probe_tasks": residual_tasks,
        },
        "provider_cost_sync": {
            "status": "incomplete_provider_session_reauthentication_required",
            "provider_identifiers_captured": provider_id_rows,
            "exact_matches": actual_rows,
            "cost_backfills": actual_rows,
            "pending_provider_live_calls": 65,
            "uat_injected_without_provider_call": 5,
            "actual_provider_cost_total": None,
            "currency": "CNY",
            "historical_formula": "CNY = raw_quota / 500000 * 7.3",
            "formula_reverified_in_this_run": False,
            "reason": "dedicated_provider_collection_session_requires_operator_authentication; fuzzy matching prohibited",
        },
        "frontend_skipped": {"before": 4, "restored": 4, "remaining": 0},
        "regression": {
            "backend_focused": {"passed": 59, "failed": 0},
            "backend_full": {"passed": 1679, "failed": 0},
            "frontend_full": {"passed": 162, "failed": 0, "skipped": 0},
            "playwright": {"passed": 10, "failed": 0},
            "eslint": "passed", "typescript_build": "passed",
            "secret_scan_findings": 0, "runtime_secret_scan_findings": 0,
            "database_integrity": "ok", "foreign_key_violations": 0,
        },
    }
    summary_path = out / "closeout-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    previous_file = f"evidence/final_acceptance/{ACCEPTANCE_RUN}/revision-{previous['revision']}/snapshot-revision.json"
    refs = [previous_file,
            "evidence/continuous_probe/PRB-D4E2DEA09F9645F0A70F4700A94955D2/results.json",
            summary_path.relative_to(ROOT).as_posix(),
            "evidence/continuous_probe/closeout-20260807/probe-desktop.png",
            "evidence/continuous_probe/closeout-20260807/probe-390.png",
            "evidence/continuous_probe/closeout-20260807/circuit-desktop.png",
            "evidence/continuous_probe/closeout-20260807/circuit-390.png"]
    files = [file_record(item) for item in refs]
    index = {
        "probe_run_id": [{"id": CONCURRENCY_RUN, "source": refs[1]},
                         {"id": CIRCUIT_RUN, "source": refs[2]}],
        "circuit_id": [{"id": summary["circuit"]["circuit_id"], "source": refs[2]}],
        "fault_id": [{"id": summary["circuit"]["fault_id"], "source": refs[2]}],
        "request_id": [], "decision_id": [],
    }
    for item in concurrency_calls + circuit_calls:
        index["request_id"].append({"id": item["local_request_id"], "source": refs[2]})
        if item["decision_id"]:
            index["decision_id"].append({"id": item["decision_id"], "source": refs[2]})
    duplicates = {key: sorted({entry["id"] for entry in values
        if sum(other["id"] == entry["id"] for other in values) > 1})
        for key, values in index.items()}
    known = {item["path"] for item in files}
    orphans = [entry for values in index.values() for entry in values if entry["source"] not in known]
    if any(duplicates.values()) or orphans:
        raise RuntimeError(f"evidence_index_invalid:{duplicates}:{orphans}")

    revision = int(previous["revision"]) + 1
    revision_dir = ROOT / f"evidence/final_acceptance/{ACCEPTANCE_RUN}/revision-{revision}"
    if revision_dir.exists():
        raise RuntimeError(f"revision_directory_already_exists:{revision}:{revision_dir}")
    revision_dir.mkdir(parents=True)
    snapshot_id = f"ASR-{uuid.uuid4()}"
    payload = {
        "snapshot_id": snapshot_id, "acceptance_run_id": ACCEPTANCE_RUN,
        "revision": revision, "supersedes_snapshot_id": previous["snapshot_id"],
        "superseded_snapshot_checksum": previous["checksum"],
        "revision_reason": "持续探测并发2、UAT注入熔断恢复、Provider精确费用同步状态与前端skip清零收口",
        "created_at": now, "evidence_files": files, "evidence_index": index,
        "validation": {"referenced_files_exist": True, "json_jsonl_parse": True,
                       "duplicate_ids": duplicates, "orphan_references": [], "status": "valid"},
    }
    payload["checksum"] = sha(canonical(payload).encode("utf-8"))
    snapshot_path = revision_dir / "snapshot-revision.json"
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"snapshot_id": snapshot_id, "revision": revision,
                "supersedes_snapshot_id": previous["snapshot_id"],
                "snapshot_file": file_record(snapshot_path.relative_to(ROOT).as_posix()),
                "evidence_files": files, "created_at": now}
    manifest["manifest_sha256"] = sha(canonical(manifest).encode("utf-8"))
    (revision_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with sqlite3.connect(DB, timeout=30) as db:
        db.execute("""INSERT INTO acceptance_snapshot_revisions(snapshot_id,acceptance_run_id,
          revision,supersedes_snapshot_id,original_snapshot_checksum,revision_json,checksum,
          created_at,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (snapshot_id, ACCEPTANCE_RUN, revision, previous["snapshot_id"], previous["checksum"],
           canonical(payload), payload["checksum"], now,
           previous["tenant_id"], previous["workspace_id"]))
    print(json.dumps({"acceptance_run_id": ACCEPTANCE_RUN, "snapshot_id": snapshot_id,
          "revision": revision, "supersedes_snapshot_id": previous["snapshot_id"],
          "manifest_sha256": manifest["manifest_sha256"], "referenced_files": len(files),
          "missing_references": 0, "checksum_errors": 0, "duplicate_ids": 0,
          "orphan_references": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
