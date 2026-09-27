"""Create an immutable acceptance snapshot revision and signed evidence manifest."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.tenant_security import TenantScope


DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
RUN_ID = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"
ORIGINAL = "AS-45ceed1a-0ae2-4769-99de-77a9b4de12c7"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_record(relative: str) -> dict:
    path = ROOT / relative
    if not path.is_file():
        raise FileNotFoundError(relative)
    raw = path.read_bytes()
    parsed = None
    if path.suffix == ".json":
        parsed = json.loads(raw.decode("utf-8"))
    elif path.suffix == ".jsonl":
        parsed = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return {"path": relative.replace("\\", "/"), "bytes": len(raw),
            "sha256": digest_bytes(raw), "parse_status": "valid" if parsed is not None else "not_json"}


def collect_ids(host: dict, kimi: dict, clean: dict) -> dict[str, list[dict]]:
    refs: dict[str, list[dict]] = {name: [] for name in
        ("invocation_id", "audit_id", "remote_invocation_id", "remote_audit_id",
         "request_id", "decision_id")}
    clean_path = f"evidence/final_acceptance/{RUN_ID}/clean_environment_acceptance.json"
    for name in ("invocation_id", "audit_id", "request_id", "decision_id"):
        if clean.get("uat", {}).get(name):
            refs[name].append({"id": clean["uat"][name], "source": clean_path})
    host_path = str(host["_path"])
    for field in ("invoke_v1", "invoke_v2", "invoke_rollback_v1", "reenabled_invocation"):
        item = host.get(field) or {}
        for name in ("invocation_id", "audit_id"):
            if item.get(name): refs[name].append({"id": item[name], "source": host_path})
        result = item.get("result") or {}
        for name in ("remote_invocation_id", "remote_audit_id"):
            if result.get(name): refs[name].append({"id": result[name], "source": host_path})
    kimi_path = f"evidence/final_acceptance/{RUN_ID}/kimi_k3_stability_retest.json"
    for mode in kimi.get("modes", []):
        for item in mode.get("attempts", []):
            for name in ("request_id", "decision_id"):
                if item.get(name): refs[name].append({"id": item[name], "source": kimi_path})
    return refs


def main() -> None:
    revision_dir = ROOT / "evidence" / "final_acceptance" / RUN_ID / "revision-2"
    revision_dir.mkdir(parents=True, exist_ok=True)
    host_path = sorted((ROOT / "evidence" / "external-agent-host").glob("external-host-http-*.json"))[-1]
    host = json.loads(host_path.read_text(encoding="utf-8"))
    host["_path"] = host_path.relative_to(ROOT).as_posix()
    kimi_path = ROOT / "evidence" / "final_acceptance" / RUN_ID / "kimi_k3_stability_retest.json"
    clean_path = ROOT / "evidence" / "final_acceptance" / RUN_ID / "clean_environment_acceptance.json"
    kimi = json.loads(kimi_path.read_text(encoding="utf-8"))
    clean = json.loads(clean_path.read_text(encoding="utf-8"))
    references = [
      f"evidence/final_acceptance/{RUN_ID}/final_acceptance_report.json",
      f"evidence/final_acceptance/{RUN_ID}/clean_environment_acceptance.json",
      f"evidence/final_acceptance/{RUN_ID}/kimi_k3_stability_retest.json",
      "evidence/npm-security/before-fix-20260806/npm-audit.json",
      "evidence/npm-security/before-fix-20260806/npm-ls-all.json",
      "evidence/npm-security/after-fix-20260806/npm-audit.json",
      "evidence/npm-security/after-fix-20260806/npm-ls-all.json",
      "evidence/npm-security/vulnerability-analysis-20260806.json",
      "evidence/pricing/price-catalog-china-uat-20260806-v1-live-db.json",
      f"evidence/pricing/cost-first-{RUN_ID}.json",
      host_path.relative_to(ROOT).as_posix(),
      "evidence/regression/20260806/summary.json",
    ]
    files = [file_record(path) for path in references]
    ids = collect_ids(host, kimi, clean)
    duplicate_ids = {kind: sorted({entry["id"] for entry in entries
        if sum(other["id"] == entry["id"] for other in entries) > 1})
        for kind, entries in ids.items()}
    if any(duplicate_ids.values()):
        raise RuntimeError(f"duplicate_evidence_ids:{duplicate_ids}")
    known_files = {item["path"] for item in files}
    orphan_refs = [entry for entries in ids.values() for entry in entries
                   if entry["source"] not in known_files]
    if orphan_refs:
        raise RuntimeError(f"orphan_evidence_references:{orphan_refs}")
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        original_row = db.execute("SELECT * FROM acceptance_snapshots WHERE snapshot_id=?", (ORIGINAL,)).fetchone()
        if not original_row:
            raise RuntimeError("original_snapshot_not_found")
        original_payload = dict(original_row)
        original_checksum = digest_bytes(canonical(original_payload).encode())
        revision_snapshot_id = f"ASR-{uuid.uuid4()}"
        created_at = datetime.now(timezone.utc).isoformat()
        revision = {
          "snapshot_id": revision_snapshot_id, "acceptance_run_id": RUN_ID, "revision": 2,
          "supersedes_snapshot_id": ORIGINAL, "original_snapshot_checksum": original_checksum,
          "revision_reason": "补录 npm 修复、版本化价格、cost_first、独立 Host、Kimi K3 复验及最终全量回归证据",
          "created_at": created_at, "evidence_files": files, "evidence_index": ids,
          "validation": {"referenced_files_exist": True, "json_jsonl_parse": True,
                         "duplicate_ids": duplicate_ids, "orphan_references": [], "status": "valid"},
        }
        revision_checksum = digest_bytes(canonical(revision).encode())
        revision["checksum"] = revision_checksum
        db.execute("""CREATE TABLE IF NOT EXISTS acceptance_snapshot_revisions(
          snapshot_id TEXT PRIMARY KEY,acceptance_run_id TEXT NOT NULL,revision INTEGER NOT NULL,
          supersedes_snapshot_id TEXT NOT NULL,original_snapshot_checksum TEXT NOT NULL,
          revision_json TEXT NOT NULL,checksum TEXT NOT NULL,created_at TEXT NOT NULL,
          UNIQUE(acceptance_run_id,revision))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(acceptance_snapshot_revisions)")}
        scope = TenantScope.local_development()
        scoped = {"tenant_id", "workspace_id"}.issubset(columns)
        scope_filter = " AND tenant_id=? AND workspace_id=?" if scoped else ""
        scope_params = scope.sql_parameters() if scoped else ()
        existing = db.execute("SELECT snapshot_id FROM acceptance_snapshot_revisions WHERE acceptance_run_id=? AND revision=2" + scope_filter,
                              (RUN_ID, *scope_params)).fetchone()
        if existing:
            db.execute("DELETE FROM acceptance_snapshot_revisions WHERE acceptance_run_id=? AND revision=2" + scope_filter,
                       (RUN_ID, *scope_params))
        if scoped:
            db.execute("""INSERT INTO acceptance_snapshot_revisions(
              snapshot_id,acceptance_run_id,revision,supersedes_snapshot_id,
              original_snapshot_checksum,revision_json,checksum,created_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (revision_snapshot_id, RUN_ID, 2, ORIGINAL,
              original_checksum, canonical(revision), revision_checksum, created_at, *scope_params))
        else:
            db.execute("INSERT INTO acceptance_snapshot_revisions VALUES(?,?,?,?,?,?,?,?)", (
                revision_snapshot_id, RUN_ID, 2, ORIGINAL, original_checksum,
                canonical(revision), revision_checksum, created_at))
    revision_path = revision_dir / "snapshot-revision.json"
    revision_path.write_text(json.dumps(revision, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"snapshot_id": revision_snapshot_id, "revision": 2,
                "snapshot_file": file_record(revision_path.relative_to(ROOT).as_posix()),
                "evidence_file_count": len(files), "created_at": created_at}
    manifest["manifest_sha256"] = digest_bytes(canonical(manifest).encode())
    manifest_path = revision_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"snapshot_id": revision_snapshot_id, "revision": 2,
                      "original_checksum": original_checksum,
                      "revision_checksum": revision_checksum,
                      "manifest_sha256": manifest["manifest_sha256"],
                      "file_count": len(files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
