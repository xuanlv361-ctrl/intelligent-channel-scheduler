from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

from backend.enterprise_tenant_migrations import EnterpriseTenantMigrator


TENANT = "tenant_local_dev_v1"
WORKSPACE = "workspace_local_dev_v1"


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _legacy_fleet(path: Path) -> str:
    payload = b"protected-evidence-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    with _connect(path) as db:
        db.executescript("""
        CREATE TABLE realtime_log_sync_cursors(
          environment_id TEXT PRIMARY KEY,cursor_value TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE realtime_log_records(
          record_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,identity_key TEXT NOT NULL,
          payload BLOB NOT NULL,payload_sha256 TEXT NOT NULL,
          UNIQUE(environment_id,identity_key));
        CREATE TABLE persistent_browser_sessions(
          persistent_session_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE persistent_session_leases(
          lease_id TEXT PRIMARY KEY,persistent_session_id TEXT NOT NULL,
          lease_owner_job_id TEXT NOT NULL,state TEXT NOT NULL,
          FOREIGN KEY(persistent_session_id) REFERENCES persistent_browser_sessions(persistent_session_id));
        CREATE UNIQUE INDEX uq_active_persistent_session_lease
          ON persistent_session_leases(persistent_session_id) WHERE state='active';
        CREATE UNIQUE INDEX uq_active_persistent_job_lease
          ON persistent_session_leases(lease_owner_job_id) WHERE state='active';
        CREATE TABLE metric_aggregation_state(
          aggregation_version TEXT PRIMARY KEY,event_count INTEGER NOT NULL);
        CREATE TABLE metric_snapshots(
          snapshot_id TEXT PRIMARY KEY,aggregation_version TEXT NOT NULL,window_name TEXT NOT NULL,
          UNIQUE(aggregation_version,window_name));
        CREATE TABLE metric_source_cursors(
          source_name TEXT PRIMARY KEY,source_cursor TEXT);
        CREATE TABLE statistical_confidence_state(
          confidence_version TEXT PRIMARY KEY,last_status TEXT NOT NULL);
        CREATE TABLE price_sync_schedule(
          source_id TEXT PRIMARY KEY,state TEXT NOT NULL,generation INTEGER NOT NULL);
        CREATE TABLE sticky_bindings(
          sticky_binding_id TEXT PRIMARY KEY,route_key_fingerprint TEXT NOT NULL,state TEXT NOT NULL);
        CREATE UNIQUE INDEX uq_sticky_active_route
          ON sticky_bindings(route_key_fingerprint) WHERE state='ACTIVE';
        CREATE TABLE sticky_mutation_idempotency(
          idempotency_key TEXT PRIMARY KEY,operation TEXT NOT NULL,response_json TEXT NOT NULL);
        CREATE TABLE circuit_breakers(
          circuit_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE circuit_probe_leases(
          lease_id TEXT PRIMARY KEY,circuit_id TEXT NOT NULL,request_id TEXT UNIQUE,state TEXT NOT NULL,
          FOREIGN KEY(circuit_id) REFERENCES circuit_breakers(circuit_id));
        CREATE UNIQUE INDEX uq_active_probe_request
          ON circuit_probe_leases(circuit_id,request_id) WHERE state='ACTIVE';
        CREATE TABLE exploration_consumption(
          idempotency_key TEXT PRIMARY KEY,run_id TEXT NOT NULL,response_json TEXT NOT NULL);
        CREATE TABLE exploration_kill_switches(
          scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,active INTEGER NOT NULL,
          UNIQUE(scope_type,scope_id));
        CREATE TABLE probe_leases(
          lease_id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,state TEXT NOT NULL);
        CREATE TABLE traffic_change_proposals(
          proposal_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE traffic_change_kill_switches(
          scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,active INTEGER NOT NULL,
          PRIMARY KEY(scope_type,scope_id));
        CREATE TABLE high_cost_tests(
          test_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE high_cost_idempotency(
          operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,response_json TEXT NOT NULL,
          PRIMARY KEY(operation,idempotency_key));
        CREATE TABLE high_cost_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL);
        CREATE TABLE capability_evidence(
          evidence_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,details_json TEXT NOT NULL);
        CREATE TABLE uat_executions(
          execution_id TEXT PRIMARY KEY,local_request_id TEXT UNIQUE NOT NULL,
          payload BLOB NOT NULL,payload_sha256 TEXT NOT NULL);
        """)
        db.execute("INSERT INTO realtime_log_records VALUES(?,?,?,?,?)",
                   ("REC-1", "cn", "external-1", payload, digest))
        db.execute("INSERT INTO persistent_browser_sessions VALUES('SESSION-1','cn','active')")
        db.execute("INSERT INTO persistent_session_leases VALUES('LEASE-1','SESSION-1','JOB-1','active')")
        db.execute("INSERT INTO uat_executions VALUES(?,?,?,?)",
                   ("EXEC-1", "REQ-1", payload, digest))
        db.execute("INSERT INTO high_cost_audit(event_type) VALUES('created')")
    return digest


