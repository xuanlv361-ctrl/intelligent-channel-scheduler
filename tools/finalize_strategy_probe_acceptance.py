"""Seal immutable evidence for the live strategy-effect and five-minute probe runs."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.live_acceptance_service import LiveAcceptanceService
from backend.tenant_security import TenantScope
DB = Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
ACCEPTANCE_RUN = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"
STRATEGY_RUN = "SER-CB9D1956D49B4A5788D15582014AF4F6"
PROBE_RUN = os.environ.get("PROBE_RUN_ID", "").strip()


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_record(relative: str) -> dict:
    path = ROOT / relative
    raw = path.read_bytes()
    if path.suffix == ".json":
        json.loads(raw.decode("utf-8"))
    elif path.suffix == ".jsonl":
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                json.loads(line)
    return {"path": relative.replace("\\", "/"), "bytes": len(raw),
            "sha256": digest(raw), "parse_status": "valid"}


def main() -> None:
    if not PROBE_RUN:
        raise RuntimeError("PROBE_RUN_ID must identify the persisted probe run to seal")
    scope = TenantScope.local_development()
    service = LiveAcceptanceService(DB, scope)
    strategy = service.strategy_run(STRATEGY_RUN)
    strategy_summary = service.summarize_strategy(STRATEGY_RUN)
    probe = service.probe_run(PROBE_RUN)
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        probe_logs = [dict(row) for row in db.execute("""SELECT request_id,response_id,
          decision_id,requested_model,actual_model,request_status,http_status,total_latency_ms,
          first_token_latency_ms,input_tokens,cached_input_tokens,output_tokens,cost_amount,
          cost_type,cost_status,provider_cost_amount_exact,error_source,is_fault_injected,
          provider_sync_failure_reason FROM standardized_call_logs WHERE probe_run_id=?
          ORDER BY cursor_id""", (PROBE_RUN,))]
        latest_batch = dict(db.execute("""SELECT * FROM provider_log_sync_batches
          WHERE finished_at IS NOT NULL AND pages_read>0
          ORDER BY pages_read DESC,finished_at DESC LIMIT 1""").fetchone())
        previous = db.execute("""SELECT snapshot_id,revision,checksum FROM
          acceptance_snapshot_revisions WHERE acceptance_run_id=? AND tenant_id=?
          AND workspace_id=? ORDER BY revision DESC LIMIT 1""",
          (ACCEPTANCE_RUN, *scope.sql_parameters())).fetchone()
        if not previous:
            raise RuntimeError("previous_snapshot_revision_not_found")

    strategy_dir = ROOT / "evidence/strategy_effect" / STRATEGY_RUN
    probe_dir = ROOT / "evidence/continuous_probe" / PROBE_RUN
    strategy_dir.mkdir(parents=True, exist_ok=True); probe_dir.mkdir(parents=True, exist_ok=True)
    strategy_file = strategy_dir / "post-sync-summary.json"
    strategy_file.write_text(json.dumps({
        "strategy_effect_run_id": STRATEGY_RUN, "summary": strategy_summary,
        "actual_provider_cost_status": "pending_provider_sync",
        "exact_provider_cost_matches": sum(item.get("actual_provider_cost") is not None for item in strategy["items"]),
        "provider_sync_batch_id": latest_batch["sync_batch_id"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    probe_file = probe_dir / "post-sync-summary.json"
    probe_file.write_text(json.dumps({
        "probe_run_id": PROBE_RUN, "run": probe, "items": probe_logs,
        "actual_provider_cost_status": "pending_provider_sync",
        "exact_provider_cost_matches": sum(item.get("provider_cost_amount_exact") is not None for item in probe_logs),
        "provider_sync_batch_id": latest_batch["sync_batch_id"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    provider_file = f"evidence/provider_cost_sync/{latest_batch['sync_job_id']}.json"
    provider_path = ROOT / provider_file
    provider_path.parent.mkdir(parents=True, exist_ok=True)
    if not provider_path.exists():
        provider_path.write_text(json.dumps(latest_batch, ensure_ascii=False, indent=2), encoding="utf-8")
    previous_file = f"evidence/final_acceptance/{ACCEPTANCE_RUN}/revision-{previous['revision']}/snapshot-revision.json"
    refs = [previous_file, strategy_file.relative_to(ROOT).as_posix(),
            probe_file.relative_to(ROOT).as_posix(), provider_file,
            "evidence/regression/20260806/strategy-probe-final.json",
            "docs/screenshots/live-acceptance-20260806-final/strategy-effect-desktop.png",
            "docs/screenshots/live-acceptance-20260806-final/strategy-effect-mobile-390.png",
            "docs/screenshots/live-acceptance-20260806-final/continuous-probe-desktop.png",
            "docs/screenshots/live-acceptance-20260806-final/continuous-probe-mobile-390.png"]
    files = [file_record(path) for path in refs]
    index = {name: [] for name in ("strategy_effect_run_id", "probe_run_id", "sync_batch_id",
                                    "request_id", "decision_id", "response_id")}
    index["strategy_effect_run_id"].append({"id": STRATEGY_RUN, "source": refs[1]})
    index["probe_run_id"].append({"id": PROBE_RUN, "source": refs[2]})
    index["sync_batch_id"].append({"id": latest_batch["sync_batch_id"], "source": provider_file})
    for source, rows in ((refs[1], strategy["items"]), (refs[2], probe_logs)):
        for row in rows:
            for name in ("request_id", "decision_id", "response_id"):
                if row.get(name):
                    index[name].append({"id": row[name], "source": source})
    duplicate_ids = {name: sorted({entry["id"] for entry in entries
        if sum(other["id"] == entry["id"] for other in entries) > 1})
        for name, entries in index.items()}
    if any(duplicate_ids.values()):
        raise RuntimeError(f"duplicate_evidence_ids:{duplicate_ids}")
    known = {item["path"] for item in files}
    orphan = [entry for entries in index.values() for entry in entries if entry["source"] not in known]
    if orphan:
        raise RuntimeError(f"orphan_evidence_references:{orphan}")

    revision_number = int(previous["revision"]) + 1
    snapshot_id = f"ASR-{uuid.uuid4()}"
    created_at = datetime.now(timezone.utc).isoformat()
    revision_dir = ROOT / "evidence/final_acceptance" / ACCEPTANCE_RUN / f"revision-{revision_number}"
    if revision_dir.exists():
        raise RuntimeError("revision_directory_already_exists")
    revision_dir.mkdir(parents=True)
    revision = {
        "snapshot_id": snapshot_id, "acceptance_run_id": ACCEPTANCE_RUN,
        "revision": revision_number, "supersedes_snapshot_id": previous["snapshot_id"],
        "superseded_snapshot_checksum": previous["checksum"],
        "revision_reason": "真实三策略效果与五分钟持续探测验收，并完成 Provider 全分页水位同步",
        "strategy_effect_run_id": STRATEGY_RUN, "probe_run_id": PROBE_RUN,
        "provider_sync_batch_id": latest_batch["sync_batch_id"],
        "provider_watermark": json.loads(latest_batch["final_watermark_json"] or "{}"),
        "created_at": created_at, "evidence_files": files, "evidence_index": index,
        "validation": {"referenced_files_exist": True, "json_jsonl_parse": True,
                       "duplicate_ids": duplicate_ids, "orphan_references": [], "status": "valid"},
    }
    revision["checksum"] = digest(canonical(revision).encode())
    snapshot_file = revision_dir / "snapshot-revision.json"
    snapshot_file.write_text(json.dumps(revision, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"snapshot_id": snapshot_id, "revision": revision_number,
                "supersedes_snapshot_id": previous["snapshot_id"],
                "snapshot_file": file_record(snapshot_file.relative_to(ROOT).as_posix()),
                "evidence_files": files, "created_at": created_at}
    manifest["manifest_sha256"] = digest(canonical(manifest).encode())
    (revision_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with sqlite3.connect(DB) as db:
        db.execute("""INSERT INTO acceptance_snapshot_revisions(snapshot_id,acceptance_run_id,
          revision,supersedes_snapshot_id,original_snapshot_checksum,revision_json,checksum,
          created_at,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (snapshot_id, ACCEPTANCE_RUN, revision_number, previous["snapshot_id"], previous["checksum"],
           canonical(revision), revision["checksum"], created_at, *scope.sql_parameters()))
    print(json.dumps({"snapshot_id": snapshot_id, "revision": revision_number,
          "supersedes_snapshot_id": previous["snapshot_id"],
          "manifest_sha256": manifest["manifest_sha256"], "strategy_request_count": len(strategy["items"]),
          "probe_request_count": len(probe_logs), "provider_pages_read": latest_batch["pages_read"],
          "provider_exact_matches": latest_batch["matched_request_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
