"""Offline approval and reservation governance for high-cost tests."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope


class HighCostTestGovernanceError(ValueError): pass
def _now() -> datetime: return datetime.now(timezone.utc)
def _iso(value: datetime) -> str:
    if value.tzinfo is None: raise HighCostTestGovernanceError("timezone_required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
def _parse(value: str) -> datetime: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
def _json(value: Mapping[str, Any]) -> str: return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def load_high_cost_test_policy(path: str | Path) -> dict[str, Any]:
    try: policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise HighCostTestGovernanceError("invalid_high_cost_policy") from exc
    required = {"policy_version", "enabled", "real_execution_allowed", "maximum_approval_ttl_seconds",
                "known_currencies", "limits", "roles"}
    if (not isinstance(policy, dict) or not required.issubset(policy)
            or policy["real_execution_allowed"] is not False
            or set(policy["limits"]) != {"global", "environment", "model"}
            or set(policy["roles"]) != {"proposer", "approver", "operator", "emergency"}):
        raise HighCostTestGovernanceError("invalid_or_unsafe_high_cost_policy")
    currencies = set(policy["known_currencies"])
    if not currencies or any(set(scope) != currencies or any(float(v) < 0 for v in scope.values()) for scope in policy["limits"].values()):
        raise HighCostTestGovernanceError("invalid_high_cost_limits")
    return policy


class HighCostTestGovernanceService:
    TERMINAL = frozenset({"RECONCILED", "CANCELLED", "EXPIRED", "EMERGENCY_STOPPED"})
    def __init__(self, database_path: str | Path, policy_path: str | Path, *,
                 clock: Callable[[], datetime] = _now,
                 authorization_service: AuthorizationService | None = None):
        self.path = Path(database_path); self.policy = load_high_cost_test_policy(policy_path); self.clock = clock
        self.authorization_service = authorization_service
        self.path.parent.mkdir(parents=True, exist_ok=True); self._init(); self.recover()

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
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None); db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000"); db.execute("PRAGMA journal_mode=WAL"); return db

    def _init(self) -> None:
        with self.connect() as db: db.executescript("""
        CREATE TABLE IF NOT EXISTS high_cost_tests(
          test_id TEXT NOT NULL,environment_id TEXT NOT NULL,model_id TEXT NOT NULL,currency TEXT NOT NULL,
          estimated_cost REAL NOT NULL,actual_cost REAL,proposer_id TEXT NOT NULL,state TEXT NOT NULL,
          created_at TEXT NOT NULL,expires_at TEXT NOT NULL,reserved_at TEXT,started_at TEXT,reconciled_at TEXT,
          terminated_at TEXT,termination_reason TEXT,revision INTEGER NOT NULL,policy_version TEXT NOT NULL,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,test_id),
          CHECK(state IN ('PROPOSED','APPROVED','RESERVED','RUNNING','RECONCILED','CANCELLED','EXPIRED','EMERGENCY_STOPPED')));
        CREATE TABLE IF NOT EXISTS high_cost_approvals(
          approval_id TEXT NOT NULL,test_id TEXT NOT NULL,approver_id TEXT NOT NULL,approved_at TEXT NOT NULL,
          approved_revision INTEGER NOT NULL,approved_amount REAL NOT NULL,currency TEXT NOT NULL,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,approval_id),
          FOREIGN KEY(tenant_id,workspace_id,test_id)
            REFERENCES high_cost_tests(tenant_id,workspace_id,test_id));
        CREATE TABLE IF NOT EXISTS high_cost_kill_switches(
          scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,active INTEGER NOT NULL,reason TEXT NOT NULL,
          updated_at TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,scope_type,scope_id));
        CREATE TABLE IF NOT EXISTS high_cost_idempotency(
          operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_sha256 TEXT NOT NULL,
          response_json TEXT NOT NULL,created_at TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,operation,idempotency_key));
        CREATE TABLE IF NOT EXISTS high_cost_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,test_id TEXT,event_type TEXT NOT NULL,actor_id TEXT NOT NULL,
          created_at TEXT NOT NULL,details_json TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
        """)
        with self.connect() as db:
            for table in ("high_cost_tests", "high_cost_approvals", "high_cost_kill_switches",
                          "high_cost_idempotency", "high_cost_audit"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'")
                if "workspace_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'")
                db.execute(f"CREATE INDEX IF NOT EXISTS ix_scope_{table} ON {table}(tenant_id,workspace_id)")

    def _role(self, actor: str, role: str, required: str) -> None:
        if not actor or role not in self.policy["roles"][required]: raise HighCostTestGovernanceError(f"{required}_role_required")
    def _audit(self, db: sqlite3.Connection, test_id: str | None, event: str, actor: str, details: Mapping[str, Any], scope: TenantScope | None = None) -> None:
        scope = scope or TenantScope.local_development()
        db.execute("INSERT INTO high_cost_audit(test_id,event_type,actor_id,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)",
                   (test_id, event, actor, _iso(self.clock()), _json(details), *scope.sql_parameters()))
    def _idem(self, db: sqlite3.Connection, operation: str, key: str, payload: Mapping[str, Any], result: Mapping[str, Any] | None = None, scope: TenantScope | None = None) -> dict[str, Any] | None:
        scope = scope or TenantScope.local_development()
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        row = db.execute("SELECT * FROM high_cost_idempotency WHERE operation=? AND idempotency_key=? AND tenant_id=? AND workspace_id=?", (operation, key, *scope.sql_parameters())).fetchone()
        if row:
            if row["request_sha256"] != digest: raise HighCostTestGovernanceError("idempotency_key_payload_mismatch")
            return json.loads(row["response_json"])
        if result is not None:
            db.execute("INSERT INTO high_cost_idempotency(operation,idempotency_key,request_sha256,response_json,created_at,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)", (operation, key, digest, _json(result), _iso(self.clock()), *scope.sql_parameters()))
        return None

    def propose(self, *, environment_id: str, model_id: str, currency: str, estimated_cost: float,
                proposer_id: str, actor_role: str, ttl_seconds: int, idempotency_key: str,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        self._role(proposer_id, actor_role, "proposer")
        if currency not in self.policy["known_currencies"]: raise HighCostTestGovernanceError("unknown_currency")
        if (not environment_id or not model_id or estimated_cost <= 0 or not idempotency_key
                or not 0 < ttl_seconds <= self.policy["maximum_approval_ttl_seconds"]): raise HighCostTestGovernanceError("invalid_high_cost_proposal")
        payload = {"environment_id": environment_id, "model_id": model_id, "currency": currency,
                   "estimated_cost": estimated_cost, "proposer_id": proposer_id, "ttl_seconds": ttl_seconds}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); prior = self._idem(db, "propose", idempotency_key, payload, scope=scope)
            if prior: db.commit(); return prior
            now = self.clock(); test_id = f"HCT-{uuid.uuid4().hex}"; expires = _iso(now + timedelta(seconds=ttl_seconds))
            result = {"test_id": test_id, "state": "PROPOSED", "revision": 0, "currency": currency,
                      "estimated_cost": float(estimated_cost), "expires_at": expires,
                      "mock_only": True, "real_execution_allowed": False}
            db.execute("""INSERT INTO high_cost_tests(
              test_id,environment_id,model_id,currency,estimated_cost,actual_cost,proposer_id,state,
              created_at,expires_at,reserved_at,started_at,reconciled_at,terminated_at,
              termination_reason,revision,policy_version,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                test_id, environment_id, model_id, currency, estimated_cost, None, proposer_id, "PROPOSED",
                _iso(now), expires, None, None, None, None, None, 0, self.policy["policy_version"], *scope.sql_parameters()))
            self._audit(db, test_id, "proposed", proposer_id, result, scope); self._idem(db, "propose", idempotency_key, payload, result, scope); db.commit(); return result

    def approve(self, test_id: str, *, approver_id: str, actor_role: str, expected_revision: int,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        self._role(approver_id, actor_role, "approver")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT * FROM high_cost_tests WHERE test_id=? AND tenant_id=? AND workspace_id=?", (test_id, *scope.sql_parameters())).fetchone()
            if not row: raise HighCostTestGovernanceError("high_cost_test_not_found")
            if row["proposer_id"] == approver_id: raise HighCostTestGovernanceError("proposer_approver_separation_required")
            if row["state"] != "PROPOSED": raise HighCostTestGovernanceError("high_cost_test_not_approvable")
            if row["revision"] != expected_revision: raise HighCostTestGovernanceError("revision_conflict")
            if _parse(row["expires_at"]) <= self.clock(): raise HighCostTestGovernanceError("approval_expired")
            approval_id = f"HCA-{uuid.uuid4().hex}"; revision = expected_revision + 1
            db.execute("""UPDATE high_cost_tests SET state='APPROVED',revision=?
                       WHERE test_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (revision, test_id, expected_revision, *scope.sql_parameters()))
            db.execute("""INSERT INTO high_cost_approvals(
              approval_id,test_id,approver_id,approved_at,approved_revision,approved_amount,
              currency,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)""", (approval_id, test_id, approver_id,
                _iso(self.clock()), expected_revision, row["estimated_cost"], row["currency"], *scope.sql_parameters()))
            result = {"test_id": test_id, "approval_id": approval_id, "state": "APPROVED", "revision": revision,
                      "real_execution_allowed": False}; self._audit(db, test_id, "approved", approver_id, result, scope); db.commit(); return result

    def _killed(self, db: sqlite3.Connection, row: sqlite3.Row, scope: TenantScope) -> bool:
        scopes = {("global", "*"), ("environment", row["environment_id"]), ("model", row["model_id"])}
        return any((x["scope_type"], x["scope_id"]) in scopes for x in db.execute(
            """SELECT scope_type,scope_id FROM high_cost_kill_switches
               WHERE active=1 AND tenant_id=? AND workspace_id=?""", scope.sql_parameters()))

    def reserve(self, test_id: str, *, operator_id: str, actor_role: str, expected_revision: int,
                idempotency_key: str, gate_results: Mapping[str, bool],
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "scheduler.execute")
        self._role(operator_id, actor_role, "operator")
        payload = {"test_id": test_id, "operator_id": operator_id, "expected_revision": expected_revision,
                   "gate_results": dict(gate_results)}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); prior = self._idem(db, "reserve", idempotency_key, payload, scope=scope)
            if prior: db.commit(); return prior
            row = db.execute("SELECT * FROM high_cost_tests WHERE test_id=? AND tenant_id=? AND workspace_id=?",
                             (test_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] != "APPROVED": raise HighCostTestGovernanceError("high_cost_test_not_reservable")
            if row["revision"] != expected_revision: raise HighCostTestGovernanceError("revision_conflict")
            if _parse(row["expires_at"]) <= self.clock(): raise HighCostTestGovernanceError("approval_expired")
            if not gate_results or not all(gate_results.values()): raise HighCostTestGovernanceError("high_cost_gate_failed")
            if self._killed(db, row, scope): raise HighCostTestGovernanceError("kill_switch_active")
            currency = row["currency"]
            for scope_type, column, value in (("global", None, None), ("environment", "environment_id", row["environment_id"]), ("model", "model_id", row["model_id"])):
                where = "currency=? AND state IN ('RESERVED','RUNNING','RECONCILED') AND test_id<>?"
                args: list[Any] = [currency, test_id]
                if column: where += f" AND {column}=?"; args.append(value)
                where += " AND tenant_id=? AND workspace_id=?"; args.extend(scope.sql_parameters())
                used = db.execute(f"SELECT COALESCE(SUM(COALESCE(actual_cost,estimated_cost)),0) FROM high_cost_tests WHERE {where}", args).fetchone()[0]
                if used + row["estimated_cost"] > self.policy["limits"][scope_type][currency] + 1e-12:
                    raise HighCostTestGovernanceError(f"{scope_type}_budget_exhausted")
            revision = expected_revision + 1
            db.execute("""UPDATE high_cost_tests SET state='RESERVED',reserved_at=?,revision=?
                       WHERE test_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (_iso(self.clock()), revision, test_id, expected_revision, *scope.sql_parameters()))
            result = {"test_id": test_id, "state": "RESERVED", "revision": revision,
                      "reserved_cost": row["estimated_cost"], "currency": currency,
                      "mock_only": True, "real_execution_allowed": False}
            self._audit(db, test_id, "budget_reserved", operator_id, result, scope); self._idem(db, "reserve", idempotency_key, payload, result, scope); db.commit(); return result

    def start_mock(self, test_id: str, *, operator_id: str, actor_role: str, expected_revision: int,
                   principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "scheduler.execute")
        self._role(operator_id, actor_role, "operator")
        if not self.policy["enabled"]: raise HighCostTestGovernanceError("high_cost_testing_disabled")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT * FROM high_cost_tests WHERE test_id=? AND tenant_id=? AND workspace_id=?", (test_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] != "RESERVED": raise HighCostTestGovernanceError("high_cost_test_not_startable")
            if row["revision"] != expected_revision: raise HighCostTestGovernanceError("revision_conflict")
            if _parse(row["expires_at"]) <= self.clock(): raise HighCostTestGovernanceError("approval_expired")
            if self._killed(db, row, scope): raise HighCostTestGovernanceError("kill_switch_active")
            db.execute("""UPDATE high_cost_tests SET state='RUNNING',started_at=?,revision=revision+1
                       WHERE test_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (_iso(self.clock()), test_id, expected_revision, *scope.sql_parameters()))
            result = {"test_id": test_id, "state": "RUNNING", "revision": expected_revision + 1,
                      "transport": "mock", "real_execution_allowed": False}
            self._audit(db, test_id, "mock_started", operator_id, result, scope); db.commit(); return result

    def reconcile(self, test_id: str, *, operator_id: str, actor_role: str,
                  expected_revision: int, actual_cost: float, currency: str,
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "scheduler.execute")
        self._role(operator_id, actor_role, "operator")
        if currency not in self.policy["known_currencies"]: raise HighCostTestGovernanceError("unknown_currency")
        if actual_cost < 0: raise HighCostTestGovernanceError("invalid_actual_cost")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT * FROM high_cost_tests WHERE test_id=? AND tenant_id=? AND workspace_id=?", (test_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] not in {"RESERVED", "RUNNING"}: raise HighCostTestGovernanceError("high_cost_test_not_reconcilable")
            if row["revision"] != expected_revision: raise HighCostTestGovernanceError("revision_conflict")
            if currency != row["currency"]: raise HighCostTestGovernanceError("currency_mismatch")
            db.execute("""UPDATE high_cost_tests SET state='RECONCILED',actual_cost=?,reconciled_at=?,revision=revision+1
                       WHERE test_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (actual_cost, _iso(self.clock()), test_id, expected_revision, *scope.sql_parameters()))
            result = {"test_id": test_id, "state": "RECONCILED", "revision": expected_revision + 1,
                      "reserved_cost": row["estimated_cost"], "actual_cost": actual_cost, "currency": currency,
                      "variance": actual_cost - row["estimated_cost"], "real_execution_allowed": False}
            self._audit(db, test_id, "reconciled", operator_id, result, scope); db.commit(); return result

    def terminate(self, test_id: str, *, actor_id: str, actor_role: str,
                  expected_revision: int, reason: str, emergency: bool = False,
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "kill_switch.activate" if emergency else "exploration.manage")
        self._role(actor_id, actor_role, "emergency" if emergency else "operator")
        target = "EMERGENCY_STOPPED" if emergency else "CANCELLED"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); row = db.execute("SELECT * FROM high_cost_tests WHERE test_id=? AND tenant_id=? AND workspace_id=?", (test_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] in self.TERMINAL: raise HighCostTestGovernanceError("high_cost_test_not_terminable")
            if row["revision"] != expected_revision: raise HighCostTestGovernanceError("revision_conflict")
            db.execute("""UPDATE high_cost_tests SET state=?,terminated_at=?,termination_reason=?,revision=revision+1
                       WHERE test_id=? AND revision=? AND tenant_id=? AND workspace_id=?""",
                       (target, _iso(self.clock()), reason, test_id, expected_revision, *scope.sql_parameters()))
            result = {"test_id": test_id, "state": target, "revision": expected_revision + 1,
                      "reservation_released": True, "real_execution_allowed": False}
            self._audit(db, test_id, "emergency_stop" if emergency else "cancelled", actor_id, result, scope); db.commit(); return result

    def set_kill_switch(self, *, scope_type: str, scope_id: str, active: bool, reason: str,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "kill_switch.activate" if active else "kill_switch.release")
        if scope_type not in {"global", "environment", "model"} or not reason: raise HighCostTestGovernanceError("invalid_kill_switch")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); db.execute("""INSERT INTO high_cost_kill_switches(
              scope_type,scope_id,active,reason,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,workspace_id,scope_type,scope_id)
              DO UPDATE SET active=excluded.active,reason=excluded.reason,updated_at=excluded.updated_at""",
              (scope_type, scope_id, int(active), reason, _iso(self.clock()), *scope.sql_parameters()))
            if active:
                clause = {"global": "1=1", "environment": "environment_id=?", "model": "model_id=?"}[scope_type]
                args = () if scope_type == "global" else (scope_id,)
                rows = db.execute(f"SELECT test_id FROM high_cost_tests WHERE state IN ('RESERVED','RUNNING') AND {clause} AND tenant_id=? AND workspace_id=?",
                                  (*args, *scope.sql_parameters())).fetchall()
                for row in rows:
                    db.execute("""UPDATE high_cost_tests SET state='EMERGENCY_STOPPED',terminated_at=?,
                               termination_reason='kill_switch',revision=revision+1
                               WHERE test_id=? AND tenant_id=? AND workspace_id=?""",
                               (_iso(self.clock()), row["test_id"], *scope.sql_parameters()))
                    self._audit(db, row["test_id"], "kill_switch_stop", "system", {"scope_type": scope_type, "scope_id": scope_id}, scope)
            result = {"scope_type": scope_type, "scope_id": scope_id, "active": bool(active)}; self._audit(db, None, "kill_switch_changed", "system", result, scope); db.commit(); return result

    def recover(self) -> dict[str, int]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE"); now = _iso(self.clock()); count = 0
            scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id FROM high_cost_tests
                                 WHERE state IN ('PROPOSED','APPROVED','RESERVED','RUNNING')
                                 AND expires_at<=?""", (now,)).fetchall()
            for item in scopes:
                scope = TenantScope(item["tenant_id"], item["workspace_id"])
                cursor = db.execute("""UPDATE high_cost_tests SET state='EXPIRED',
                  terminated_at=?,termination_reason='approval_expired',revision=revision+1
                  WHERE state IN ('PROPOSED','APPROVED','RESERVED','RUNNING') AND expires_at<=?
                  AND tenant_id=? AND workspace_id=?""",
                  (now, now, *scope.sql_parameters()))
                count += cursor.rowcount
                if cursor.rowcount:
                    self._audit(db, None, "restart_recovery", "system",
                                {"expired": cursor.rowcount}, scope)
            db.commit(); return {"expired": count}

    def get(self, test_id: str, *,
            principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        with self.connect() as db:
            row = db.execute("SELECT * FROM high_cost_tests WHERE test_id=? AND tenant_id=? AND workspace_id=?",
                             (test_id, *scope.sql_parameters())).fetchone()
            approvals = db.execute("SELECT * FROM high_cost_approvals WHERE test_id=? AND tenant_id=? AND workspace_id=? ORDER BY approved_at",
                                   (test_id, *scope.sql_parameters())).fetchall()
        if not row: raise HighCostTestGovernanceError("high_cost_test_not_found")
        return {**dict(row), "approvals": [dict(x) for x in approvals], "real_execution_allowed": False, "network_called": False}

    def list_audit(self, test_id: str | None = None, *,
                   principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._authorize(principal, "audit.read")
        query = "SELECT * FROM high_cost_audit WHERE tenant_id=? AND workspace_id=?"
        args: tuple[Any, ...] = scope.sql_parameters()
        if test_id is not None: query += " AND test_id=?"; args = (*args, test_id)
        query += " ORDER BY audit_id"
        with self.connect() as db: rows = db.execute(query, args).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def monitoring_status(self, limit: int = 100, *,
                          principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        if not 1 <= limit <= 500: raise HighCostTestGovernanceError("invalid_status_limit")
        with self.connect() as db:
            counts = {row["state"]: row["count"] for row in db.execute(
                """SELECT state,COUNT(*) count FROM high_cost_tests
                   WHERE tenant_id=? AND workspace_id=? GROUP BY state""", scope.sql_parameters())}
            rows = db.execute("""SELECT test_id,environment_id,model_id,currency,estimated_cost,
              actual_cost,state,created_at,expires_at,reserved_at,started_at,reconciled_at,
              terminated_at,revision,policy_version FROM high_cost_tests
              WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC,test_id LIMIT ?""", (*scope.sql_parameters(), limit)).fetchall()
            switches = db.execute("""SELECT scope_type,scope_id,active,updated_at FROM high_cost_kill_switches
                                  WHERE tenant_id=? AND workspace_id=? ORDER BY scope_type,scope_id""",
                                  scope.sql_parameters()).fetchall()
            reserved = {row["currency"]: float(row["amount"]) for row in db.execute("""SELECT currency,
              COALESCE(SUM(estimated_cost),0) amount FROM high_cost_tests
              WHERE state IN ('RESERVED','RUNNING') AND tenant_id=? AND workspace_id=? GROUP BY currency""",
              scope.sql_parameters())}
        return {"status": "disabled" if not self.policy["enabled"] else "mock_only",
                "capability_id": "ADV-019", "enabled": bool(self.policy["enabled"]),
                "mode": "mock_only", "real_execution_allowed": False,
                "policy_version": self.policy["policy_version"], "state_counts": counts,
                "known_currencies": list(self.policy["known_currencies"]),
                "limits": self.policy["limits"], "reserved_by_currency": reserved,
                "items": [dict(row) for row in rows], "kill_switches": [dict(row) for row in switches],
                "authorization_status": "identity_provider_and_separate_authorization_required",
                "network_called": False}

    def monitoring_audit(self, limit: int = 100, *,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "audit.read")
        if not 1 <= limit <= 500: raise HighCostTestGovernanceError("invalid_audit_limit")
        with self.connect() as db:
            rows = db.execute("""SELECT audit_id,test_id,event_type,created_at FROM high_cost_audit
                              WHERE tenant_id=? AND workspace_id=? ORDER BY audit_id DESC LIMIT ?""",
                              (*scope.sql_parameters(), limit)).fetchall()
        return {"status": "ready", "capability_id": "ADV-019", "items": [dict(row) for row in rows], "network_called": False}
