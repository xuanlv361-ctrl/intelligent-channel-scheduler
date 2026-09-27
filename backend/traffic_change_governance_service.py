"""Offline governance for approval-controlled production-traffic changes.

The service persists intent and sandbox rollout state.  It deliberately has no
platform adapter and can never authorize a real production mutation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope


class TrafficChangeGovernanceError(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise TrafficChangeGovernanceError("timezone_required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def load_traffic_change_policy(path: str | Path) -> dict[str, Any]:
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrafficChangeGovernanceError("invalid_traffic_change_policy") from exc
    required = {"policy_version", "enabled", "real_adapter_enabled",
                "maximum_rollout_percent", "maximum_ttl_seconds", "required_gates", "roles"}
    if not isinstance(policy, dict) or not required.issubset(policy):
        raise TrafficChangeGovernanceError("invalid_traffic_change_policy")
    if policy["real_adapter_enabled"] is not False:
        raise TrafficChangeGovernanceError("real_adapter_must_be_disabled")
    if not 0 < policy["maximum_rollout_percent"] <= 100 or policy["maximum_ttl_seconds"] <= 0:
        raise TrafficChangeGovernanceError("invalid_traffic_change_limits")
    if not policy["required_gates"] or set(policy["roles"]) != {
            "proposer", "approver", "operator", "emergency"}:
        raise TrafficChangeGovernanceError("invalid_traffic_change_roles_or_gates")
    return policy


class TrafficChangeGovernanceService:
    TERMINAL = frozenset({"ROLLED_BACK", "EXPIRED", "CANCELLED"})

    def __init__(self, database_path: str | Path, policy_path: str | Path, *,
                 clock: Callable[[], datetime] = _utc_now,
                 authorization_service: AuthorizationService | None = None):
        self.path = Path(database_path)
        self.policy = load_traffic_change_policy(policy_path)
        self.clock = clock
        self.authorization_service = authorization_service
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()
        self.recover()

    def _authorize(self, principal: PrincipalContext | None, permission: str) -> TenantScope:
        if self.authorization_service is not None:
            verified = self.authorization_service.authorize(principal, permission)
            if (permission.startswith("scheduler.") and
                    (verified.principal_type is not PrincipalType.SERVICE or
                     "scheduler_service" not in verified.roles)):
                raise PermissionError("scheduler_service_identity_required")
            return TenantScope(verified.tenant_id, verified.workspace_id)
        return TenantScope.local_development()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS traffic_change_proposals(
              proposal_id TEXT NOT NULL, environment_id TEXT NOT NULL,
              channel_id TEXT NOT NULL, model_id TEXT NOT NULL,
              change_sha256 TEXT NOT NULL, proposer_id TEXT NOT NULL,
              state TEXT NOT NULL, rollout_percent REAL NOT NULL,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              activated_at TEXT, terminated_at TEXT, termination_reason TEXT,
              revision INTEGER NOT NULL, policy_version TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,proposal_id),
              CHECK(state IN ('PROPOSED','APPROVED','ACTIVE','ROLLED_BACK','EXPIRED','CANCELLED')));
            CREATE TABLE IF NOT EXISTS traffic_change_approvals(
              approval_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
              approver_id TEXT NOT NULL, approved_at TEXT NOT NULL,
              proposal_revision INTEGER NOT NULL, policy_version TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,approval_id),
              FOREIGN KEY(tenant_id,workspace_id,proposal_id)
                REFERENCES traffic_change_proposals(tenant_id,workspace_id,proposal_id));
            CREATE TABLE IF NOT EXISTS traffic_change_kill_switches(
              scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, active INTEGER NOT NULL,
              reason TEXT NOT NULL, actor_id TEXT NOT NULL, updated_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,scope_type,scope_id));
            CREATE TABLE IF NOT EXISTS traffic_change_idempotency(
              operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL,
              created_at TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,operation,idempotency_key));
            CREATE TABLE IF NOT EXISTS traffic_change_audit(
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT,
              event_type TEXT NOT NULL, actor_id TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
            CREATE TABLE IF NOT EXISTS traffic_policy_switch_proposals(
              proposal_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              source_policy_version TEXT NOT NULL,target_policy_version TEXT NOT NULL,
              requested_percentage REAL NOT NULL,reason TEXT NOT NULL,
              rollback_condition TEXT NOT NULL,state TEXT NOT NULL,
              proposer_id TEXT NOT NULL,created_at TEXT NOT NULL,
              policy_version TEXT NOT NULL,tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,proposal_id));
            CREATE TABLE IF NOT EXISTS traffic_change_control_proposals(
              proposal_id TEXT NOT NULL, environment_id TEXT NOT NULL,
              environment_mode TEXT NOT NULL, source_model_id TEXT,
              source_channel_id TEXT, target_model_id TEXT NOT NULL,
              target_channel_id TEXT, source_policy_version TEXT,
              target_policy_version TEXT NOT NULL, rollout_percent REAL NOT NULL,
              state TEXT NOT NULL, reason TEXT NOT NULL, approval_reference TEXT,
              approval_id TEXT, observation_seconds INTEGER NOT NULL,
              minimum_sample_count INTEGER NOT NULL, stop_conditions_json TEXT NOT NULL,
              rollback_condition TEXT NOT NULL, impact_json TEXT NOT NULL,
              linked_request_ids_json TEXT NOT NULL, linked_decision_ids_json TEXT NOT NULL,
              last_trigger_json TEXT, proposer_id TEXT NOT NULL, operator_id TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT,
              paused_at TEXT, completed_at TEXT, rolled_back_at TEXT,
              revision INTEGER NOT NULL, policy_version TEXT NOT NULL,
              acceptance_run_id TEXT, baseline_binding_json TEXT,
              candidate_binding_json TEXT, restored_binding_json TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,proposal_id));
            CREATE INDEX IF NOT EXISTS ix_traffic_control_state
              ON traffic_change_control_proposals(tenant_id,workspace_id,state,updated_at);
            CREATE TABLE IF NOT EXISTS traffic_change_metric_snapshots(
              snapshot_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
              collected_at TEXT NOT NULL, metrics_json TEXT NOT NULL,
              trigger_json TEXT, tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,snapshot_id));
            """)
            for table in ("traffic_change_proposals", "traffic_change_approvals",
                          "traffic_change_kill_switches", "traffic_change_idempotency",
                          "traffic_change_audit"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'")
                if "workspace_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'")
                db.execute(f"CREATE INDEX IF NOT EXISTS ix_scope_{table} ON {table}(tenant_id,workspace_id)")
            control_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(traffic_change_control_proposals)")}
            for name in ("acceptance_run_id", "baseline_binding_json",
                         "candidate_binding_json", "restored_binding_json"):
                if name not in control_columns:
                    db.execute(
                        f"ALTER TABLE traffic_change_control_proposals ADD COLUMN {name} TEXT")
            channel_info = next(row for row in db.execute(
                "PRAGMA table_info(traffic_change_control_proposals)")
                if row[1] == "target_channel_id")
            if channel_info[3]:
                # SQLite cannot drop a NOT NULL constraint in place. Rebuild once so
                # databases created by the channel-only version accept model canaries.
                db.execute("DROP INDEX IF EXISTS ix_traffic_control_state")
                db.execute("ALTER TABLE traffic_change_control_proposals RENAME TO traffic_change_control_legacy")
                db.execute("""CREATE TABLE traffic_change_control_proposals(
                  proposal_id TEXT NOT NULL, environment_id TEXT NOT NULL,
                  environment_mode TEXT NOT NULL, source_model_id TEXT,
                  source_channel_id TEXT, target_model_id TEXT NOT NULL,
                  target_channel_id TEXT, source_policy_version TEXT,
                  target_policy_version TEXT NOT NULL, rollout_percent REAL NOT NULL,
                  state TEXT NOT NULL, reason TEXT NOT NULL, approval_reference TEXT,
                  approval_id TEXT, observation_seconds INTEGER NOT NULL,
                  minimum_sample_count INTEGER NOT NULL, stop_conditions_json TEXT NOT NULL,
                  rollback_condition TEXT NOT NULL, impact_json TEXT NOT NULL,
                  linked_request_ids_json TEXT NOT NULL, linked_decision_ids_json TEXT NOT NULL,
                  last_trigger_json TEXT, proposer_id TEXT NOT NULL, operator_id TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT,
                  paused_at TEXT, completed_at TEXT, rolled_back_at TEXT,
                  revision INTEGER NOT NULL, policy_version TEXT NOT NULL,
                  acceptance_run_id TEXT, baseline_binding_json TEXT,
                  candidate_binding_json TEXT, restored_binding_json TEXT,
                  tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,proposal_id))""")
                columns = [row[1] for row in db.execute(
                    "PRAGMA table_info(traffic_change_control_legacy)")]
                names = ",".join(f'"{name}"' for name in columns)
                db.execute(f"INSERT INTO traffic_change_control_proposals({names}) SELECT {names} FROM traffic_change_control_legacy")
                db.execute("DROP TABLE traffic_change_control_legacy")
                db.execute("""CREATE INDEX IF NOT EXISTS ix_traffic_control_state
                  ON traffic_change_control_proposals(tenant_id,workspace_id,state,updated_at)""")

    def propose_policy_switch(self, *, environment_id: str,
                              source_policy_version: str,
                              target_policy_version: str,
                              requested_percentage: float, reason: str,
                              rollback_condition: str, proposer_id: str,
                              principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Persist a proposal only; this path deliberately has no activation API."""
        scope = self._authorize(principal, "exploration.manage")
        if environment_id != "china_uat":
            raise TrafficChangeGovernanceError("only_domestic_uat_proposal_allowed")
        if (not source_policy_version or not target_policy_version or not reason.strip()
                or not rollback_condition.strip()
                or not 0 < requested_percentage <= self.policy["maximum_rollout_percent"]):
            raise TrafficChangeGovernanceError("invalid_policy_switch_proposal")
        proposal_id = f"TPS-{uuid.uuid4().hex}"
        now = _iso(self.clock())
        with self.connect() as db:
            db.execute("""INSERT INTO traffic_policy_switch_proposals VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                proposal_id, environment_id, source_policy_version,
                target_policy_version, requested_percentage, reason.strip(),
                rollback_condition.strip(), "PROPOSED", proposer_id, now,
                self.policy["policy_version"], *scope.sql_parameters()))
            self._audit(db, proposal_id, "policy_switch_proposed", proposer_id,
                        {"requested_percentage": requested_percentage,
                         "source_policy_version": source_policy_version,
                         "target_policy_version": target_policy_version,
                         "production_change_allowed": False}, scope)
        return {"proposal_id": proposal_id, "state": "PROPOSED",
                "environment_id": environment_id,
                "requested_percentage": requested_percentage,
                "policy_version": self.policy["policy_version"],
                "production_change_allowed": False,
                "activation_endpoint_available": False}

    def _role(self, actor_id: str, actor_role: str, required: str) -> None:
        if not actor_id or actor_role not in self.policy["roles"][required]:
            raise TrafficChangeGovernanceError(f"{required}_role_required")

    def _audit(self, db: sqlite3.Connection, proposal_id: str | None,
               event: str, actor: str, details: Mapping[str, Any],
               scope: TenantScope | None = None) -> None:
        scope = scope or TenantScope.local_development()
        db.execute("INSERT INTO traffic_change_audit(proposal_id,event_type,actor_id,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)",
                   (proposal_id, event, actor, _iso(self.clock()), _canonical(details),
                    *scope.sql_parameters()))

    def _prior(self, db: sqlite3.Connection, operation: str, key: str,
               payload: Mapping[str, Any], scope: TenantScope | None = None) -> dict[str, Any] | None:
        scope = scope or TenantScope.local_development()
        row = db.execute("SELECT * FROM traffic_change_idempotency WHERE operation=? AND idempotency_key=? AND tenant_id=? AND workspace_id=?",
                         (operation, key, *scope.sql_parameters())).fetchone()
        if not row:
            return None
        digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        if row["request_sha256"] != digest:
            raise TrafficChangeGovernanceError("idempotency_key_payload_mismatch")
        return json.loads(row["response_json"])

    def _remember(self, db: sqlite3.Connection, operation: str, key: str,
                  payload: Mapping[str, Any], result: Mapping[str, Any],
                  scope: TenantScope | None = None) -> None:
        scope = scope or TenantScope.local_development()
        db.execute("""INSERT INTO traffic_change_idempotency(
          operation,idempotency_key,request_sha256,response_json,created_at,
          tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)""", (
            operation, key, hashlib.sha256(_canonical(payload).encode()).hexdigest(),
            _canonical(result), _iso(self.clock()), *scope.sql_parameters()))

    def propose(self, *, environment_id: str, channel_id: str, model_id: str,
                change: Mapping[str, Any], proposer_id: str, actor_role: str,
                rollout_percent: float, ttl_seconds: int, idempotency_key: str,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        self._role(proposer_id, actor_role, "proposer")
        if (not idempotency_key or not environment_id or not channel_id or not model_id
                or not change or not 0 < rollout_percent <= self.policy["maximum_rollout_percent"]
                or not 0 < ttl_seconds <= self.policy["maximum_ttl_seconds"]):
            raise TrafficChangeGovernanceError("invalid_traffic_change_proposal")
        payload = {"environment_id": environment_id, "channel_id": channel_id,
                   "model_id": model_id, "change": dict(change), "proposer_id": proposer_id,
                   "rollout_percent": rollout_percent, "ttl_seconds": ttl_seconds}
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = self._prior(db, "propose", idempotency_key, payload, scope)
            if prior: db.commit(); return prior
            proposal_id = f"TC-{uuid.uuid4().hex}"
            result = {"proposal_id": proposal_id, "state": "PROPOSED", "revision": 0,
                      "rollout_percent": float(rollout_percent), "expires_at": _iso(now + timedelta(seconds=ttl_seconds)),
                      "policy_version": self.policy["policy_version"], "real_execution_allowed": False,
                      "adapter": "offline_sandbox"}
            db.execute("""INSERT INTO traffic_change_proposals(
              proposal_id,environment_id,channel_id,model_id,change_sha256,proposer_id,
              state,rollout_percent,created_at,expires_at,activated_at,terminated_at,
              termination_reason,revision,policy_version,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                proposal_id, environment_id, channel_id, model_id,
                hashlib.sha256(_canonical(change).encode()).hexdigest(), proposer_id, "PROPOSED",
                rollout_percent, _iso(now), result["expires_at"], None, None, None, 0,
                self.policy["policy_version"], *scope.sql_parameters()))
            self._audit(db, proposal_id, "proposed", proposer_id, result, scope)
            self._remember(db, "propose", idempotency_key, payload, result, scope); db.commit()
        return result

    def approve(self, proposal_id: str, *, approver_id: str, actor_role: str,
                expected_revision: int, idempotency_key: str,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        self._role(approver_id, actor_role, "approver")
        payload = {"proposal_id": proposal_id, "approver_id": approver_id,
                   "expected_revision": expected_revision}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = self._prior(db, "approve", idempotency_key, payload, scope)
            if prior: db.commit(); return prior
            row = db.execute("SELECT * FROM traffic_change_proposals WHERE proposal_id=? AND tenant_id=? AND workspace_id=?",
                             (proposal_id, *scope.sql_parameters())).fetchone()
            if not row: raise TrafficChangeGovernanceError("proposal_not_found")
            if row["proposer_id"] == approver_id: raise TrafficChangeGovernanceError("proposer_approver_separation_required")
            if row["state"] != "PROPOSED": raise TrafficChangeGovernanceError("proposal_not_approvable")
            if row["revision"] != expected_revision: raise TrafficChangeGovernanceError("revision_conflict")
            if _parse(row["expires_at"]) <= self.clock(): raise TrafficChangeGovernanceError("proposal_expired")
            revision = expected_revision + 1; approval_id = f"TCA-{uuid.uuid4().hex}"
            updated = db.execute("""UPDATE traffic_change_proposals SET state='APPROVED',revision=?
                                 WHERE proposal_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                                 (revision, proposal_id, expected_revision, *scope.sql_parameters()))
            if updated.rowcount != 1: raise TrafficChangeGovernanceError("revision_conflict")
            db.execute("""INSERT INTO traffic_change_approvals(
              approval_id,proposal_id,approver_id,approved_at,proposal_revision,
              policy_version,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?)""", (
                approval_id, proposal_id, approver_id, _iso(self.clock()), expected_revision,
                self.policy["policy_version"], *scope.sql_parameters()))
            result = {"proposal_id": proposal_id, "approval_id": approval_id,
                      "state": "APPROVED", "revision": revision, "real_execution_allowed": False}
            self._audit(db, proposal_id, "approved", approver_id, result, scope)
            self._remember(db, "approve", idempotency_key, payload, result, scope); db.commit()
        return result

    def _killed(self, db: sqlite3.Connection, row: sqlite3.Row, scope: TenantScope) -> bool:
        scopes = {("global", "*"), ("environment", row["environment_id"]),
                  ("channel", row["channel_id"]), ("model", row["model_id"])}
        return any((r["scope_type"], r["scope_id"]) in scopes for r in db.execute(
            """SELECT scope_type,scope_id FROM traffic_change_kill_switches
               WHERE active=1 AND tenant_id=? AND workspace_id=?""",
            scope.sql_parameters()))

    def activate(self, proposal_id: str, *, operator_id: str, actor_role: str,
                 expected_revision: int, gate_results: Mapping[str, bool],
                 idempotency_key: str,
                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "scheduler.execute")
        self._role(operator_id, actor_role, "operator")
        if not self.policy["enabled"]:
            raise TrafficChangeGovernanceError("traffic_change_policy_disabled")
        payload = {"proposal_id": proposal_id, "operator_id": operator_id,
                   "expected_revision": expected_revision, "gate_results": dict(gate_results)}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = self._prior(db, "activate", idempotency_key, payload, scope)
            if prior: db.commit(); return prior
            row = db.execute("SELECT * FROM traffic_change_proposals WHERE proposal_id=? AND tenant_id=? AND workspace_id=?",
                             (proposal_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] != "APPROVED": raise TrafficChangeGovernanceError("proposal_not_activatable")
            if row["revision"] != expected_revision: raise TrafficChangeGovernanceError("revision_conflict")
            if _parse(row["expires_at"]) <= self.clock(): raise TrafficChangeGovernanceError("proposal_expired")
            required = set(self.policy["required_gates"])
            if set(gate_results) != required or not all(gate_results.values()):
                raise TrafficChangeGovernanceError("traffic_change_gate_failed")
            if self._killed(db, row, scope): raise TrafficChangeGovernanceError("kill_switch_active")
            revision = expected_revision + 1
            db.execute("""UPDATE traffic_change_proposals SET state='ACTIVE',activated_at=?,revision=?
                       WHERE proposal_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (_iso(self.clock()), revision, proposal_id, expected_revision, *scope.sql_parameters()))
            result = {"proposal_id": proposal_id, "state": "ACTIVE", "revision": revision,
                      "rollout_percent": row["rollout_percent"], "adapter": "offline_sandbox",
                      "real_execution_allowed": False, "network_called": False}
            self._audit(db, proposal_id, "sandbox_rollout_activated", operator_id, result, scope)
            self._remember(db, "activate", idempotency_key, payload, result, scope); db.commit()
        return result

    def terminate(self, proposal_id: str, *, actor_id: str, actor_role: str,
                  expected_revision: int, reason: str, emergency: bool = False,
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "kill_switch.activate" if emergency else "exploration.manage")
        self._role(actor_id, actor_role, "emergency" if emergency else "operator")
        target = "ROLLED_BACK" if emergency else "CANCELLED"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM traffic_change_proposals WHERE proposal_id=? AND tenant_id=? AND workspace_id=?",
                             (proposal_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] in self.TERMINAL: raise TrafficChangeGovernanceError("proposal_not_terminable")
            if row["revision"] != expected_revision: raise TrafficChangeGovernanceError("revision_conflict")
            db.execute("""UPDATE traffic_change_proposals SET state=?,terminated_at=?,termination_reason=?,revision=revision+1
                       WHERE proposal_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (target, _iso(self.clock()), reason, proposal_id, expected_revision, *scope.sql_parameters()))
            result = {"proposal_id": proposal_id, "state": target, "revision": expected_revision + 1,
                      "real_execution_allowed": False}
            self._audit(db, proposal_id, "emergency_rollback" if emergency else "cancelled", actor_id, result, scope)
            db.commit(); return result

    def set_kill_switch(self, *, scope_type: str, scope_id: str, active: bool,
                        reason: str, actor_id: str, actor_role: str,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "kill_switch.activate" if active else "kill_switch.release")
        self._role(actor_id, actor_role, "emergency")
        if scope_type not in {"global", "environment", "channel", "model"} or not reason:
            raise TrafficChangeGovernanceError("invalid_kill_switch")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO traffic_change_kill_switches(
              scope_type,scope_id,active,reason,actor_id,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,workspace_id,scope_type,scope_id)
              DO UPDATE SET active=excluded.active,
              reason=excluded.reason,actor_id=excluded.actor_id,updated_at=excluded.updated_at""",
              (scope_type, scope_id, int(active), reason, actor_id, _iso(self.clock()),
               *scope.sql_parameters()))
            if active:
                rows = db.execute("SELECT * FROM traffic_change_proposals WHERE state='ACTIVE' AND tenant_id=? AND workspace_id=?",
                                  scope.sql_parameters()).fetchall()
                for row in rows:
                    if self._killed(db, row, scope):
                        db.execute("""UPDATE traffic_change_proposals SET state='ROLLED_BACK',terminated_at=?,
                                   termination_reason='kill_switch',revision=revision+1
                                   WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
                                   (_iso(self.clock()), row["proposal_id"], *scope.sql_parameters()))
                        self._audit(db, row["proposal_id"], "kill_switch_rollback", actor_id, {"scope_type": scope_type, "scope_id": scope_id}, scope)
            result = {"scope_type": scope_type, "scope_id": scope_id, "active": bool(active),
                      "real_execution_allowed": False}
            self._audit(db, None, "kill_switch_changed", actor_id, result, scope); db.commit(); return result

    def recover(self) -> dict[str, int]:
        now = _iso(self.clock()); expired = 0; rolled_back = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id FROM traffic_change_proposals
                                 WHERE state IN ('PROPOSED','APPROVED','ACTIVE') AND expires_at<=?""",
                                 (now,)).fetchall()
            for item in scopes:
                scope = TenantScope(item["tenant_id"], item["workspace_id"])
                rows = db.execute("""SELECT * FROM traffic_change_proposals
                                  WHERE state IN ('PROPOSED','APPROVED','ACTIVE') AND expires_at<=?
                                  AND tenant_id=? AND workspace_id=?""",
                                  (now, *scope.sql_parameters())).fetchall()
                for row in rows:
                    target = "ROLLED_BACK" if row["state"] == "ACTIVE" else "EXPIRED"
                    db.execute("""UPDATE traffic_change_proposals SET state=?,terminated_at=?,
                               termination_reason='expired',revision=revision+1
                               WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
                               (target, now, row["proposal_id"], *scope.sql_parameters()))
                    self._audit(db, row["proposal_id"], "recovery_expiry", "system",
                                {"from": row["state"], "to": target}, scope)
                    rolled_back += target == "ROLLED_BACK"; expired += target == "EXPIRED"
            db.commit()
        return {"expired": expired, "rolled_back": rolled_back}

    def get(self, proposal_id: str, *,
            principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        with self.connect() as db:
            row = db.execute("SELECT * FROM traffic_change_proposals WHERE proposal_id=? AND tenant_id=? AND workspace_id=?",
                             (proposal_id, *scope.sql_parameters())).fetchone()
            approvals = db.execute("SELECT * FROM traffic_change_approvals WHERE proposal_id=? AND tenant_id=? AND workspace_id=? ORDER BY approved_at",
                                   (proposal_id, *scope.sql_parameters())).fetchall()
        if not row: raise TrafficChangeGovernanceError("proposal_not_found")
        return {**dict(row), "approvals": [dict(x) for x in approvals],
                "real_execution_allowed": False, "network_called": False}

    def list_audit(self, proposal_id: str | None = None, *,
                   principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._authorize(principal, "audit.read")
        query = "SELECT * FROM traffic_change_audit WHERE tenant_id=? AND workspace_id=?"
        args: tuple[Any, ...] = scope.sql_parameters()
        if proposal_id is not None: query += " AND proposal_id=?"; args = (*args, proposal_id)
        query += " ORDER BY audit_id"
        with self.connect() as db: rows = db.execute(query, args).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def monitoring_status(self, limit: int = 100, *,
                          principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        if not 1 <= limit <= 500: raise TrafficChangeGovernanceError("invalid_status_limit")
        with self.connect() as db:
            counts = {row["state"]: row["count"] for row in db.execute(
                """SELECT state,COUNT(*) count FROM traffic_change_proposals
                   WHERE tenant_id=? AND workspace_id=? GROUP BY state""",
                scope.sql_parameters())}
            rows = db.execute("""SELECT proposal_id,environment_id,channel_id,model_id,state,
              rollout_percent,created_at,expires_at,activated_at,terminated_at,termination_reason,
              revision,policy_version FROM traffic_change_proposals
              WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC,proposal_id LIMIT ?""", (*scope.sql_parameters(), limit)).fetchall()
            switches = db.execute("""SELECT scope_type,scope_id,active,updated_at
                                  FROM traffic_change_kill_switches
                                  WHERE tenant_id=? AND workspace_id=? ORDER BY scope_type,scope_id""",
                                  scope.sql_parameters()).fetchall()
        return {"status": "disabled" if not self.policy["enabled"] else "sandbox_only",
                "capability_id": "ADV-017", "enabled": bool(self.policy["enabled"]),
                "mode": "offline_sandbox", "real_execution_allowed": False,
                "policy_version": self.policy["policy_version"], "state_counts": counts,
                "maximum_rollout_percent": self.policy["maximum_rollout_percent"],
                "required_gates": list(self.policy["required_gates"]),
                "items": [{key:value for key,value in dict(row).items() if key!="termination_reason"} for row in rows],
                "kill_switches": [dict(row) for row in switches],
                "authorization_status": "identity_provider_and_separate_authorization_required",
                "network_called": False}

    def monitoring_audit(self, limit: int = 100, *,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "audit.read")
        if not 1 <= limit <= 500: raise TrafficChangeGovernanceError("invalid_audit_limit")
        with self.connect() as db:
            rows = db.execute("""SELECT audit_id,proposal_id,event_type,created_at
                              FROM traffic_change_audit WHERE tenant_id=? AND workspace_id=?
                              ORDER BY audit_id DESC LIMIT ?""",
                              (*scope.sql_parameters(), limit)).fetchall()
        return {"status": "ready", "capability_id": "ADV-017", "items": [dict(row) for row in rows],
                "network_called": False}

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * percentile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return round(ordered[lower], 3)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)

    def _call_rows(self, db: sqlite3.Connection, scope: TenantScope, *,
                   environment_id: str = "china_uat", model_id: str | None = None,
                   channel_id: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "standardized_call_logs" not in tables:
            return []
        query = """SELECT * FROM standardized_call_logs
          WHERE tenant_id=? AND workspace_id=? AND environment_id=?
          AND COALESCE(duplicate_status,'canonical')='canonical'"""
        args: list[Any] = [*scope.sql_parameters(), environment_id]
        if model_id:
            query += " AND COALESCE(actual_model,requested_model)=?"
            args.append(model_id)
        if channel_id:
            query += " AND channel_id=? AND channel_source!='unknown'"
            args.append(channel_id)
        if since:
            query += " AND occurred_at>=?"
            args.append(since)
        query += " ORDER BY occurred_at"
        return db.execute(query, tuple(args)).fetchall()

    def _metrics(self, rows: list[sqlite3.Row]) -> dict[str, Any]:
        count = len(rows)
        successes = sum(str(row["request_status"]).upper() == "SUCCESS" for row in rows)
        latencies = [float(row["total_latency_ms"]) for row in rows
                     if row["total_latency_ms"] is not None]
        costs = [float(row["cost_amount"]) for row in rows if row["cost_amount"] is not None]
        fallback_count = sum(int(row["total_attempts"] or 1) > 1 for row in rows)
        tokens = sum(int(row["input_tokens"] or 0) + int(row["cached_input_tokens"] or 0)
                     + int(row["output_tokens"] or 0) for row in rows)
        return {
            "request_count": count,
            "success_rate": round(successes / count, 6) if count else None,
            "error_rate": round((count - successes) / count, 6) if count else None,
            "p50_latency_ms": self._percentile(latencies, .50),
            "p95_latency_ms": self._percentile(latencies, .95),
            "p99_latency_ms": self._percentile(latencies, .99),
            "token_count": tokens if count else None,
            "total_cost": round(sum(costs), 8) if costs else None,
            "average_cost": round(sum(costs) / len(costs), 8) if costs else None,
            "fallback_rate": round(fallback_count / count, 6) if count else None,
            "coverage": {
                "latency": round(len(latencies) / count, 6) if count else 0,
                "cost": round(len(costs) / count, 6) if count else 0,
            },
            "latest_at": rows[-1]["occurred_at"] if rows else None,
        }

    def control_current(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        since = _iso(self.clock() - timedelta(hours=24))
        with self.connect() as db:
            rows = self._call_rows(db, scope, since=since)
            config = None
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "configuration_versions" in tables:
                config = db.execute("""SELECT configuration_version,policy_version,created_at
                  FROM configuration_versions WHERE tenant_id=? AND workspace_id=? AND is_active=1
                  ORDER BY revision DESC LIMIT 1""", scope.sql_parameters()).fetchone()
        authoritative = [row for row in rows if row["channel_id"] and row["channel_source"] != "unknown"]
        latest = rows[-1] if rows else None
        latest_channel = authoritative[-1] if authoritative else None
        distribution: dict[str, int] = {}
        for row in authoritative:
            key = f"{row['actual_model'] or row['requested_model']}::{row['channel_id']}"
            distribution[key] = distribution.get(key, 0) + 1
        total = sum(distribution.values())
        return {
            "status": "ready", "environment_id": "china_uat",
            "current_mode": "sandbox_validation",
            "production_execution": "not_configured",
            "policy_version": config["policy_version"] if config else None,
            "configuration_version": config["configuration_version"] if config else None,
            "current_model_id": (latest["actual_model"] or latest["requested_model"]) if latest else None,
            "current_channel_id": latest_channel["channel_id"] if latest_channel else None,
            "traffic_distribution": [
                {"model_id": key.split("::", 1)[0], "channel_id": key.split("::", 1)[1],
                 "percentage": round(value / total * 100, 2), "request_count": value}
                for key, value in sorted(distribution.items(), key=lambda item: item[1], reverse=True)
            ],
            "metrics_24h": self._metrics(rows),
            "channel_evidence_available": bool(authoritative),
            "data_updated_at": rows[-1]["updated_at"] if rows else None,
            "network_called": False,
        }

    def control_channels(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        with self.connect() as db:
            rows = self._call_rows(db, scope)
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row["channel_id"] or row["channel_source"] == "unknown":
                continue
            channel = found.setdefault(row["channel_id"], {
                "channel_id": row["channel_id"], "channel_name": row["channel_name"],
                "channel_source": row["channel_source"], "models": set(),
                "last_seen_at": row["occurred_at"], "sample_count": 0})
            channel["models"].add(row["actual_model"] or row["requested_model"])
            channel["last_seen_at"] = max(channel["last_seen_at"], row["occurred_at"])
            channel["sample_count"] += 1
        return {"status": "ready", "items": [
            {**item, "models": sorted(item["models"])} for item in found.values()],
            "total": len(found), "network_called": False}

    def _impact(self, db: sqlite3.Connection, scope: TenantScope, *,
                target_model_id: str, target_channel_id: str | None = None) -> dict[str, Any]:
        baseline_rows = self._call_rows(db, scope)
        target_rows = self._call_rows(db, scope, model_id=target_model_id,
                                      channel_id=target_channel_id)
        baseline = self._metrics(baseline_rows)
        target = self._metrics(target_rows)
        def delta(name: str) -> float | None:
            left, right = baseline.get(name), target.get(name)
            return round(float(right) - float(left), 8) if left is not None and right is not None else None
        authoritative = [row for row in target_rows
                         if row["channel_id"] and row["channel_source"] != "unknown"]
        channel_counts: dict[str, int] = {}
        for row in authoritative:
            channel_counts[row["channel_id"]] = channel_counts.get(row["channel_id"], 0) + 1
        channel_total = sum(channel_counts.values())
        channel_hhi = (round(sum((count / channel_total) ** 2
                                 for count in channel_counts.values()), 6)
                       if channel_total else None)
        return {
            "status": "ready" if target_rows else "insufficient_evidence",
            "baseline": baseline, "target": target,
            "difference": {name: delta(name) for name in (
                "success_rate", "p95_latency_ms", "average_cost", "fallback_rate")},
            "coverage_rate": round(len(target_rows) / len(baseline_rows), 6) if baseline_rows else 0,
            "channel_concentration": channel_hhi,
            "channel_hhi": channel_hhi,
            "channel_evidence_count": channel_total,
            "metrics_scope": "model_channel" if target_channel_id else "model",
            "limitations": [] if target_rows else ["目标模型与渠道没有权威历史样本，无法估算。"],
            "calculation_basis": "去重后的统一真实调用日志",
        }

    def control_impact(self, *, target_model_id: str, target_channel_id: str | None = None,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        if not target_model_id:
            raise TrafficChangeGovernanceError("target_model_required")
        with self.connect() as db:
            return self._impact(db, scope, target_model_id=target_model_id,
                                target_channel_id=target_channel_id)

    def create_control_proposal(self, payload: Mapping[str, Any], *, actor_id: str,
                                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        mode = str(payload.get("environment_mode") or "sandbox")
        if mode not in {"sandbox", "china_uat", "production"}:
            raise TrafficChangeGovernanceError("invalid_environment_mode")
        if mode == "production" and not self.policy["real_adapter_enabled"]:
            raise TrafficChangeGovernanceError("production_adapter_not_configured")
        target_model = str(payload.get("target_model_id") or "").strip()
        target_channel = str(payload.get("target_channel_id") or "").strip() or None
        target_policy = str(payload.get("target_policy_version") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        rollback = str(payload.get("rollback_condition") or "").strip()
        rollout = float(payload.get("rollout_percent") or 0)
        observation = int(payload.get("observation_seconds") or 0)
        minimum = int(payload.get("minimum_sample_count") or 0)
        stop_conditions = payload.get("stop_conditions") or {}
        if (not target_model or not target_policy or not reason or
                not rollback or not 0 < rollout <= float(self.policy["maximum_rollout_percent"])
                or observation <= 0 or minimum <= 0 or
                not isinstance(stop_conditions, Mapping) or not stop_conditions):
            raise TrafficChangeGovernanceError("invalid_control_proposal")
        proposal_id = f"TCG-{uuid.uuid4().hex.upper()}"
        now = _iso(self.clock())
        with self.connect() as db:
            impact = self._impact(db, scope, target_model_id=target_model,
                                  target_channel_id=target_channel)
            baseline_binding = payload.get("baseline_binding") or {
                "model_id": payload.get("source_model_id"),
                "channel_id": payload.get("source_channel_id"),
                "policy_version": payload.get("source_policy_version"),
            }
            candidate_binding = payload.get("candidate_binding") or {
                "model_id": target_model,
                "channel_id": target_channel,
                "policy_version": target_policy,
            }
            if not isinstance(baseline_binding, Mapping) or not isinstance(candidate_binding, Mapping):
                raise TrafficChangeGovernanceError("invalid_control_binding")
            db.execute("""INSERT INTO traffic_change_control_proposals(
              proposal_id,environment_id,environment_mode,source_model_id,source_channel_id,
              target_model_id,target_channel_id,source_policy_version,target_policy_version,
              rollout_percent,state,reason,approval_reference,approval_id,observation_seconds,
              minimum_sample_count,stop_conditions_json,rollback_condition,impact_json,
              linked_request_ids_json,linked_decision_ids_json,last_trigger_json,proposer_id,
              operator_id,created_at,updated_at,started_at,paused_at,completed_at,rolled_back_at,
              revision,policy_version,acceptance_run_id,baseline_binding_json,
              candidate_binding_json,restored_binding_json,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                proposal_id, "china_uat", mode, payload.get("source_model_id"),
                payload.get("source_channel_id"), target_model, target_channel,
                payload.get("source_policy_version"), target_policy, rollout, "DRAFT", reason,
                payload.get("approval_reference"), None, observation, minimum,
                _canonical(stop_conditions), rollback, _canonical(impact), "[]", "[]", None,
                actor_id, None, now, now, None, None, None, None, 0,
                self.policy["policy_version"], payload.get("acceptance_run_id"),
                _canonical(baseline_binding), _canonical(candidate_binding), None,
                *scope.sql_parameters()))
            self._audit(db, proposal_id, "control_draft_created", actor_id,
                        {"environment_mode": mode, "target_model_id": target_model,
                         "target_channel_id": target_channel, "rollout_percent": rollout}, scope)
        return self.control_get(proposal_id, principal=principal)

    def _control_transition(self, proposal_id: str, *, allowed: set[str], target: str,
                            actor_id: str, event: str, details: Mapping[str, Any] | None = None,
                            principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM traffic_change_control_proposals
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters())).fetchone()
            if not row:
                raise TrafficChangeGovernanceError("proposal_not_found")
            if row["state"] not in allowed:
                raise TrafficChangeGovernanceError("invalid_control_state_transition")
            now = _iso(self.clock())
            fields = {"updated_at": now, "revision": int(row["revision"]) + 1}
            if target == "CANARY_RUNNING": fields.update(started_at=now, paused_at=None, operator_id=actor_id)
            if target == "PAUSED": fields["paused_at"] = now
            if target == "COMPLETED": fields["completed_at"] = now
            if target == "ROLLED_BACK": fields["rolled_back_at"] = now
            if target == "APPROVED": fields["approval_id"] = f"TCA-{uuid.uuid4().hex.upper()}"
            assignments = ",".join(f"{name}=?" for name in fields)
            db.execute(f"UPDATE traffic_change_control_proposals SET state=?,{assignments} "
                       "WHERE proposal_id=? AND tenant_id=? AND workspace_id=?",
                       (target, *fields.values(), proposal_id, *scope.sql_parameters()))
            self._audit(db, proposal_id, event, actor_id,
                        {"from_state": row["state"], "to_state": target, **dict(details or {})}, scope)
            db.commit()
        return self.control_get(proposal_id, principal=principal)

    def control_validate(self, proposal_id: str, *, actor_id: str,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        proposal = self.control_get(proposal_id, principal=principal)
        if proposal["impact"]["status"] == "insufficient_evidence" and proposal["environment_mode"] != "sandbox":
            raise TrafficChangeGovernanceError("impact_evidence_insufficient")
        return self._control_transition(proposal_id, allowed={"DRAFT"}, target="VALIDATED",
          actor_id=actor_id, event="control_validated", principal=principal)

    def control_submit(self, proposal_id: str, *, actor_id: str,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._control_transition(proposal_id, allowed={"VALIDATED"}, target="PENDING_APPROVAL",
          actor_id=actor_id, event="control_submitted", principal=principal)

    def control_approve(self, proposal_id: str, *, actor_id: str,
                        approval_reference: str | None = None,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        proposal = self.control_get(proposal_id, principal=principal)
        if proposal["environment_mode"] == "production" and not str(approval_reference or "").strip():
            raise TrafficChangeGovernanceError("approval_reference_required")
        result = self._control_transition(proposal_id, allowed={"PENDING_APPROVAL"}, target="APPROVED",
          actor_id=actor_id, event="control_approved",
          details={"approval_reference_present": bool(approval_reference)}, principal=principal)
        if approval_reference:
            scope = self._authorize(principal, "exploration.manage")
            with self.connect() as db:
                db.execute("""UPDATE traffic_change_control_proposals SET approval_reference=?
                  WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
                  (approval_reference.strip(), proposal_id, *scope.sql_parameters()))
        return self.control_get(proposal_id, principal=principal)

    def control_reject(self, proposal_id: str, *, actor_id: str, reason: str,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._control_transition(proposal_id, allowed={"PENDING_APPROVAL"}, target="REJECTED",
          actor_id=actor_id, event="control_rejected", details={"reason": reason}, principal=principal)

    def control_activate(self, proposal_id: str, *, actor_id: str,
                         request_ids: list[str] | None = None,
                         decision_ids: list[str] | None = None,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        result = self._control_transition(proposal_id, allowed={"APPROVED"}, target="CANARY_RUNNING",
          actor_id=actor_id, event="control_canary_started", principal=principal)
        if request_ids or decision_ids:
            request_ids = request_ids or []
            decision_ids = decision_ids or []
            for index in range(max(len(request_ids), len(decision_ids))):
                self.control_link_execution(
                    proposal_id,
                    request_id=request_ids[index] if index < len(request_ids) else None,
                    decision_id=decision_ids[index] if index < len(decision_ids) else None,
                    actor_id=actor_id, principal=principal)
        return self.control_get(proposal_id, principal=principal)

    def control_pause(self, proposal_id: str, *, actor_id: str,
                      principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._control_transition(proposal_id, allowed={"CANARY_RUNNING"}, target="PAUSED",
          actor_id=actor_id, event="control_paused", principal=principal)

    def control_resume(self, proposal_id: str, *, actor_id: str,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._control_transition(proposal_id, allowed={"PAUSED"}, target="CANARY_RUNNING",
          actor_id=actor_id, event="control_resumed", principal=principal)

    def control_adjust(self, proposal_id: str, *, actor_id: str, rollout_percent: float,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        if not 0 < rollout_percent <= float(self.policy["maximum_rollout_percent"]):
            raise TrafficChangeGovernanceError("invalid_rollout_percent")
        scope = self._authorize(principal, "exploration.manage")
        with self.connect() as db:
            row = db.execute("""SELECT state,rollout_percent,revision FROM traffic_change_control_proposals
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] not in {"CANARY_RUNNING", "PAUSED"}:
                raise TrafficChangeGovernanceError("proposal_not_adjustable")
            db.execute("""UPDATE traffic_change_control_proposals SET rollout_percent=?,updated_at=?,
              revision=revision+1 WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (rollout_percent, _iso(self.clock()), proposal_id, *scope.sql_parameters()))
            self._audit(db, proposal_id, "control_rollout_adjusted", actor_id,
                        {"before": row["rollout_percent"], "after": rollout_percent}, scope)
        return self.control_get(proposal_id, principal=principal)

    def control_rollback(self, proposal_id: str, *, actor_id: str, reason: str,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        proposal = self.control_get(proposal_id, principal=principal)
        restored = proposal.get("baseline_binding") or {
            "model_id": proposal.get("source_model_id"),
            "channel_id": proposal.get("source_channel_id"),
            "policy_version": proposal.get("source_policy_version"),
        }
        self._control_transition(proposal_id,
          allowed={"CANARY_RUNNING", "PAUSED", "AUTO_STOPPED"}, target="ROLLED_BACK",
          actor_id=actor_id, event="control_rolled_back",
          details={"reason": reason, "restored_binding": restored}, principal=principal)
        scope = self._authorize(principal, "exploration.manage")
        with self.connect() as db:
            db.execute("""UPDATE traffic_change_control_proposals SET restored_binding_json=?
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (_canonical(restored), proposal_id, *scope.sql_parameters()))
        return self.control_get(proposal_id, principal=principal)

    def control_complete(self, proposal_id: str, *, actor_id: str,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._control_transition(proposal_id, allowed={"CANARY_RUNNING"}, target="COMPLETED",
          actor_id=actor_id, event="control_completed", principal=principal)

    def control_get(self, proposal_id: str, *,
                    principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        with self.connect() as db:
            row = db.execute("""SELECT * FROM traffic_change_control_proposals
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters())).fetchone()
        if not row:
            raise TrafficChangeGovernanceError("proposal_not_found")
        result = dict(row)
        for source, target in (("stop_conditions_json", "stop_conditions"),
                               ("impact_json", "impact"),
                               ("linked_request_ids_json", "request_ids"),
                               ("linked_decision_ids_json", "decision_ids"),
                               ("last_trigger_json", "last_trigger"),
                               ("baseline_binding_json", "baseline_binding"),
                               ("candidate_binding_json", "candidate_binding"),
                               ("restored_binding_json", "restored_binding")):
            raw = result.pop(source)
            result[target] = json.loads(raw) if raw else None
        result["target_channel_id"] = result["target_channel_id"] or None
        result["baseline_binding"] = result["baseline_binding"] or {
            "model_id": result.get("source_model_id"),
            "channel_id": result.get("source_channel_id"),
            "policy_version": result.get("source_policy_version"),
        }
        result["candidate_binding"] = result["candidate_binding"] or {
            "model_id": result.get("target_model_id"),
            "channel_id": result.get("target_channel_id"),
            "policy_version": result.get("target_policy_version"),
        }
        return result

    def control_list(self, limit: int = 100, *,
                     principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        with self.connect() as db:
            ids = [row[0] for row in db.execute("""SELECT proposal_id
              FROM traffic_change_control_proposals WHERE tenant_id=? AND workspace_id=?
              ORDER BY updated_at DESC LIMIT ?""", (*scope.sql_parameters(), limit))]
        return {"status": "ready", "items": [self.control_get(item, principal=principal) for item in ids],
                "total": len(ids), "network_called": False}

    def control_delete(self, proposal_id: str, *, actor_id: str,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT state FROM traffic_change_control_proposals
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters())).fetchone()
            if not row:
                raise TrafficChangeGovernanceError("proposal_not_found")
            if row["state"] != "DRAFT":
                raise TrafficChangeGovernanceError("only_draft_can_be_deleted")
            self._audit(db, proposal_id, "control_draft_deleted", actor_id,
                        {"from_state": "DRAFT"}, scope)
            db.execute("""DELETE FROM traffic_change_control_proposals
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters()))
            db.commit()
        return {"proposal_id": proposal_id, "state": "DELETED", "network_called": False}

    def control_active_proposal(self, *, environment_id: str = "china_uat",
                                model_id: str | None = None,
                                principal: PrincipalContext | None = None) -> dict[str, Any] | None:
        """Return the newest running proposal eligible for a request."""
        scope = self._authorize(principal, "exploration.read")
        query = """SELECT proposal_id FROM traffic_change_control_proposals
          WHERE tenant_id=? AND workspace_id=? AND environment_id=?
          AND state='CANARY_RUNNING'"""
        args: list[Any] = [*scope.sql_parameters(), environment_id]
        if model_id:
            query += " AND (source_model_id=? OR target_model_id=?)"
            args.extend((model_id, model_id))
        query += " ORDER BY updated_at DESC,proposal_id DESC LIMIT 1"
        with self.connect() as db:
            row = db.execute(query, tuple(args)).fetchone()
        return self.control_get(row["proposal_id"], principal=principal) if row else None

    # Short alias intended for request-path integration.
    active_control_proposal = control_active_proposal
    get_active_control_proposal = control_active_proposal

    def control_link_execution(self, proposal_id: str, *, request_id: str | None = None,
                               decision_id: str | None = None, actor_id: str = "system",
                               principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Idempotently attach real execution evidence to a proposal."""
        if not request_id and not decision_id:
            raise TrafficChangeGovernanceError("request_or_decision_id_required")
        scope = self._authorize(principal, "exploration.manage")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT linked_request_ids_json,linked_decision_ids_json
              FROM traffic_change_control_proposals
              WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters())).fetchone()
            if not row:
                raise TrafficChangeGovernanceError("proposal_not_found")
            request_ids = json.loads(row["linked_request_ids_json"] or "[]")
            decision_ids = json.loads(row["linked_decision_ids_json"] or "[]")
            changed = False
            if request_id and request_id not in request_ids:
                request_ids.append(request_id)
                changed = True
            if decision_id and decision_id not in decision_ids:
                decision_ids.append(decision_id)
                changed = True
            if changed:
                db.execute("""UPDATE traffic_change_control_proposals
                  SET linked_request_ids_json=?,linked_decision_ids_json=?,updated_at=?
                  WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
                  (json.dumps(request_ids, separators=(",", ":")),
                   json.dumps(decision_ids, separators=(",", ":")), _iso(self.clock()),
                   proposal_id, *scope.sql_parameters()))
                self._audit(db, proposal_id, "control_execution_linked", actor_id,
                            {"request_id": request_id, "decision_id": decision_id}, scope)
            db.commit()
        return self.control_get(proposal_id, principal=principal)

    link_control_execution = control_link_execution

    def control_assignment(self, assignment_key: str, *, proposal_id: str | None = None,
                           environment_id: str = "china_uat", model_id: str | None = None,
                           request_id: str | None = None, decision_id: str | None = None,
                           actor_id: str = "system",
                           principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Assign baseline/candidate by a stable SHA-256 bucket and optionally link evidence."""
        if not str(assignment_key or "").strip():
            raise TrafficChangeGovernanceError("assignment_key_required")
        proposal = (self.control_get(proposal_id, principal=principal) if proposal_id else
                    self.control_active_proposal(environment_id=environment_id,
                                                 model_id=model_id, principal=principal))
        if not proposal or proposal["state"] != "CANARY_RUNNING":
            raise TrafficChangeGovernanceError("active_control_proposal_not_found")
        rollout = float(proposal["rollout_percent"])
        digest = hashlib.sha256(
            f"{proposal['proposal_id']}:{assignment_key}".encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % 10_000
        variant = "candidate" if bucket < round(rollout * 100) else "baseline"
        if request_id or decision_id:
            self.control_link_execution(
                proposal["proposal_id"], request_id=request_id, decision_id=decision_id,
                actor_id=actor_id, principal=principal)
        return {
            "proposal_id": proposal["proposal_id"],
            "acceptance_run_id": proposal.get("acceptance_run_id"),
            "assignment_key": assignment_key,
            "assignment_method": "sha256_mod_10000_v1",
            "bucket": bucket,
            "rollout_percent": rollout,
            "variant": variant,
            "binding": proposal[f"{variant}_binding"],
            "baseline_binding": proposal["baseline_binding"],
            "candidate_binding": proposal["candidate_binding"],
            "proposal_binding": {
                "proposal_id": proposal["proposal_id"],
                "acceptance_run_id": proposal.get("acceptance_run_id"),
                "variant": variant,
                "assignment_method": "sha256_mod_10000_v1",
                "bucket": bucket,
            },
            "network_called": False,
        }

    assign_control_variant = control_assignment
    assign_canary_variant = control_assignment

    def _control_metric_result(self, proposal_id: str, *,
                               principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        proposal = self.control_get(proposal_id, principal=principal)
        with self.connect() as db:
            rows = self._call_rows(db, scope, model_id=proposal["target_model_id"],
                                   channel_id=proposal["target_channel_id"],
                                   since=proposal["started_at"])
        metrics = self._metrics(rows)
        conditions = proposal["stop_conditions"] or {}
        triggers: list[dict[str, Any]] = []
        checks = {
            "error_rate_above": metrics["error_rate"],
            "p95_latency_ms_above": metrics["p95_latency_ms"],
            "success_rate_below": metrics["success_rate"],
            "average_cost_increase_above": None,
            "fallback_rate_above": metrics["fallback_rate"],
        }
        for name, actual in checks.items():
            threshold = conditions.get(name)
            if threshold is None or actual is None:
                continue
            breached = actual < float(threshold) if name == "success_rate_below" else actual > float(threshold)
            if breached:
                triggers.append({"condition": name, "actual": actual, "threshold": threshold})
        if conditions.get("consecutive_failures"):
            consecutive = 0
            for row in reversed(rows):
                if str(row["request_status"]).upper() == "SUCCESS":
                    break
                consecutive += 1
            if consecutive >= int(conditions["consecutive_failures"]):
                triggers.append({"condition": "consecutive_failures", "actual": consecutive,
                                 "threshold": conditions["consecutive_failures"]})
        return {"status": "ready", "proposal_id": proposal_id,
                "proposal_state": proposal["state"],
                "rollout_percent": proposal["rollout_percent"], "metrics": metrics,
                "metrics_scope": "model_channel" if proposal["target_channel_id"] else "model",
                "stop_conditions": conditions, "triggers": triggers,
                "collected_at": _iso(self.clock()), "network_called": False}

    def control_read_metrics(self, proposal_id: str, *,
                             principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Pure metric read: no snapshots, audits, or proposal state changes."""
        return self._control_metric_result(proposal_id, principal=principal)

    def control_evaluate_stop(self, proposal_id: str, *, actor_id: str = "system",
                              principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Persist a snapshot and atomically apply automatic stop conditions."""
        scope = self._authorize(principal, "exploration.manage")
        result = self._control_metric_result(proposal_id, principal=principal)
        snapshot_id = f"TCM-{uuid.uuid4().hex.upper()}"
        now = _iso(self.clock())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO traffic_change_metric_snapshots VALUES(?,?,?,?,?,?,?)""",
              (snapshot_id, proposal_id, now, _canonical(result["metrics"]),
               json.dumps(result["triggers"], sort_keys=True, separators=(",", ":"))
               if result["triggers"] else None,
               *scope.sql_parameters()))
            row = db.execute("""SELECT state,minimum_sample_count FROM
              traffic_change_control_proposals WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
              (proposal_id, *scope.sql_parameters())).fetchone()
            if not row:
                raise TrafficChangeGovernanceError("proposal_not_found")
            if (result["triggers"] and row["state"] == "CANARY_RUNNING" and
                    result["metrics"]["request_count"] >= row["minimum_sample_count"]):
                db.execute("""UPDATE traffic_change_control_proposals SET state='AUTO_STOPPED',
                  last_trigger_json=?,updated_at=?,revision=revision+1
                  WHERE proposal_id=? AND tenant_id=? AND workspace_id=?""",
                  (json.dumps(result["triggers"], sort_keys=True, separators=(",", ":")), now,
                   proposal_id, *scope.sql_parameters()))
                self._audit(db, proposal_id, "control_auto_stopped", actor_id,
                            {"triggers": result["triggers"], "snapshot_id": snapshot_id}, scope)
                result["proposal_state"] = "AUTO_STOPPED"
            db.commit()
        return {**result, "snapshot_id": snapshot_id, "collected_at": now}

    def control_metrics(self, proposal_id: str, *, evaluate_stop: bool = True,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Backward-compatible facade; callers should choose read or evaluate explicitly."""
        if evaluate_stop:
            return self.control_evaluate_stop(proposal_id, principal=principal)
        return self.control_read_metrics(proposal_id, principal=principal)

    def control_audit(self, proposal_id: str, *,
                      principal: PrincipalContext | None = None) -> dict[str, Any]:
        items = self.list_audit(proposal_id, principal=principal)
        return {"status": "ready", "proposal_id": proposal_id, "items": items,
                "network_called": False}
