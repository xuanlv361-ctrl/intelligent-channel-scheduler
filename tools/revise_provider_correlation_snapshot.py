"""Append a validated Provider-correlation acceptance revision without overwriting history."""
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
RUN_ID = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"
STRATEGY_RUN_ID = "SER-F61CFBCB170E464AA2099D83662CBC84"


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
    return {"path": relative.replace("\\", "/"), "bytes": len(raw), "sha256": digest(raw), "parse_status": "valid"}


def main() -> None:
    refs = [
        "evidence/provider-correlation/provider-interface-capture.json",
        "evidence/provider-correlation/provider-correlation-exact-summary-20260806T1910.json",
        "evidence/provider-correlation/CORR-F8162DC576064BF4B5E5BD7A7FA99AD3.json",
        "evidence/provider-correlation/CORR-300C0855AD3C468B9B649142287F5EA7.json",
        "evidence/provider_cost_sync/PBS-C02EEC03D0F1426689FE8AC0.json",
        "evidence/provider_cost_sync/PBS-C59901ECEC1941838DD94560.json",
        "evidence/provider_cost_sync/PBS-5252AFBD4B1A428AB03D6392.json",
        "evidence/provider_cost_sync/PBS-8B24E9614963435A8FEDC51E.json",
        "evidence/pricing/price-catalog-china-uat-20260806100218-a42d372ccf0b-v1.json",
        f"evidence/strategy_effect/{STRATEGY_RUN_ID}/results.json",
        f"evidence/strategy_effect/{STRATEGY_RUN_ID}/summary-after-provider-sync-20260806T1910.json",
        "evidence/regression/provider-correlation-regression-20260806T1910.json",
    ]
    files = [file_record(item) for item in refs]
    created_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        previous = db.execute(
            "SELECT snapshot_id,revision,checksum FROM acceptance_snapshot_revisions "
            "WHERE acceptance_run_id=? ORDER BY revision DESC LIMIT 1", (RUN_ID,)
        ).fetchone()
        if previous is None:
            raise RuntimeError("previous_snapshot_revision_not_found")
        revision_number = int(previous["revision"]) + 1
        revision_dir = ROOT / "evidence/final_acceptance" / RUN_ID / f"revision-{revision_number}"
        if revision_dir.exists():
            raise RuntimeError("revision_directory_already_exists")
        revision_dir.mkdir(parents=True)
        snapshot_id = f"ASR-{uuid.uuid4()}"
        revision = {
            "snapshot_id": snapshot_id,
            "acceptance_run_id": RUN_ID,
            "revision": revision_number,
            "supersedes_snapshot_id": previous["snapshot_id"],
            "superseded_snapshot_checksum": previous["checksum"],
            "revision_reason": "修复本地请求ID与Provider请求ID混用，加入真实接口Schema、精确关联、费用回填与策略复验证据",
            "created_at": created_at,
            "evidence_files": files,
            "evidence_index": {
                "strategy_effect_run_id": [{"id": STRATEGY_RUN_ID, "source": f"evidence/strategy_effect/{STRATEGY_RUN_ID}/summary-after-provider-sync-20260806T1910.json"}],
                "correlation_test_id": [
                    {"id": "CORR-F8162DC576064BF4B5E5BD7A7FA99AD3", "source": "evidence/provider-correlation/CORR-F8162DC576064BF4B5E5BD7A7FA99AD3.json"},
                    {"id": "CORR-300C0855AD3C468B9B649142287F5EA7", "source": "evidence/provider-correlation/CORR-300C0855AD3C468B9B649142287F5EA7.json"},
                ],
                "sync_batch_id": [
                    {"id": "PBS-C02EEC03D0F1426689FE8AC0", "source": "evidence/provider_cost_sync/PBS-C02EEC03D0F1426689FE8AC0.json"},
                    {"id": "PBS-C59901ECEC1941838DD94560", "source": "evidence/provider_cost_sync/PBS-C59901ECEC1941838DD94560.json"},
                    {"id": "PBS-5252AFBD4B1A428AB03D6392", "source": "evidence/provider_cost_sync/PBS-5252AFBD4B1A428AB03D6392.json"},
                    {"id": "PBS-8B24E9614963435A8FEDC51E", "source": "evidence/provider_cost_sync/PBS-8B24E9614963435A8FEDC51E.json"},
                ],
            },
            "validation": {"referenced_files_exist": True, "json_jsonl_parse": True, "duplicate_ids": [], "orphan_references": [], "status": "valid"},
        }
        all_ids = [entry["id"] for entries in revision["evidence_index"].values() for entry in entries]
        if len(all_ids) != len(set(all_ids)):
            raise RuntimeError("duplicate_evidence_ids")
        known = {item["path"] for item in files}
        if any(entry["source"] not in known for entries in revision["evidence_index"].values() for entry in entries):
            raise RuntimeError("orphan_evidence_reference")
        revision["checksum"] = digest(canonical(revision).encode("utf-8"))
        snapshot_path = revision_dir / "snapshot-revision.json"
        snapshot_path.write_text(json.dumps(revision, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = {
            "snapshot_id": snapshot_id,
            "revision": revision_number,
            "supersedes_snapshot_id": previous["snapshot_id"],
            "snapshot_file": file_record(snapshot_path.relative_to(ROOT).as_posix()),
            "evidence_files": files,
            "created_at": created_at,
        }
        manifest["manifest_sha256"] = digest(canonical(manifest).encode("utf-8"))
        (revision_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        columns = {row[1] for row in db.execute("PRAGMA table_info(acceptance_snapshot_revisions)")}
        base = (snapshot_id, RUN_ID, revision_number, previous["snapshot_id"], previous["checksum"], canonical(revision), revision["checksum"], created_at)
        if {"tenant_id", "workspace_id"}.issubset(columns):
            db.execute(
                "INSERT INTO acceptance_snapshot_revisions(snapshot_id,acceptance_run_id,revision,supersedes_snapshot_id,original_snapshot_checksum,revision_json,checksum,created_at,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*base, "tenant-local-development", "workspace-local-development"),
            )
        else:
            db.execute("INSERT INTO acceptance_snapshot_revisions VALUES(?,?,?,?,?,?,?,?)", base)
    print(json.dumps({"snapshot_id": snapshot_id, "revision": revision_number, "supersedes_snapshot_id": previous["snapshot_id"], "manifest_sha256": manifest["manifest_sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
