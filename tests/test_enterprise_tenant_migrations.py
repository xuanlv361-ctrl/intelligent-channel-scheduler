from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from backend.enterprise_tenant_migrations import EnterpriseTenantMigrator


TENANT = "tenant_local_dev_v1"
WORKSPACE = "workspace_local_dev_v1"


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def create_legacy_database(path: Path) -> tuple[str, str]:
    payload = '{"decision_id":"DEC-001","immutable":true}'
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE uat_executions(
              execution_id TEXT PRIMARY KEY,
              local_request_id TEXT UNIQUE NOT NULL,
              payload TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL
            );
            CREATE TABLE metric_evidence_events(
              evidence_id TEXT PRIMARY KEY,
              environment_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO uat_executions VALUES(?,?,?,?)",
            ("EXEC-001", "REQ-001", payload, payload_hash),
        )
        db.execute(
            "INSERT INTO metric_evidence_events VALUES(?,?,?,?)",
            ("EVID-001", "china_uat", payload_hash, payload),
        )
    return payload, payload_hash


def columns(db: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {row[1]: row for row in db.execute(f"PRAGMA table_info({table})")}


def test_empty_database_migration_creates_catalog_and_is_repeat_safe(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    first = EnterpriseTenantMigrator(path).migrate()
    second = EnterpriseTenantMigrator(path).migrate()
    assert first["status"] == "applied"
    assert first["tenant_owned_tables"] == []
    assert second["status"] == "already_applied"
    with connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tenant = db.execute("SELECT * FROM enterprise_tenants").fetchone()
        workspace = db.execute("SELECT * FROM enterprise_workspaces").fetchone()
        assert tenant["tenant_id"] == TENANT
        assert workspace["workspace_id"] == WORKSPACE
        assert db.execute("SELECT COUNT(*) FROM enterprise_schema_migrations").fetchone()[0] == 1


def test_late_created_table_is_migrated_even_when_version_was_applied(tmp_path: Path) -> None:
    path = tmp_path / "late.sqlite3"
    assert EnterpriseTenantMigrator(path).migrate()["status"] == "applied"
    with connect(path) as db:
        db.execute(
            "CREATE TABLE collector_runs(run_id TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )
        db.execute("INSERT INTO collector_runs VALUES('RUN-1','unchanged')")
    repaired = EnterpriseTenantMigrator(path).migrate()
    assert repaired["status"] == "applied"
    assert repaired["changed_tables"] == ["collector_runs"]
    with connect(path) as db:
        row = db.execute("SELECT * FROM collector_runs").fetchone()
        assert row["tenant_id"] == TENANT
        assert row["workspace_id"] == WORKSPACE
        assert row["payload"] == "unchanged"


def test_existing_database_is_backfilled_without_rewriting_values(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    payload, payload_hash = create_legacy_database(path)
    result = EnterpriseTenantMigrator(path).migrate()
    assert result["status"] == "applied"
    assert result["changed_tables"] == ["metric_evidence_events", "uat_executions"]
    assert result["protected_external_evidence_modified"] is False
    with connect(path) as db:
        for table in ("uat_executions", "metric_evidence_events"):
            info = columns(db, table)
            assert info["tenant_id"][3] == 1
            assert info["workspace_id"][3] == 1
            row = db.execute(f"SELECT * FROM {table}").fetchone()
            assert row["tenant_id"] == TENANT
            assert row["workspace_id"] == WORKSPACE
            assert row["payload"] == payload
            hash_column = "payload_sha256" if table == "uat_executions" else "payload_hash"
            assert row[hash_column] == payload_hash
            index_names = {item[1] for item in db.execute(f"PRAGMA index_list({table})")}
            assert f"ix_ent_scope_{table}" in index_names
        assert db.execute("SELECT execution_id FROM uat_executions").fetchone()[0] == "EXEC-001"
        assert db.execute("SELECT evidence_id FROM metric_evidence_events").fetchone()[0] == "EVID-001"

    for table in result["tenant_owned_tables"]:
        assert result["pre_migration"][table]["row_count"] == result["post_migration"][table]["row_count"]
        assert result["pre_migration"][table]["identity_fingerprint"] == result["post_migration"][table]["identity_fingerprint"]
        assert result["pre_migration"][table]["rowid_fingerprint"] == result["post_migration"][table]["rowid_fingerprint"]


def test_v1_additive_database_is_upgraded_to_v2_constraint_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "v1-applied.sqlite3"
    create_legacy_database(path)
    migrator = EnterpriseTenantMigrator(path)
    with migrator.connect() as db:
        migrator._control_schema(db)
        db.execute("INSERT INTO enterprise_tenants VALUES(?,?,1,'2026-08-01T00:00:00+00:00')",
                   (TENANT, "Local development tenant"))
        db.execute("INSERT INTO enterprise_workspaces VALUES(?,?,?,1,'2026-08-01T00:00:00+00:00')",
                   (TENANT, WORKSPACE, "Local development workspace"))
        for table in ("uat_executions", "metric_evidence_events"):
            db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '{TENANT}'")
            db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '{WORKSPACE}'")
            db.execute(f"CREATE INDEX ix_ent_scope_{table} ON {table}(tenant_id,workspace_id)")
        db.execute("INSERT INTO enterprise_schema_migrations VALUES(1,'enterprise_tenant_policy_v1',"
                   "'2026-08-01T00:00:00+00:00','legacy-manifest')")
    result = migrator.migrate()
    assert result["status"] == "applied"
    assert result["migration_version"] == 2
    assert result["remaining_scoped_unique_rewrites"] == {}
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM enterprise_schema_migrations").fetchone()[0] == 2
        primary = [row[1] for row in sorted(db.execute("PRAGMA table_info(uat_executions)"),
                                            key=lambda row: row[5]) if row[5]]
        assert primary == ["tenant_id", "workspace_id", "execution_id"]
        assert result["pre_migration"][table]["protected_fingerprint"] == result["post_migration"][table]["protected_fingerprint"]


def test_interruption_rolls_back_and_next_run_recovers(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.sqlite3"
    create_legacy_database(path)
    failed = EnterpriseTenantMigrator(path).migrate(interrupt_after_tables=1)
    assert failed["status"] == "failed"
    assert failed["rolled_back"] is True
    assert failed["error_code"] == "simulated_migration_interruption"
    with connect(path) as db:
        assert "tenant_id" not in columns(db, "uat_executions")
        assert "tenant_id" not in columns(db, "metric_evidence_events")
        assert db.execute("SELECT COUNT(*) FROM enterprise_schema_migrations").fetchone()[0] == 0
    recovered = EnterpriseTenantMigrator(path).migrate()
    assert recovered["status"] == "applied"
    with connect(path) as db:
        assert "tenant_id" in columns(db, "uat_executions")
        statuses = [row[0] for row in db.execute(
            "SELECT status FROM enterprise_tenant_migration_runs ORDER BY started_at"
        )]
        assert "failed" in statuses
        assert "applied" in statuses


def test_unclassified_table_is_quarantined_and_business_schema_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite3"
    with connect(path) as db:
        db.execute("CREATE TABLE plugin_private_state(id TEXT PRIMARY KEY,payload TEXT)")
        db.execute("INSERT INTO plugin_private_state VALUES('ID-1','safe-test-value')")
    result = EnterpriseTenantMigrator(path).migrate()
    assert result["status"] == "blocked"
    assert result["unknown_tables"] == ["plugin_private_state"]
    with connect(path) as db:
        assert "tenant_id" not in columns(db, "plugin_private_state")
        quarantine = db.execute(
            "SELECT table_name,reason_code FROM enterprise_tenant_migration_quarantine"
        ).fetchone()
        assert tuple(quarantine) == (
            "plugin_private_state",
            "unclassified_persistent_table",
        )
        serialized = json.dumps(dict(quarantine))
        assert "safe-test-value" not in serialized


def test_manifest_proves_unscoped_unique_keys_were_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "keys.sqlite3"
    create_legacy_database(path)
    result = EnterpriseTenantMigrator(path).migrate()
    assert result["remaining_scoped_unique_rewrites"] == {}
    rebuild = next(item for item in result["shadow_rebuilds"]
                   if item["table"] == "uat_executions")
    assert rebuild["safe_global_surrogate_primary_key"] == []
    with connect(path) as db:
        primary = [row[1] for row in sorted(
            db.execute("PRAGMA table_info(uat_executions)"), key=lambda row: row[5]
        ) if row[5]]
        assert primary == ["tenant_id", "workspace_id", "execution_id"]
        unique_columns = [
            [column[2] for column in db.execute(f"PRAGMA index_info({index[1]})")]
            for index in db.execute("PRAGMA index_list(uat_executions)")
            if index[2]
        ]
        assert ["tenant_id", "workspace_id", "local_request_id"] in unique_columns
    assert result["foreign_key_violation_count"] == 0
    assert result["integrity_check"] == "ok"