def _unique_indexes(db: sqlite3.Connection, table: str) -> list[list[str]]:
    return [[str(column[2]) for column in db.execute(f'PRAGMA index_info("{index[1]}")')]
            for index in db.execute(f'PRAGMA index_list("{table}")') if index[2]]


def _add_scope_catalog(db: sqlite3.Connection, tenant: str, workspace: str = "workspace_main") -> None:
    db.execute("INSERT INTO enterprise_tenants VALUES(?,?,0,'2026-08-02T00:00:00+00:00')",
               (tenant, tenant))
    db.execute("INSERT INTO enterprise_workspaces VALUES(?,?,?,0,'2026-08-02T00:00:00+00:00')",
               (tenant, workspace, workspace))


def test_rebuilds_representative_legacy_constraints_and_preserves_evidence(tmp_path: Path) -> None:
    path = tmp_path / "legacy-fleet.sqlite3"
    digest = _legacy_fleet(path)
    result = EnterpriseTenantMigrator(path).migrate()
    assert result["status"] == "applied"
    assert result["remaining_scoped_unique_rewrites"] == {}
    assert result["foreign_key_violation_count"] == 0
    assert result["integrity_check"] == "ok"
    assert result["safe_global_surrogate_primary_keys"] == {"high_cost_audit": ["audit_id"]}
    assert len(result["shadow_rebuilds"]) == 23

    with _connect(path) as db:
        for table in result["tenant_owned_tables"]:
            columns = {str(row[1]): row for row in db.execute(f'PRAGMA table_info("{table}")')}
            assert columns["tenant_id"][3] == 1
            assert columns["workspace_id"][3] == 1
            for unique in _unique_indexes(db, table):
                if unique == ["audit_id"]:
                    continue
                assert unique[:2] == ["tenant_id", "workspace_id"], (table, unique)
        assert db.execute("SELECT payload_sha256 FROM realtime_log_records").fetchone()[0] == digest
        assert db.execute("SELECT payload_sha256 FROM uat_executions").fetchone()[0] == digest
        assert db.execute("SELECT payload FROM realtime_log_records").fetchone()[0] == b"protected-evidence-bytes"
        assert not list(db.execute("PRAGMA foreign_key_check"))
        lease_fk = list(db.execute("PRAGMA foreign_key_list(persistent_session_leases)"))
        assert {row[3] for row in lease_fk if row[2] == "persistent_browser_sessions"} == {
            "tenant_id", "workspace_id", "persistent_session_id"
        }
        for index_name in ("uq_active_persistent_session_lease", "uq_active_persistent_job_lease"):
            columns = [row[2] for row in db.execute(f'PRAGMA index_info("{index_name}")')]
            assert columns[:2] == ["tenant_id", "workspace_id"]
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (index_name,)).fetchone()[0]
            assert " WHERE state='active'" in sql


