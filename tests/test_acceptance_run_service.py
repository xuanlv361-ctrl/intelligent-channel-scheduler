from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend.acceptance_run_service import AcceptanceRunError, AcceptanceRunService
from backend.call_log_service import CallLogService


def test_run_links_evidence_and_finalizes_an_immutable_snapshot(tmp_path: Path):
    path = tmp_path / "acceptance.sqlite3"
    calls = CallLogService(path)
    service = AcceptanceRunService(path)
    run = service.start_run(
        acceptance_run_id="AR-FIXTURE", environment_id="china_uat",
        baseline_git={"commit": "abc123", "dirty": False},
        baseline_config={"version": "config-v1", "sha256": "a" * 64},
        baseline_price={"version": "price-v1"},
        baseline_migration={"version": "migration-v1"},
        baseline_time={"captured_at": "2026-08-06T01:00:00+00:00"},
        database_watermark={"schema_version": 7},
        log_watermark={"cursor_id": 646},
    )
    assert run["status"] == "RUNNING"
    assert run["log_watermark"] == {"cursor_id": 646}
    service.link_evidence(
        "AR-FIXTURE", request_id=["REQ-2", "REQ-1", "REQ-1"],
        decision_id="DEC-1", fault_id="FAULT-1", proposal_id="PROPOSAL-1",
        invocation_id="INV-1", audit_id="AUDIT-1",
    )
    record_id = calls.start_execution(
        request_id="REQ-1", decision_id="DEC-1", environment_id="china_uat",
        requested_model="model-a", stream=False, acceptance_run_id="AR-FIXTURE",
    )
    calls.finish_execution(record_id, status="SUCCESS", provider_log_id="PL-1")

    snapshot = service.finalize_snapshot(
        "AR-FIXTURE", snapshot_id="AS-FIXTURE",
        final_categories={"functional": "PASS", "fault_recovery": "PASS"},
        human_signoff_state={"state": "PENDING", "roles": []},
    )
    assert snapshot["baseline"]["baseline_git"]["commit"] == "abc123"
    assert snapshot["evidence_index"]["request_id"] == ["REQ-1", "REQ-2"]
    assert snapshot["evidence_counts"] == {
        "historical": 0, "realtime": 1, "injected": 0,
        "provider_live": 1, "total": 1,
    }
    assert service.get_run("AR-FIXTURE")["status"] == "FINALIZED"
    assert service.get_snapshot("AS-FIXTURE") == snapshot
    with pytest.raises(AcceptanceRunError, match="acceptance_run_finalized"):
        service.link_evidence("AR-FIXTURE", request_id="REQ-LATE")
    with pytest.raises(AcceptanceRunError, match="acceptance_run_finalized"):
        service.finalize_snapshot(
            "AR-FIXTURE", final_categories={}, human_signoff_state="PENDING"
        )


def test_schema_initialization_is_idempotent_and_ids_are_scope_unique(tmp_path: Path):
    path = tmp_path / "acceptance.sqlite3"
    AcceptanceRunService(path)
    service = AcceptanceRunService(path)
    service.start_run(acceptance_run_id="AR-ONE", environment_id="uat")
    with pytest.raises(AcceptanceRunError, match="acceptance_run_id_exists"):
        service.start_run(acceptance_run_id="AR-ONE", environment_id="uat")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM acceptance_runs").fetchone()[0] == 1
        assert {row[1] for row in db.execute("PRAGMA table_info(acceptance_runs)")} >= {
            "acceptance_run_id", "database_watermark_json", "log_watermark_json",
            "evidence_index_json", "finalized_at",
        }


def test_live_summary_reads_current_traffic_control_schema(tmp_path: Path):
    path = tmp_path / "acceptance-live.sqlite3"
    service = AcceptanceRunService(path)
    service.start_run(acceptance_run_id="AR-LIVE", environment_id="china_uat")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE traffic_change_control_proposals(
          proposal_id TEXT,state TEXT,source_model_id TEXT,target_model_id TEXT,
          source_policy_version TEXT,target_policy_version TEXT,rollout_percent REAL,
          last_trigger_json TEXT,restored_binding_json TEXT,created_at TEXT,
          updated_at TEXT,acceptance_run_id TEXT)""")
        db.execute("""INSERT INTO traffic_change_control_proposals VALUES(
          'TCG-1','ROLLED_BACK','model-a','model-b','v1','v2',25,
          '[{"condition":"error_rate"}]','{"model_id":"model-a"}',
          '2026-08-06T00:00:00Z','2026-08-06T00:01:00Z','AR-LIVE')""")
    summary = service.live_summary("AR-LIVE")
    assert summary["traffic_proposals"][0]["proposal_id"] == "TCG-1"
    assert "error_rate" in summary["traffic_proposals"][0]["stop_reason"]
