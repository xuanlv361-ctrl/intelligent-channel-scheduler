"""Append (never overwrite) the Provider-cost acceptance snapshot revision."""
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
from backend.tenant_security import TenantScope
DB = Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
RUN = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def sha(raw: bytes) -> str:
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
            "sha256": sha(raw), "parse_status": "valid"}


def main() -> None:
    summary_path = "evidence/provider_cost_sync/provider-cost-sync-summary.json"
    summary = json.loads((ROOT / summary_path).read_text(encoding="utf-8"))
    sync_path = f"evidence/provider_cost_sync/{summary['sync_batch_id']}.json"
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(acceptance_snapshot_revisions)")}
        scope = TenantScope.local_development()
        scoped = {"tenant_id", "workspace_id"}.issubset(columns)
        where = " AND tenant_id=? AND workspace_id=?" if scoped else ""
        params = scope.sql_parameters() if scoped else ()
        previous = db.execute("""SELECT snapshot_id,revision,checksum FROM
          acceptance_snapshot_revisions WHERE acceptance_run_id=?""" + where +
          " ORDER BY revision DESC LIMIT 1", (RUN, *params)).fetchone()
        if not previous:
            raise RuntimeError("previous_snapshot_revision_not_found")
        revision_number = int(previous["revision"]) + 1
        snapshot_id = f"ASR-{uuid.uuid4()}"
        created = datetime.now(timezone.utc).isoformat()
        revision_dir = ROOT / "evidence/final_acceptance" / RUN / f"revision-{revision_number}"
        if revision_dir.exists():
            raise RuntimeError("revision_directory_already_exists")
        revision_dir.mkdir(parents=True)
        refs = [
            f"evidence/final_acceptance/{RUN}/revision-{previous['revision']}/snapshot-revision.json",
            summary_path,
            sync_path,
            "evidence/pricing/cost-first-provider-sync-20260806.json",
            "evidence/regression/20260806/provider-cost-sync-final.json",
        ]
        files = [file_record(item) for item in refs]
        evidence_index = {
            "sync_batch_id": [{"id": summary["sync_batch_id"], "source": summary_path}],
            "provider_cost_evidence_id": [{"id": summary["evidence_id"], "source": summary_path}],
            "request_id": [], "decision_id": [], "invocation_id": [], "audit_id": [],
        }
        ids = [item["id"] for values in evidence_index.values() for item in values]
        if len(ids) != len(set(ids)):
            raise RuntimeError("duplicate_evidence_ids")
        known = {item["path"] for item in files}
        if any(item["source"] not in known for values in evidence_index.values()
               for item in values):
            raise RuntimeError("orphan_evidence_reference")
        revision = {
            "snapshot_id": snapshot_id, "acceptance_run_id": RUN,
            "revision": revision_number,
            "supersedes_snapshot_id": previous["snapshot_id"],
            "superseded_snapshot_checksum": previous["checksum"],
            "revision_reason": "Provider日志水位同步、精确Request ID关联、费用口径和幂等复核",
            "sync_batch_id": summary["sync_batch_id"],
            "data_watermark": summary["final_watermark"],
            "sync_summary": {
                "task_start_pending_count": summary["task_start_pending_count"],
                "pending_created_during_sync": summary["pending_created_during_sync"],
                "provider_new_records": summary["provider_new_records_in_task"],
                "matched_count": summary["exact_request_id_matches"],
                "unmatched_count": summary["unmatched_count"],
                "backfilled_count": summary["cost_backfilled_count"],
                "unmatched_reasons": summary["unmatched_reasons"],
            },
            "created_at": created, "evidence_files": files,
            "evidence_index": evidence_index,
            "validation": {"referenced_files_exist": True,
                           "json_jsonl_parse": True, "duplicate_ids": [],
                           "orphan_references": [], "status": "valid"},
        }
        revision_checksum = sha(canonical(revision).encode())
        revision["checksum"] = revision_checksum
        snapshot_file = revision_dir / "snapshot-revision.json"
        snapshot_file.write_text(json.dumps(revision, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        manifest = {
            "snapshot_id": snapshot_id, "revision": revision_number,
            "supersedes_snapshot_id": previous["snapshot_id"],
            "snapshot_file": file_record(snapshot_file.relative_to(ROOT).as_posix()),
            "evidence_files": files, "created_at": created,
        }
        manifest["manifest_sha256"] = sha(canonical(manifest).encode())
        (revision_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        values = (snapshot_id, RUN, revision_number, previous["snapshot_id"],
                  previous["checksum"], canonical(revision), revision_checksum, created)
        if scoped:
            db.execute("""INSERT INTO acceptance_snapshot_revisions(
              snapshot_id,acceptance_run_id,revision,supersedes_snapshot_id,
              original_snapshot_checksum,revision_json,checksum,created_at,
              tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (*values, *params))
        else:
            db.execute("INSERT INTO acceptance_snapshot_revisions VALUES(?,?,?,?,?,?,?,?)", values)
    print(json.dumps({"snapshot_id": snapshot_id, "revision": revision_number,
          "supersedes_snapshot_id": previous["snapshot_id"],
          "revision_checksum": revision_checksum,
          "manifest_sha256": manifest["manifest_sha256"],
          "file_count": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