def test_same_external_keys_coexist_across_tenants_after_legacy_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "coexist.sqlite3"
    _legacy_fleet(path)
    assert EnterpriseTenantMigrator(path).migrate()["status"] == "applied"
    with _connect(path) as db:
        _add_scope_catalog(db, "tenant_a")
        _add_scope_catalog(db, "tenant_b")
        for tenant in ("tenant_a", "tenant_b"):
            scope = (tenant, "workspace_main")
            db.execute("INSERT INTO realtime_log_sync_cursors VALUES(?,?,?,?,?)",
                       ("same-environment", "cursor", "now", *scope))
            db.execute("INSERT INTO sticky_mutation_idempotency VALUES(?,?,?,?,?)",
                       ("same-key", "mutate", "{}", *scope))
            db.execute("INSERT INTO exploration_consumption VALUES(?,?,?,?,?)",
                       ("same-key", "RUN", "{}", *scope))
            db.execute("INSERT INTO probe_leases VALUES(?,?,?,?,?)",
                       ("same-lease", "same-key", "ACTIVE", *scope))
            db.execute("INSERT INTO sticky_bindings VALUES(?,?,?,?,?)",
                       ("same-binding", "same-route", "ACTIVE", *scope))
            db.execute("INSERT INTO persistent_browser_sessions VALUES(?,?,?,?,?)",
                       ("same-session", "cn", "active", *scope))
            db.execute("INSERT INTO persistent_session_leases VALUES(?,?,?,?,?,?)",
                       ("same-lease", "same-session", "same-job", "active", *scope))
            db.execute("INSERT INTO uat_executions VALUES(?,?,?,?,?,?)",
                       ("same-exec", "same-request", b"x", hashlib.sha256(b"x").hexdigest(), *scope))
        assert db.execute("SELECT COUNT(*) FROM realtime_log_sync_cursors WHERE environment_id='same-environment'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM probe_leases WHERE idempotency_key='same-key'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM sticky_bindings WHERE route_key_fingerprint='same-route'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM persistent_session_leases WHERE lease_owner_job_id='same-job'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM uat_executions WHERE local_request_id='same-request'").fetchone()[0] == 2


def test_composite_foreign_key_rejects_cross_tenant_reference(tmp_path: Path) -> None:
    path = tmp_path / "cross-tenant-fk.sqlite3"
    _legacy_fleet(path)
    assert EnterpriseTenantMigrator(path).migrate()["status"] == "applied"
    with _connect(path) as db:
        _add_scope_catalog(db, "tenant_a")
        _add_scope_catalog(db, "tenant_b")
        db.execute("INSERT INTO persistent_browser_sessions VALUES(?,?,?,?,?)",
                   ("only-in-b", "cn", "active", "tenant_b", "workspace_main"))
        try:
            db.execute("INSERT INTO persistent_session_leases VALUES(?,?,?,?,?,?)",
                       ("lease-a", "only-in-b", "job-a", "active", "tenant_a", "workspace_main"))
        except sqlite3.IntegrityError as exc:
            assert "FOREIGN KEY constraint failed" in str(exc)
        else:
            raise AssertionError("cross-tenant reference was accepted")


def test_legacy_shadow_rebuild_is_repeat_safe(tmp_path: Path) -> None:
    path = tmp_path / "repeat.sqlite3"
    _legacy_fleet(path)
    first = EnterpriseTenantMigrator(path).migrate()
    second = EnterpriseTenantMigrator(path).migrate()
    assert first["status"] == "applied"
    assert second["status"] == "already_applied"
    assert second["remaining_scoped_unique_rewrites"] == {}
    with _connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM enterprise_schema_migrations WHERE version=2").fetchone()[0] == 1
        assert not list(db.execute("PRAGMA foreign_key_check"))


def test_unsupported_legacy_definition_is_quarantined_and_rolled_back(tmp_path: Path) -> None:
    path = tmp_path / "blocked.sqlite3"
    with _connect(path) as db:
        db.execute("CREATE TABLE collector_runs(run_id TEXT PRIMARY KEY,payload TEXT) WITHOUT ROWID")
        # DEFERRABLE semantics cannot be recovered from SQLite's FK pragma, so the
        # migrator must fail closed instead of silently weakening them.
        db.execute("CREATE TABLE collector_events(event_id TEXT PRIMARY KEY,run_id TEXT,"
                   "FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) DEFERRABLE INITIALLY DEFERRED)")
    result = EnterpriseTenantMigrator(path).migrate()
    assert result["status"] == "blocked"
    assert result["rolled_back"] is True
    with _connect(path) as db:
        assert "tenant_id" not in {row[1] for row in db.execute("PRAGMA table_info(collector_events)")}
        quarantine = db.execute("SELECT table_name,reason_code FROM enterprise_tenant_migration_quarantine").fetchone()
        assert tuple(quarantine) == ("collector_events", "migration_unsupported_table_definition")
