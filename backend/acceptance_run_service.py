"""Durable, tenant-scoped acceptance runs and immutable final snapshots."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.tenant_security import TenantScope


EVIDENCE_FIELDS = (
    "request_id", "decision_id", "fault_id", "proposal_id", "invocation_id", "audit_id",
)


class AcceptanceRunError(ValueError):
    """Stable acceptance persistence error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AcceptanceRunError(f"{field}_must_be_object")
    return value


class AcceptanceRunService:
    schema_version = "acceptance_run_v1"

    def __init__(self, path: Path, scope: TenantScope | None = None):
        self.path = Path(path)
        self.scope = scope or TenantScope.local_development()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS acceptance_runs(
              acceptance_run_id TEXT NOT NULL,
              environment_id TEXT NOT NULL,
              status TEXT NOT NULL,
              baseline_git_json TEXT NOT NULL,
              baseline_config_json TEXT NOT NULL,
              baseline_price_json TEXT NOT NULL,
              baseline_migration_json TEXT NOT NULL,
              baseline_time_json TEXT NOT NULL,
              database_watermark_json TEXT NOT NULL,
              log_watermark_json TEXT NOT NULL,
              evidence_index_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              started_by TEXT,
              started_at TEXT NOT NULL,
              finalized_at TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,acceptance_run_id));
            CREATE TABLE IF NOT EXISTS acceptance_snapshots(
              snapshot_id TEXT NOT NULL,
              acceptance_run_id TEXT NOT NULL,
              baseline_json TEXT NOT NULL,
              evidence_index_json TEXT NOT NULL,
              evidence_counts_json TEXT NOT NULL,
              final_categories_json TEXT NOT NULL,
              human_signoff_state_json TEXT NOT NULL,
              notes TEXT,
              created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,snapshot_id),
              FOREIGN KEY(tenant_id,workspace_id,acceptance_run_id)
                REFERENCES acceptance_runs(tenant_id,workspace_id,acceptance_run_id));
            CREATE INDEX IF NOT EXISTS ix_acceptance_runs_status
              ON acceptance_runs(tenant_id,workspace_id,status,started_at);
            CREATE INDEX IF NOT EXISTS ix_acceptance_snapshots_run
              ON acceptance_snapshots(tenant_id,workspace_id,acceptance_run_id,created_at);
            """)

    def start_run(
        self, *, environment_id: str, acceptance_run_id: str | None = None,
        baseline_git: dict[str, Any] | None = None,
        baseline_config: dict[str, Any] | None = None,
        baseline_price: dict[str, Any] | None = None,
        baseline_migration: dict[str, Any] | None = None,
        baseline_time: dict[str, Any] | None = None,
        database_watermark: dict[str, Any] | None = None,
        log_watermark: dict[str, Any] | None = None,
        started_by: str | None = None,
        metadata: dict[str, Any] | None = None,
        baselines: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(environment_id).strip():
            raise AcceptanceRunError("environment_id_required")
        combined = _object(baselines, "baselines")
        baseline_git = baseline_git if baseline_git is not None else combined.get("git")
        baseline_config = baseline_config if baseline_config is not None else combined.get("config")
        baseline_price = baseline_price if baseline_price is not None else combined.get("price")
        baseline_migration = (baseline_migration if baseline_migration is not None
                              else combined.get("migration"))
        baseline_time = baseline_time if baseline_time is not None else combined.get("time")
        database_watermark = (database_watermark if database_watermark is not None
                              else combined.get("database_watermark", combined.get("database")))
        log_watermark = (log_watermark if log_watermark is not None
                         else combined.get("log_watermark", combined.get("log")))
        run_id = acceptance_run_id or f"AR-{uuid.uuid4()}"
        now = _now()
        values = (
            run_id, str(environment_id), "RUNNING",
            _json(_object(baseline_git, "baseline_git")),
            _json(_object(baseline_config, "baseline_config")),
            _json(_object(baseline_price, "baseline_price")),
            _json(_object(baseline_migration, "baseline_migration")),
            _json(_object(baseline_time, "baseline_time")),
            _json(_object(database_watermark, "database_watermark")),
            _json(_object(log_watermark, "log_watermark")),
            _json({field: [] for field in EVIDENCE_FIELDS}),
            _json(_object(metadata, "metadata")), started_by, now,
            *self.scope.sql_parameters(),
        )
        try:
            with self.connect() as db:
                db.execute("""INSERT INTO acceptance_runs(
                  acceptance_run_id,environment_id,status,baseline_git_json,
                  baseline_config_json,baseline_price_json,baseline_migration_json,
                  baseline_time_json,database_watermark_json,log_watermark_json,
                  evidence_index_json,metadata_json,started_by,started_at,
                  tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        except sqlite3.IntegrityError as exc:
            raise AcceptanceRunError("acceptance_run_id_exists") from exc
        return self.get_run(run_id)  # type: ignore[return-value]

    # Integration-friendly alias: a run is created and baselines are frozen atomically.
    start = start_run
    create_run = start_run

    def get_run(self, acceptance_run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("""SELECT * FROM acceptance_runs WHERE acceptance_run_id=?
              AND tenant_id=? AND workspace_id=?""",
              (acceptance_run_id, *self.scope.sql_parameters())).fetchone()
        return self._run_dict(row) if row else None

    run_lookup = get_run
    get_acceptance_run = get_run

    def active_run(self, environment_id: str = "china_uat") -> dict[str, Any] | None:
        """Return the newest still-running acceptance for automatic evidence binding."""
        with self.connect() as db:
            row = db.execute("""SELECT * FROM acceptance_runs
              WHERE environment_id=? AND status='RUNNING' AND tenant_id=? AND workspace_id=?
              ORDER BY started_at DESC LIMIT 1""",
              (environment_id, *self.scope.sql_parameters())).fetchone()
        return self._run_dict(row) if row else None

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for column in (
            "baseline_git", "baseline_config", "baseline_price", "baseline_migration",
            "baseline_time", "database_watermark", "log_watermark", "evidence_index", "metadata",
        ):
            result[column] = json.loads(result.pop(f"{column}_json"))
        result.pop("tenant_id", None)
        result.pop("workspace_id", None)
        return result

    def link_evidence(self, acceptance_run_id: str,
                      evidence_index: dict[str, Any] | None = None,
                      **evidence: Any) -> dict[str, Any]:
        supplied = _object(evidence_index, "evidence_index")
        overlap = set(supplied) & set(evidence)
        if overlap:
            raise AcceptanceRunError("duplicate_evidence_field")
        evidence = {**supplied, **evidence}
        unknown = set(evidence) - set(EVIDENCE_FIELDS)
        if unknown:
            raise AcceptanceRunError("unsupported_evidence_field")
        with self.connect() as db:
            row = db.execute("""SELECT status,evidence_index_json FROM acceptance_runs
              WHERE acceptance_run_id=? AND tenant_id=? AND workspace_id=?""",
              (acceptance_run_id, *self.scope.sql_parameters())).fetchone()
            if not row:
                raise AcceptanceRunError("acceptance_run_not_found")
            if row["status"] != "RUNNING":
                raise AcceptanceRunError("acceptance_run_finalized")
            index = json.loads(row["evidence_index_json"])
            for field, raw in evidence.items():
                values = raw if isinstance(raw, (list, tuple, set)) else [raw]
                for value in values:
                    text = str(value or "").strip()
                    if text and text not in index[field]:
                        index[field].append(text)
                index[field].sort()
            db.execute("""UPDATE acceptance_runs SET evidence_index_json=?
              WHERE acceptance_run_id=? AND tenant_id=? AND workspace_id=?""",
              (_json(index), acceptance_run_id, *self.scope.sql_parameters()))
        return self.get_run(acceptance_run_id)  # type: ignore[return-value]

    def evidence_counts(self, acceptance_run_id: str) -> dict[str, int]:
        if not self.get_run(acceptance_run_id):
            raise AcceptanceRunError("acceptance_run_not_found")
        with self.connect() as db:
            exists = db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
              AND name='standardized_call_logs'""").fetchone()
            if not exists:
                return {key: 0 for key in ("historical", "realtime", "injected", "provider_live", "total")}
            columns = {row[1] for row in db.execute("PRAGMA table_info(standardized_call_logs)")}
            required = {"acceptance_run_id", "is_historical", "source_type",
                        "is_fault_injected", "provider_log_id", "duplicate_status"}
            if not required.issubset(columns):
                return {key: 0 for key in ("historical", "realtime", "injected", "provider_live", "total")}
            row = db.execute("""SELECT
              SUM(CASE WHEN is_historical=1 THEN 1 ELSE 0 END) historical,
              SUM(CASE WHEN source_type='realtime_execution' THEN 1 ELSE 0 END) realtime,
              SUM(CASE WHEN COALESCE(is_fault_injected,0)=1 THEN 1 ELSE 0 END) injected,
              SUM(CASE WHEN source_type='realtime_execution' AND provider_log_id IS NOT NULL
                AND COALESCE(is_fault_injected,0)=0 THEN 1 ELSE 0 END) provider_live,
              COUNT(*) total FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND acceptance_run_id=?
              AND duplicate_status<>'exact_duplicate'""",
              (*self.scope.sql_parameters(), acceptance_run_id)).fetchone()
        return {key: int(row[key] or 0) for key in
                ("historical", "realtime", "injected", "provider_live", "total")}

    count_evidence = evidence_counts

    def live_summary(self, acceptance_run_id: str) -> dict[str, Any]:
        """Return a compact, business-facing view of persisted live evidence."""
        run = self.get_run(acceptance_run_id)
        if not run:
            raise AcceptanceRunError("acceptance_run_not_found")
        models = ("kimi-k3", "deepseek-v4-flash", "glm-5.2", "kimi-k2.7-code",
                  "dreamina-seedance-2-0-fast-260128", "kimi-k2.6")
        rows: list[dict[str, Any]] = []
        proposals: list[dict[str, Any]] = []
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='standardized_call_logs'").fetchone():
                placeholders = ",".join("?" for _ in models)
                source = db.execute(f"""SELECT requested_model,actual_model,stream,request_status,
                  http_status,request_id,response_id,decision_id,total_latency_ms,
                  first_token_latency_ms,input_tokens,cached_input_tokens,output_tokens,
                  cost_amount,currency,error_category,error_source,is_fault_injected,
                  strategy_variant,traffic_proposal_id,occurred_at FROM standardized_call_logs
                  WHERE acceptance_run_id=? AND requested_model IN ({placeholders})
                  AND COALESCE(duplicate_status,'canonical')<>'exact_duplicate'
                  ORDER BY occurred_at,cursor_id""", (acceptance_run_id, *models)).fetchall()
                rows = [dict(row) for row in source]
                for item in rows:
                    item["stream"] = bool(item["stream"])
                    item["is_fault_injected"] = bool(item["is_fault_injected"])
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='traffic_change_control_proposals'").fetchone():
                source = db.execute("""SELECT proposal_id,state,source_model_id,target_model_id,
                  source_policy_version,target_policy_version,rollout_percent,
                  last_trigger_json AS stop_reason,
                  restored_binding_json,created_at,updated_at FROM traffic_change_control_proposals
                  WHERE acceptance_run_id=? ORDER BY created_at""", (acceptance_run_id,)).fetchall()
                proposals = [dict(row) for row in source]
        clean = [row for row in rows if not row["is_fault_injected"]]
        injected = [row for row in rows if row["is_fault_injected"]]
        return {"acceptance_run_id": acceptance_run_id, "status": run["status"],
                "started_at": run["started_at"], "evidence_counts": self.evidence_counts(acceptance_run_id),
                "six_model_results": clean, "fault_injection_results": injected,
                "traffic_proposals": proposals, "evidence_index": run["evidence_index"]}

    def finalize_snapshot(
        self, acceptance_run_id: str, *, final_categories: dict[str, Any],
        human_signoff_state: dict[str, Any] | str,
        snapshot_id: str | None = None, notes: str | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(acceptance_run_id)
        if not run:
            raise AcceptanceRunError("acceptance_run_not_found")
        if run["status"] != "RUNNING":
            raise AcceptanceRunError("acceptance_run_finalized")
        categories = _object(final_categories, "final_categories")
        signoff = ({"state": human_signoff_state} if isinstance(human_signoff_state, str)
                   else _object(human_signoff_state, "human_signoff_state"))
        snap_id = snapshot_id or f"AS-{uuid.uuid4()}"
        now = _now()
        baseline = {key: run[key] for key in (
            "baseline_git", "baseline_config", "baseline_price", "baseline_migration",
            "baseline_time", "database_watermark", "log_watermark",
        )}
        counts = self.evidence_counts(acceptance_run_id)
        try:
            with self.connect() as db:
                db.execute("""INSERT INTO acceptance_snapshots(
                  snapshot_id,acceptance_run_id,baseline_json,evidence_index_json,
                  evidence_counts_json,final_categories_json,human_signoff_state_json,
                  notes,created_at,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    snap_id, acceptance_run_id, _json(baseline), _json(run["evidence_index"]),
                    _json(counts), _json(categories), _json(signoff), notes, now,
                    *self.scope.sql_parameters(),
                ))
                updated = db.execute("""UPDATE acceptance_runs SET status='FINALIZED',finalized_at=?
                  WHERE acceptance_run_id=? AND tenant_id=? AND workspace_id=? AND status='RUNNING'""",
                  (now, acceptance_run_id, *self.scope.sql_parameters()))
                if updated.rowcount != 1:
                    raise AcceptanceRunError("acceptance_run_finalized")
        except sqlite3.IntegrityError as exc:
            raise AcceptanceRunError("snapshot_id_exists") from exc
        return self.get_snapshot(snap_id)  # type: ignore[return-value]

    finalize = finalize_snapshot

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("""SELECT * FROM acceptance_snapshots WHERE snapshot_id=?
              AND tenant_id=? AND workspace_id=?""",
              (snapshot_id, *self.scope.sql_parameters())).fetchone()
        if not row:
            return None
        result = dict(row)
        for source, target in (
            ("baseline_json", "baseline"), ("evidence_index_json", "evidence_index"),
            ("evidence_counts_json", "evidence_counts"),
            ("final_categories_json", "final_categories"),
            ("human_signoff_state_json", "human_signoff_state"),
        ):
            result[target] = json.loads(result.pop(source))
        result.pop("tenant_id", None)
        result.pop("workspace_id", None)
        return result

    snapshot_lookup = get_snapshot
    get_acceptance_snapshot = get_snapshot

    def snapshots_for_run(self, acceptance_run_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = db.execute("""SELECT snapshot_id FROM acceptance_snapshots
              WHERE acceptance_run_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY created_at,snapshot_id""",
              (acceptance_run_id, *self.scope.sql_parameters())).fetchall()
        return [snapshot for row in ids if (snapshot := self.get_snapshot(row[0])) is not None]
