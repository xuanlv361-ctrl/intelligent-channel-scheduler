"""Persistent, disabled-by-default governance for offline/Mock probes."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope


class ProbeGovernanceError(ValueError): pass


def _now() -> datetime: return datetime.now(timezone.utc)
def _iso(value: datetime) -> str:
    if value.tzinfo is None: raise ProbeGovernanceError("timezone_required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
def _parse(value: str) -> datetime: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_probe_policy(path: str | Path) -> dict[str, Any]:
    try: policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ProbeGovernanceError("invalid_probe_policy") from exc
    needed = {"policy_version", "enabled", "live_worker_authorized", "maximum_approval_ttl_seconds",
              "lease_ttl_seconds", "limits", "backoff", "stop"}
    if not isinstance(policy, dict) or not needed.issubset(policy) or policy["live_worker_authorized"] is not False:
        raise ProbeGovernanceError("invalid_or_unsafe_probe_policy")
    if set(policy["limits"]) != {"global", "environment", "channel", "model"}:
        raise ProbeGovernanceError("invalid_probe_limits")
    for item in policy["limits"].values():
        if any(float(item.get(key, -1)) < 0 for key in ("requests", "cost", "rate_per_minute", "burst", "concurrency")):
            raise ProbeGovernanceError("invalid_probe_limits")
    return policy


class ProbeGovernanceService:
    def __init__(self, database_path: str | Path, policy_path: str | Path, *,
                 clock: Callable[[], datetime] = _now,
                 authorization_service: AuthorizationService | None = None):
        self.path = Path(database_path); self.policy = load_probe_policy(policy_path); self.clock = clock
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
        CREATE TABLE IF NOT EXISTS probe_approvals(
          approval_id TEXT NOT NULL,environment_id TEXT NOT NULL,channel_id TEXT NOT NULL,
          model_id TEXT NOT NULL,approved_by TEXT NOT NULL,created_at TEXT NOT NULL,
          starts_at TEXT NOT NULL,expires_at TEXT NOT NULL,max_requests INTEGER NOT NULL,max_cost REAL NOT NULL,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,approval_id));
        CREATE TABLE IF NOT EXISTS probe_leases(
          lease_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_sha256 TEXT NOT NULL,
          run_id TEXT NOT NULL,environment_id TEXT NOT NULL,channel_id TEXT NOT NULL,model_id TEXT NOT NULL,
          approval_id TEXT NOT NULL,cost REAL NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,completed_at TEXT,result TEXT,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,lease_id),
          UNIQUE(tenant_id,workspace_id,idempotency_key),
          CHECK(state IN ('ACTIVE','SUCCEEDED','FAILED','EXPIRED','STOPPED')));
        CREATE TABLE IF NOT EXISTS probe_scope_state(
          scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,consecutive_failures INTEGER NOT NULL DEFAULT 0,
          sample_count INTEGER NOT NULL DEFAULT 0,failure_count INTEGER NOT NULL DEFAULT 0,
          stopped INTEGER NOT NULL DEFAULT 0,stop_reason TEXT,backoff_until TEXT,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,scope_type,scope_id));
        CREATE TABLE IF NOT EXISTS probe_kill_switches(
          scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,active INTEGER NOT NULL,reason TEXT NOT NULL,
          updated_at TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
          workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,scope_type,scope_id));
        CREATE TABLE IF NOT EXISTS probe_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT NOT NULL,created_at TEXT NOT NULL,
          details_json TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
          workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
        CREATE TABLE IF NOT EXISTS probe_tasks(
          task_id TEXT NOT NULL,name TEXT NOT NULL,environment_id TEXT NOT NULL,
          channel_id TEXT NOT NULL,model_id TEXT NOT NULL,request_template TEXT NOT NULL,
          frequency_seconds INTEGER NOT NULL,max_requests INTEGER NOT NULL,
          used_requests INTEGER NOT NULL DEFAULT 0,max_concurrency INTEGER NOT NULL,
          deadline TEXT NOT NULL,stop_condition TEXT NOT NULL,state TEXT NOT NULL,
          last_result TEXT,last_run_at TEXT,next_run_at TEXT,created_at TEXT NOT NULL,
          started_at TEXT,stopped_at TEXT,
          tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
          workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
          PRIMARY KEY(tenant_id,workspace_id,task_id),
          CHECK(state IN ('DRAFT','RUNNING','STOPPED','SUCCEEDED','FAILED','EXPIRED')));
        """)
        with self.connect() as db:
            for table in ("probe_approvals", "probe_leases", "probe_scope_state",
                          "probe_kill_switches", "probe_audit", "probe_tasks"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'")
                if "workspace_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'")
                db.execute(f"CREATE INDEX IF NOT EXISTS ix_scope_{table} ON {table}(tenant_id,workspace_id)")

    def create_task(self, *, name: str, environment_id: str, channel_id: str,
                    model_id: str, request_template: str, frequency_seconds: int,
                    max_requests: int, max_concurrency: int, deadline: datetime,
                    stop_condition: str,
                    principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Persist a bounded local/Mock probe task without contacting a provider."""
        scope = self._authorize(principal, "exploration.manage")
        now = self.clock()
        channel_limit = self.policy["limits"]["channel"]
        minimum_frequency = max(1, math.ceil(60 / max(1, channel_limit["rate_per_minute"])))
        if (not all((name.strip(), environment_id.strip(), channel_id.strip(), model_id.strip()))
                or request_template not in {"mock_success", "mock_failure"}
                or stop_condition not in {"request_limit", "first_failure"}
                or frequency_seconds < minimum_frequency
                or max_requests <= 0 or max_requests > int(channel_limit["requests"])
                or max_concurrency <= 0 or max_concurrency > int(channel_limit["concurrency"])
                or deadline.tzinfo is None or deadline <= now
                or deadline > now + timedelta(seconds=self.policy["maximum_approval_ttl_seconds"])):
            raise ProbeGovernanceError("probe_task_outside_local_safety_limits")
        task_id = f"PT-{uuid.uuid4().hex}"
        with self.connect() as db:
            db.execute("""INSERT INTO probe_tasks(
              task_id,name,environment_id,channel_id,model_id,request_template,
              frequency_seconds,max_requests,max_concurrency,deadline,stop_condition,
              state,created_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                task_id, name.strip(), environment_id.strip(), channel_id.strip(),
                model_id.strip(), request_template, frequency_seconds, max_requests,
                max_concurrency, _iso(deadline), stop_condition, "DRAFT", _iso(now),
                *scope.sql_parameters()))
            self._audit(db, "probe_task_created", {"task_id": task_id,
                "transport": "mock", "network_called": False}, scope)
        return self.get_task(task_id, principal=principal)

    def get_task(self, task_id: str, *,
                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        with self.connect() as db:
            row = db.execute("""SELECT task_id,name,environment_id,channel_id,model_id,
              request_template,frequency_seconds,max_requests,used_requests,max_concurrency,
              deadline,stop_condition,state,last_result,last_run_at,next_run_at,created_at,
              started_at,stopped_at FROM probe_tasks WHERE task_id=? AND tenant_id=?
              AND workspace_id=?""", (task_id, *scope.sql_parameters())).fetchone()
        if row is None:
            raise ProbeGovernanceError("probe_task_not_found")
        return {**dict(row), "transport": "mock", "network_called": False}

    def list_tasks(self, limit: int = 100, *,
                   principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        if not 1 <= limit <= 500:
            raise ProbeGovernanceError("invalid_status_limit")
        with self.connect() as db:
            rows = db.execute("""SELECT task_id,name,environment_id,channel_id,model_id,
              request_template,frequency_seconds,max_requests,used_requests,max_concurrency,
              deadline,stop_condition,state,last_result,last_run_at,next_run_at,created_at,
              started_at,stopped_at FROM probe_tasks WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC,task_id LIMIT ?""",
              (*scope.sql_parameters(), limit)).fetchall()
        return {"status": "ready", "mode": "mock_only", "items": [dict(row) for row in rows],
                "total": len(rows), "network_called": False}

    def start_task(self, task_id: str, *,
                   principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM probe_tasks WHERE task_id=? AND tenant_id=? AND workspace_id=?",
                             (task_id, *scope.sql_parameters())).fetchone()
            if row is None:
                raise ProbeGovernanceError("probe_task_not_found")
            if row["state"] not in {"DRAFT", "STOPPED"}:
                raise ProbeGovernanceError("probe_task_not_startable")
            if _parse(row["deadline"]) <= now:
                db.execute("UPDATE probe_tasks SET state='EXPIRED',stopped_at=? WHERE task_id=? AND tenant_id=? AND workspace_id=?",
                           (_iso(now), task_id, *scope.sql_parameters()))
                db.commit(); raise ProbeGovernanceError("probe_task_expired")
            success = row["request_template"] == "mock_success"
            used = int(row["used_requests"]) + 1
            terminal = used >= int(row["max_requests"])
            state = ("SUCCEEDED" if success else "FAILED") if terminal or (not success and row["stop_condition"] == "first_failure") else "RUNNING"
            next_run = None if state != "RUNNING" else _iso(now + timedelta(seconds=row["frequency_seconds"]))
            stopped_at = _iso(now) if state in {"SUCCEEDED", "FAILED"} else None
            db.execute("""UPDATE probe_tasks SET state=?,used_requests=?,last_result=?,
              last_run_at=?,next_run_at=?,started_at=COALESCE(started_at,?),stopped_at=?
              WHERE task_id=? AND tenant_id=? AND workspace_id=?""", (
                state, used, "mock_success" if success else "mock_failure", _iso(now),
                next_run, _iso(now), stopped_at, task_id, *scope.sql_parameters()))
            self._audit(db, "probe_task_started", {"task_id": task_id, "state": state,
                "result": "mock_success" if success else "mock_failure",
                "network_called": False}, scope)
            db.commit()
        return self.get_task(task_id, principal=principal)

    def stop_task(self, task_id: str, *,
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        now = _iso(self.clock())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM probe_tasks WHERE task_id=? AND tenant_id=? AND workspace_id=?",
                             (task_id, *scope.sql_parameters())).fetchone()
            if row is None:
                raise ProbeGovernanceError("probe_task_not_found")
            if row["state"] in {"SUCCEEDED", "FAILED", "EXPIRED"}:
                raise ProbeGovernanceError("probe_task_terminal")
            db.execute("UPDATE probe_tasks SET state='STOPPED',next_run_at=NULL,stopped_at=? WHERE task_id=? AND tenant_id=? AND workspace_id=?",
                       (now, task_id, *scope.sql_parameters()))
            self._audit(db, "probe_task_stopped", {"task_id": task_id,
                "network_called": False}, scope); db.commit()
        return self.get_task(task_id, principal=principal)

    def _audit(self, db: sqlite3.Connection, event: str, details: Mapping[str, Any],
               scope: TenantScope | None = None) -> None:
        scope = scope or TenantScope.local_development()
        db.execute("INSERT INTO probe_audit(event_type,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?)",
                   (event, _iso(self.clock()), json.dumps(dict(details), sort_keys=True),
                    *scope.sql_parameters()))

    def approve(self, *, environment_id: str, channel_id: str, model_id: str, approved_by: str,
                starts_at: datetime, ttl_seconds: int, max_requests: int, max_cost: float,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.manage")
        if (not all((environment_id, channel_id, model_id, approved_by)) or starts_at.tzinfo is None
                or not 0 < ttl_seconds <= self.policy["maximum_approval_ttl_seconds"]
                or max_requests <= 0 or max_cost < 0): raise ProbeGovernanceError("invalid_probe_approval")
        identifier = f"PA-{uuid.uuid4().hex}"; result = {"approval_id": identifier,
            "environment_id": environment_id, "channel_id": channel_id, "model_id": model_id,
            "approved_by": approved_by, "created_at": _iso(self.clock()), "starts_at": _iso(starts_at),
            "expires_at": _iso(starts_at + timedelta(seconds=ttl_seconds)),
            "max_requests": max_requests, "max_cost": float(max_cost)}
        with self.connect() as db:
            db.execute("""INSERT INTO probe_approvals(
              approval_id,environment_id,channel_id,model_id,approved_by,created_at,
              starts_at,expires_at,max_requests,max_cost,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              (*tuple(result.values()), *scope.sql_parameters()))
            self._audit(db, "approval_created", {"approval_id": identifier}, scope)
        return result

    @staticmethod
    def _scopes(environment_id: str, channel_id: str, model_id: str) -> tuple[tuple[str, str], ...]:
        return (("global", "*"), ("environment", environment_id), ("channel", channel_id), ("model", model_id))

    def deterministic_backoff_seconds(self, scope_id: str, attempt: int) -> float:
        cfg = self.policy["backoff"]; base = min(cfg["maximum_seconds"], cfg["base_seconds"] * (2 ** max(0, attempt - 1)))
        fraction = int.from_bytes(hashlib.sha256(f"{scope_id}:{attempt}".encode()).digest()[:8], "big") / 2**64
        return base * (1 - cfg["jitter_fraction"] + 2 * cfg["jitter_fraction"] * fraction)

    def _blocked(self, db: sqlite3.Connection, scopes: tuple[tuple[str, str], ...], circuit_state: str,
                 tenant_scope: TenantScope) -> str | None:
        if not self.policy["enabled"]: return "probe_policy_disabled"
        if circuit_state != "CLOSED": return "circuit_not_closed"
        for scope_type, scope_id in scopes:
            kill = db.execute("SELECT active FROM probe_kill_switches WHERE scope_type=? AND scope_id=? AND tenant_id=? AND workspace_id=?",
                              (scope_type, scope_id, *tenant_scope.sql_parameters())).fetchone()
            if kill and kill[0]: return "kill_switch_active"
            state = db.execute("SELECT * FROM probe_scope_state WHERE scope_type=? AND scope_id=? AND tenant_id=? AND workspace_id=?",
                               (scope_type, scope_id, *tenant_scope.sql_parameters())).fetchone()
            if state and state["stopped"]: return "stop_condition_active"
            if state and state["backoff_until"] and _parse(state["backoff_until"]) > self.clock(): return "backoff_active"
        return None

    def acquire(self, *, idempotency_key: str, run_id: str, environment_id: str,
                channel_id: str, model_id: str, estimated_cost: float,
                circuit_state: str = "CLOSED", transport: str = "mock",
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "scheduler.execute")
        if self.authorization_service is not None:
            idempotency_key = scope.idempotency_key(
                operation="probe_acquire", external_key=idempotency_key, version="v1")
        if transport != "mock": raise ProbeGovernanceError("live_probe_worker_not_authorized")
        if not idempotency_key or estimated_cost < 0: raise ProbeGovernanceError("invalid_probe_request")
        payload = {"run_id": run_id, "environment_id": environment_id, "channel_id": channel_id,
                   "model_id": model_id, "estimated_cost": estimated_cost, "circuit_state": circuit_state}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        scopes = self._scopes(environment_id, channel_id, model_id); now = self.clock(); cutoff = _iso(now - timedelta(seconds=60))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM probe_leases WHERE idempotency_key=? AND tenant_id=? AND workspace_id=?",
                               (idempotency_key, *scope.sql_parameters())).fetchone()
            if prior:
                if prior["request_sha256"] != digest: raise ProbeGovernanceError("idempotency_key_payload_mismatch")
                db.commit(); return {"lease_id": prior["lease_id"], "state": prior["state"],
                    "expires_at": prior["expires_at"], "mock_only": True,
                    "live_execution_allowed": False, "network_called": False}
            reason = self._blocked(db, scopes, circuit_state, scope)
            if reason: raise ProbeGovernanceError(reason)
            approval = db.execute("""SELECT * FROM probe_approvals WHERE environment_id=? AND channel_id=? AND model_id=?
              AND starts_at<=? AND expires_at>? AND tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT 1""",
              (environment_id, channel_id, model_id, _iso(now), _iso(now),
               *scope.sql_parameters())).fetchone()
            if not approval: raise ProbeGovernanceError("approval_missing_or_expired")
            approval_use = db.execute("SELECT COUNT(*),COALESCE(SUM(cost),0) FROM probe_leases WHERE approval_id=? AND tenant_id=? AND workspace_id=?",
                                      (approval["approval_id"], *scope.sql_parameters())).fetchone()
            if approval_use[0] + 1 > approval["max_requests"] or approval_use[1] + estimated_cost > approval["max_cost"] + 1e-12:
                raise ProbeGovernanceError("approval_quota_exhausted")
            for scope_type, scope_id in scopes:
                limit = self.policy["limits"][scope_type]
                clause = {"global": "1=1", "environment": "environment_id=?", "channel": "channel_id=?", "model": "model_id=?"}[scope_type]
                args = () if scope_type == "global" else (scope_id,)
                total = db.execute(f"SELECT COUNT(*),COALESCE(SUM(cost),0) FROM probe_leases WHERE {clause} AND tenant_id=? AND workspace_id=?",
                                   (*args, *scope.sql_parameters())).fetchone()
                recent = db.execute(f"SELECT COUNT(*) FROM probe_leases WHERE {clause} AND created_at>=? AND tenant_id=? AND workspace_id=?",
                                    (*args, cutoff, *scope.sql_parameters())).fetchone()[0]
                active = db.execute(f"SELECT COUNT(*) FROM probe_leases WHERE {clause} AND state='ACTIVE' AND expires_at>? AND tenant_id=? AND workspace_id=?",
                                    (*args, _iso(now), *scope.sql_parameters())).fetchone()[0]
                if total[0] + 1 > limit["requests"] or total[1] + estimated_cost > limit["cost"] + 1e-12: raise ProbeGovernanceError(f"{scope_type}_quota_exhausted")
                if recent >= min(limit["rate_per_minute"], limit["burst"]): raise ProbeGovernanceError(f"{scope_type}_rate_limited")
                if active >= limit["concurrency"]: raise ProbeGovernanceError(f"{scope_type}_concurrency_limited")
            lease_id = f"PL-{uuid.uuid4().hex}"; expires = _iso(now + timedelta(seconds=self.policy["lease_ttl_seconds"]))
            db.execute("""INSERT INTO probe_leases(
              lease_id,idempotency_key,request_sha256,run_id,environment_id,channel_id,
              model_id,approval_id,cost,state,created_at,expires_at,completed_at,result,
              tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                lease_id, idempotency_key, digest, run_id, environment_id, channel_id, model_id,
                approval["approval_id"], estimated_cost, "ACTIVE", _iso(now), expires, None, None,
                *scope.sql_parameters()))
            result = {"lease_id": lease_id, "state": "ACTIVE", "expires_at": expires,
                      "mock_only": True, "live_execution_allowed": False, "network_called": False}
            self._audit(db, "lease_acquired", result, scope); db.commit(); return result

    def complete(self, lease_id: str, *, success: bool,
                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "scheduler.execute")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM probe_leases WHERE lease_id=? AND tenant_id=? AND workspace_id=?",
                             (lease_id, *scope.sql_parameters())).fetchone()
            if not row or row["state"] != "ACTIVE": raise ProbeGovernanceError("probe_lease_not_active")
            if _parse(row["expires_at"]) <= self.clock(): raise ProbeGovernanceError("probe_lease_expired")
            state = "SUCCEEDED" if success else "FAILED"
            db.execute("""UPDATE probe_leases SET state=?,completed_at=?,result=?
                       WHERE lease_id=? AND tenant_id=? AND workspace_id=?""",
                       (state, _iso(self.clock()), state.lower(), lease_id, *scope.sql_parameters()))
            stopped = False
            for scope_type, scope_id in self._scopes(row["environment_id"], row["channel_id"], row["model_id"]):
                db.execute("INSERT OR IGNORE INTO probe_scope_state(scope_type,scope_id,tenant_id,workspace_id) VALUES(?,?,?,?)",
                           (scope_type, scope_id, *scope.sql_parameters()))
                current = db.execute("SELECT * FROM probe_scope_state WHERE scope_type=? AND scope_id=? AND tenant_id=? AND workspace_id=?",
                                     (scope_type, scope_id, *scope.sql_parameters())).fetchone()
                consecutive = 0 if success else current["consecutive_failures"] + 1
                samples = current["sample_count"] + 1; failures = current["failure_count"] + (not success)
                stop = (consecutive >= self.policy["stop"]["maximum_consecutive_failures"] or
                        (samples >= self.policy["stop"]["minimum_samples"] and failures / samples > self.policy["stop"]["maximum_failure_rate"]))
                backoff = None if success else _iso(self.clock() + timedelta(seconds=self.deterministic_backoff_seconds(scope_id, consecutive)))
                db.execute("""UPDATE probe_scope_state SET consecutive_failures=?,sample_count=?,failure_count=?,
                  stopped=?,stop_reason=?,backoff_until=? WHERE scope_type=? AND scope_id=?
                  AND tenant_id=? AND workspace_id=?""",
                  (consecutive, samples, failures, int(stop), "failure_threshold" if stop else None,
                   backoff, scope_type, scope_id, *scope.sql_parameters())); stopped = stopped or stop
            result = {"lease_id": lease_id, "state": state, "stop_triggered": stopped, "network_called": False}
            self._audit(db, "lease_completed", result, scope); db.commit(); return result

    def set_kill_switch(self, *, scope_type: str, scope_id: str, active: bool, reason: str,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "kill_switch.activate" if active else "kill_switch.release")
        if scope_type not in {"global", "environment", "channel", "model"} or not reason: raise ProbeGovernanceError("invalid_kill_switch")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO probe_kill_switches(
              scope_type,scope_id,active,reason,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,workspace_id,scope_type,scope_id)
              DO UPDATE SET active=excluded.active,reason=excluded.reason,updated_at=excluded.updated_at""",
              (scope_type, scope_id, int(active), reason, _iso(self.clock()),
               *scope.sql_parameters()))
            if active:
                clause = {"global": "1=1", "environment": "environment_id=?", "channel": "channel_id=?", "model": "model_id=?"}[scope_type]
                args = () if scope_type == "global" else (scope_id,)
                db.execute(f"UPDATE probe_leases SET state='STOPPED',completed_at=?,result='kill_switch' WHERE state='ACTIVE' AND {clause} AND tenant_id=? AND workspace_id=?",
                           (_iso(self.clock()), *args, *scope.sql_parameters()))
            result = {"scope_type": scope_type, "scope_id": scope_id, "active": bool(active)}
            self._audit(db, "kill_switch_changed", result, scope); db.commit(); return result

    def recover(self) -> dict[str, int]:
        """Recover expired leases per authoritative tenant/workspace partition."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _iso(self.clock()); count = 0
            scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id FROM probe_leases
                                 WHERE state='ACTIVE' AND expires_at<=?""", (now,)).fetchall()
            for item in scopes:
                scope = TenantScope(item["tenant_id"], item["workspace_id"])
                cursor = db.execute("""UPDATE probe_leases SET state='EXPIRED',completed_at=?,result='lease_expired'
                                    WHERE state='ACTIVE' AND expires_at<=? AND tenant_id=? AND workspace_id=?""",
                                    (now, now, *scope.sql_parameters()))
                count += cursor.rowcount
                if cursor.rowcount:
                    self._audit(db, "restart_recovery", {"expired_leases": cursor.rowcount}, scope)
            db.commit()
        return {"expired_leases": count}

    def audit(self, *, principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._authorize(principal, "audit.read")
        with self.connect() as db: return [dict(r) for r in db.execute(
            "SELECT * FROM probe_audit WHERE tenant_id=? AND workspace_id=? ORDER BY audit_id",
            scope.sql_parameters())]

    def get_lease(self, lease_id: str, *,
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        """Return a safe lease projection for Scheduler admission checks."""
        if not lease_id:
            raise ProbeGovernanceError("probe_lease_required")
        with self.connect() as db:
            row = db.execute("""SELECT lease_id,idempotency_key,run_id,
              environment_id,channel_id,model_id,state,created_at,expires_at,
              completed_at,result FROM probe_leases
              WHERE lease_id=? AND tenant_id=? AND workspace_id=?""",
              (lease_id, *scope.sql_parameters())).fetchone()
        if row is None:
            raise ProbeGovernanceError("probe_lease_not_found")
        return {**dict(row), "network_called": False}

    list_audit = audit

    def monitoring_status(self, limit: int = 100, *,
                          principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "exploration.read")
        if not 1 <= limit <= 500: raise ProbeGovernanceError("invalid_status_limit")
        with self.connect() as db:
            counts = {row["state"]: row["count"] for row in db.execute(
                """SELECT state,COUNT(*) count FROM probe_leases
                   WHERE tenant_id=? AND workspace_id=? GROUP BY state""", scope.sql_parameters())}
            leases = db.execute("""SELECT lease_id,run_id,environment_id,channel_id,model_id,state,
              cost,created_at,expires_at,completed_at FROM probe_leases
              WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC,lease_id LIMIT ?""", (*scope.sql_parameters(), limit)).fetchall()
            switches = db.execute("""SELECT scope_type,scope_id,active,updated_at FROM probe_kill_switches
                                  WHERE tenant_id=? AND workspace_id=? ORDER BY scope_type,scope_id""",
                                  scope.sql_parameters()).fetchall()
            stopped = db.execute("""SELECT scope_type,scope_id,consecutive_failures,sample_count,
              failure_count,stopped,backoff_until FROM probe_scope_state
              WHERE (stopped=1 OR backoff_until IS NOT NULL)
              AND tenant_id=? AND workspace_id=? ORDER BY scope_type,scope_id""",
              scope.sql_parameters()).fetchall()
            active_approvals = db.execute("""SELECT COUNT(*) FROM probe_approvals
                                          WHERE starts_at<=? AND expires_at>?
                                          AND tenant_id=? AND workspace_id=?""",
                                          (_iso(self.clock()), _iso(self.clock()), *scope.sql_parameters())).fetchone()[0]
        return {"status": "disabled" if not self.policy["enabled"] else "mock_only",
                "capability_id": "ADV-018", "enabled": bool(self.policy["enabled"]),
                "mode": "mock_only", "live_execution_allowed": False,
                "policy_version": self.policy["policy_version"], "state_counts": counts,
                "active_approval_count": active_approvals, "active_lease_count": counts.get("ACTIVE", 0),
                "limits": self.policy["limits"], "backoff_policy": self.policy["backoff"],
                "stop_policy": self.policy["stop"], "items": [dict(row) for row in leases],
                "stopped_scopes": [dict(row) for row in stopped], "kill_switches": [dict(row) for row in switches],
                "authorization_status": "identity_provider_and_separate_authorization_required",
                "network_called": False}

    def monitoring_audit(self, limit: int = 100, *,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "audit.read")
        if not 1 <= limit <= 500: raise ProbeGovernanceError("invalid_audit_limit")
        with self.connect() as db:
            rows = db.execute("""SELECT audit_id,event_type,created_at FROM probe_audit
                              WHERE tenant_id=? AND workspace_id=? ORDER BY audit_id DESC LIMIT ?""",
                              (*scope.sql_parameters(), limit)).fetchall()
        return {"status": "ready", "capability_id": "ADV-018", "items": [dict(row) for row in rows], "network_called": False}
