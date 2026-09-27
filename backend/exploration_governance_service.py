"""Offline, fail-closed governance for shadow-only controlled exploration."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope


class ExplorationGovernanceError(ValueError):
    """A controlled-exploration request failed a governance rule."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_exploration_policy(path: str | Path) -> dict[str, Any]:
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExplorationGovernanceError("invalid_exploration_policy") from exc
    required = {"policy_version", "feature_flag_env", "mode", "allowed_environments",
                "allowed_channels", "allowed_models", "selector", "approval", "budgets",
                "stops", "allowed_circuit_states", "real_execution_allowed"}
    if not isinstance(policy, dict) or not required.issubset(policy):
        raise ExplorationGovernanceError("invalid_exploration_policy")
    if policy["mode"] != "shadow_only" or policy["real_execution_allowed"] is not False:
        raise ExplorationGovernanceError("unsafe_exploration_policy")
    if not all(isinstance(policy[k], list) for k in
               ("allowed_environments", "allowed_channels", "allowed_models")):
        raise ExplorationGovernanceError("invalid_exploration_allowlists")
    if policy["selector"].get("algorithm") != "deterministic_epsilon_ucb_v1":
        raise ExplorationGovernanceError("invalid_exploration_selector")
    epsilon = policy["selector"].get("epsilon")
    if not isinstance(epsilon, (int, float)) or not 0 <= epsilon <= 1:
        raise ExplorationGovernanceError("invalid_exploration_epsilon")
    if set(policy["budgets"]) != {"per_run", "per_environment", "per_channel", "per_model"}:
        raise ExplorationGovernanceError("invalid_exploration_budgets")
    for budget in policy["budgets"].values():
        if (not isinstance(budget, dict) or not isinstance(budget.get("requests"), int)
                or budget["requests"] < 0 or not isinstance(budget.get("cost"), (int, float))
                or budget["cost"] < 0):
            raise ExplorationGovernanceError("invalid_exploration_budgets")
    if policy["approval"].get("required") is not True:
        raise ExplorationGovernanceError("approval_must_be_required")
    policy["_policy_sha256"] = hashlib.sha256(json.dumps(policy, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return policy


class ExplorationGovernanceService:
    """Persists approvals, budgets, kill switches and an append-only audit locally."""

    def __init__(self, database_path: str | Path, policy_path: str | Path, *,
                 clock: Callable[[], datetime] = _now, environ: Mapping[str, str] | None = None,
                 authorization_service: AuthorizationService | None = None):
        self.path = Path(database_path)
        self.policy = load_exploration_policy(policy_path)
        self.clock = clock
        self.environ = os.environ if environ is None else environ
        self.authorization_service = authorization_service
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _scope(self, principal: PrincipalContext | None, permission: str,
               *, scheduler_only: bool = False) -> TenantScope:
        if self.authorization_service is None:
            return TenantScope.local_development()
        verified = self.authorization_service.authorize(principal, permission)
        if scheduler_only and (verified.principal_type is not PrincipalType.SERVICE
                               or "scheduler_service" not in verified.roles):
            raise PermissionError("scheduler_service_identity_required")
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def _connection(self):
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init(self) -> None:
        with self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS exploration_approvals(
              approval_id TEXT NOT NULL, environment_id TEXT NOT NULL,
              channel_id TEXT NOT NULL, model_id TEXT NOT NULL, approved_by TEXT NOT NULL,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,approval_id));
            CREATE TABLE IF NOT EXISTS exploration_consumption(
              idempotency_key TEXT NOT NULL, run_id TEXT NOT NULL,
              environment_id TEXT NOT NULL, channel_id TEXT NOT NULL, model_id TEXT NOT NULL,
              requests INTEGER NOT NULL, cost REAL NOT NULL, created_at TEXT NOT NULL,
              response_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,idempotency_key));
            CREATE TABLE IF NOT EXISTS exploration_kill_switches(
              scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, active INTEGER NOT NULL,
              reason TEXT NOT NULL, updated_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              UNIQUE(tenant_id,workspace_id,scope_type,scope_id));
            CREATE TABLE IF NOT EXISTS exploration_audit(
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
            CREATE TABLE IF NOT EXISTS exploration_decisions(
              decision_id TEXT NOT NULL,request_id TEXT NOT NULL,run_id TEXT NOT NULL,
              environment_id TEXT NOT NULL,channel_id TEXT NOT NULL,model_id TEXT NOT NULL,
              selected_candidate_id TEXT NOT NULL,selector TEXT NOT NULL,
              idempotency_key TEXT,created_at TEXT NOT NULL,decision_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,decision_id),
              UNIQUE(tenant_id,workspace_id,idempotency_key));
            """)
            for table in ("exploration_approvals", "exploration_consumption",
                          "exploration_kill_switches", "exploration_audit",
                          "exploration_decisions"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'")
                if "workspace_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'")
                db.execute(f"CREATE INDEX IF NOT EXISTS ix_scope_{table} ON {table}(tenant_id,workspace_id)")

    def _audit(self, db: sqlite3.Connection, event: str, details: Mapping[str, Any],
               scope: TenantScope | None = None) -> None:
        scope = scope or TenantScope.local_development()
        db.execute("INSERT INTO exploration_audit(event_type,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?)",
                   (event, _iso(self.clock()), json.dumps(dict(details), sort_keys=True),
                    *scope.sql_parameters()))

    def approve(self, *, environment_id: str, channel_id: str, model_id: str,
                approved_by: str, ttl_seconds: int, approval_id: str | None = None,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "exploration.manage")
        maximum = int(self.policy["approval"]["maximum_ttl_seconds"])
        if not approved_by or not 0 < ttl_seconds <= maximum:
            raise ExplorationGovernanceError("invalid_approval")
        created = self.clock(); identifier = approval_id or str(uuid.uuid4())
        result = {"approval_id": identifier, "environment_id": environment_id,
                  "channel_id": channel_id, "model_id": model_id, "approved_by": approved_by,
                  "created_at": _iso(created), "expires_at": _iso(created + timedelta(seconds=ttl_seconds))}
        with self._connection() as db:
            try:
                db.execute("""INSERT INTO exploration_approvals(
                  approval_id,environment_id,channel_id,model_id,approved_by,created_at,
                  expires_at,revoked_at,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,NULL,?,?)""",
                  (*tuple(result.values()), *scope.sql_parameters()))
            except sqlite3.IntegrityError as exc:
                raise ExplorationGovernanceError("duplicate_approval_id") from exc
            self._audit(db, "approval_created", result, scope)
        return result

    def set_kill_switch(self, *, active: bool, reason: str, scope_type: str = "global",
                        scope_id: str = "*",
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "kill_switch.activate" if active else "kill_switch.release")
        if scope_type not in {"global", "environment", "channel", "model"} or not reason:
            raise ExplorationGovernanceError("invalid_kill_switch")
        result = {"scope_type": scope_type, "scope_id": scope_id, "active": bool(active),
                  "reason": reason, "updated_at": _iso(self.clock())}
        with self._connection() as db:
            db.execute("""INSERT INTO exploration_kill_switches(
              scope_type,scope_id,active,reason,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(tenant_id,workspace_id,scope_type,scope_id)
              DO UPDATE SET active=excluded.active,
              reason=excluded.reason,updated_at=excluded.updated_at""",
              (*tuple(result.values()), *scope.sql_parameters()))
            self._audit(db, "kill_switch_changed", result, scope)
        return result

    def revoke_approval(self, approval_id: str, *, reason: str,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "exploration.manage")
        if not approval_id or not reason:
            raise ExplorationGovernanceError("invalid_approval_revocation")
        revoked_at = _iso(self.clock())
        with self._connection() as db:
            cursor = db.execute("""UPDATE exploration_approvals SET revoked_at=?
              WHERE approval_id=? AND revoked_at IS NULL AND tenant_id=? AND workspace_id=?""",
              (revoked_at, approval_id, *scope.sql_parameters()))
            if cursor.rowcount != 1:
                raise ExplorationGovernanceError("approval_not_active")
            result = {"approval_id": approval_id, "revoked_at": revoked_at, "reason": reason}
            self._audit(db, "approval_revoked", result, scope)
        return result

    def _decision(self, *, run_id: str, environment_id: str, channel_id: str, model_id: str,
                  health: Mapping[str, Any] | None,
                  circuit_state: str = "UNKNOWN",
                  scope: TenantScope | None = None) -> tuple[bool, list[str]]:
        scope = scope or TenantScope.local_development()
        reasons: list[str] = []
        flag = self.environ.get(self.policy["feature_flag_env"], "")
        if flag != self.policy.get("enabled_value", "1"): reasons.append("feature_disabled")
        for value, key in ((environment_id, "allowed_environments"), (channel_id, "allowed_channels"),
                           (model_id, "allowed_models")):
            if value not in self.policy[key]: reasons.append(key.replace("allowed_", "") + "_not_allowed")
        with self._connection() as db:
            kills = db.execute("SELECT scope_type,scope_id FROM exploration_kill_switches WHERE active=1 AND tenant_id=? AND workspace_id=?",
                               scope.sql_parameters()).fetchall()
            scopes = {("global", "*"), ("environment", environment_id),
                      ("channel", channel_id), ("model", model_id)}
            if any((r["scope_type"], r["scope_id"]) in scopes for r in kills): reasons.append("kill_switch_active")
            approval = db.execute("""SELECT 1 FROM exploration_approvals WHERE environment_id=?
              AND channel_id=? AND model_id=? AND revoked_at IS NULL AND expires_at>?
              AND tenant_id=? AND workspace_id=? LIMIT 1""",
              (environment_id, channel_id, model_id, _iso(self.clock()),
               *scope.sql_parameters())).fetchone()
            if not approval: reasons.append("approval_missing_or_expired")
        stops = self.policy["stops"]; health = health or {}
        if circuit_state not in self.policy["allowed_circuit_states"]: reasons.append("circuit_stop")
        if health.get("state") not in stops["allowed_health_states"]: reasons.append("health_stop")
        samples = int(health.get("samples", 0) or 0)
        if samples >= stops["minimum_samples_for_failure_stop"] and float(health.get("failure_rate", 1)) > stops["maximum_failure_rate"]:
            reasons.append("failure_stop")
        if float(health.get("latency_ms", math.inf)) > stops["maximum_latency_ms"]: reasons.append("latency_stop")
        return not reasons, reasons

    def evaluate(self, *, principal: PrincipalContext | None = None,
                 **kwargs: Any) -> dict[str, Any]:
        scope = self._scope(principal, "exploration.read")
        allowed, reasons = self._decision(**kwargs, scope=scope)
        result = {"allowed": allowed, "mode": "shadow_only", "shadow_recommendation_allowed": allowed,
                  "real_execution_allowed": False, "reasons": reasons,
                  "policy_version": self.policy["policy_version"], "network_called": False}
        with self._connection() as db: self._audit(db, "eligibility_evaluated", result, scope)
        return result

    def recommend(self, *, candidates: Sequence[Mapping[str, Any]], request_id: str,
                  run_id: str, environment_id: str, channel_id: str, model_id: str,
                  health: Mapping[str, Any] | None = None,
                  circuit_state: str = "UNKNOWN",
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "exploration.read")
        gate = self.evaluate(run_id=run_id, environment_id=environment_id, channel_id=channel_id,
                             model_id=model_id, health=health, circuit_state=circuit_state,
                             principal=principal)
        if not gate["allowed"] or not candidates:
            return {**gate, "selected": None}
        normalized = sorted((dict(c) for c in candidates), key=lambda c: str(c.get("id", "")))
        if any(not c.get("id") for c in normalized):
            raise ExplorationGovernanceError("invalid_candidate")
        digest = hashlib.sha256(f'{self.policy["selector"]["seed"]}:{request_id}'.encode()).digest()
        fraction = int.from_bytes(digest[:8], "big") / 2**64
        explore = fraction < self.policy["selector"]["epsilon"]
        if explore:
            index = int.from_bytes(digest[8:16], "big") % len(normalized); selected = normalized[index]
        else:
            total = max(1, sum(int(c.get("trials", 0)) for c in normalized))
            weight = float(self.policy["selector"]["ucb_exploration_weight"])
            def score(c: Mapping[str, Any]) -> tuple[float, str]:
                trials = int(c.get("trials", 0)); mean = float(c.get("mean_reward", 0))
                return ((math.inf if trials == 0 else mean + weight * math.sqrt(math.log(total + 1) / trials)), str(c["id"]))
            selected = max(normalized, key=score)
        selected_channel = str(selected.get("channel_id") or "")
        selected_model = str(selected.get("model_id") or model_id)
        if not selected_channel:
            return {**gate, "allowed": False, "shadow_recommendation_allowed": False,
                    "selected": None, "reasons": [*gate["reasons"], "selected_candidate_scope_missing"]}
        candidate_allowed, candidate_reasons = self._decision(
            run_id=run_id, environment_id=environment_id, channel_id=selected_channel,
            model_id=selected_model, health=(selected.get("health") or health),
            circuit_state=str(selected.get("circuit_state") or circuit_state), scope=scope)
        if not candidate_allowed:
            return {**gate, "allowed": False, "shadow_recommendation_allowed": False,
                    "selected": None, "reasons": [f"selected_candidate:{x}" for x in candidate_reasons]}
        return {**gate, "selected": selected["id"], "selected_channel_id": selected_channel,
                "selected_model_id": selected_model, "selector": "epsilon" if explore else "ucb",
                "request_id": request_id, "decision_contract_version": "shadow_exploration_decision_v1",
                "budget_reservation_required": True, "execution_authorized": False}

    def reserve_recommendation(self, *, idempotency_key: str, requests: int = 1,
                               cost: float = 0.0,
                               principal: PrincipalContext | None = None,
                               **recommendation: Any) -> dict[str, Any]:
        """Return a Scheduler-consumable decision only after persistent budget consumption."""
        scope = self._scope(principal, "scheduler.decide", scheduler_only=True)
        if self.authorization_service is not None:
            idempotency_key = scope.idempotency_key(
                operation="exploration_reserve", external_key=idempotency_key,
                version="v1")
        result = self.recommend(**recommendation, principal=principal)
        if not result.get("selected"):
            return result
        consumed = self.consume(idempotency_key=idempotency_key,
            run_id=recommendation["run_id"], environment_id=recommendation["environment_id"],
            channel_id=result["selected_channel_id"], model_id=result["selected_model_id"],
            requests=requests, cost=cost, principal=principal)
        decision_id = "EXD-" + hashlib.sha256(
            f'{idempotency_key}:{result["selected"]}'.encode()).hexdigest()[:24]
        final = {**result, "decision_id": decision_id, "budget": consumed,
                 "budget_reservation_required": False, "execution_authorized": False}
        with self._connection() as db:
            try:
                db.execute("""INSERT INTO exploration_decisions(
                  decision_id,request_id,run_id,environment_id,channel_id,model_id,
                  selected_candidate_id,selector,idempotency_key,created_at,decision_json,
                  tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    decision_id, recommendation["request_id"], recommendation["run_id"],
                    recommendation["environment_id"], result["selected_channel_id"],
                    result["selected_model_id"], result["selected"], result["selector"],
                    idempotency_key, _iso(self.clock()), json.dumps(final, sort_keys=True),
                    *scope.sql_parameters()))
                self._audit(db, "shadow_decision_reserved", {"decision_id": decision_id,
                    "run_id": recommendation["run_id"], "environment_id": recommendation["environment_id"],
                    "channel_id": result["selected_channel_id"], "model_id": result["selected_model_id"],
                    "selected": result["selected"]}, scope)
            except sqlite3.IntegrityError:
                row = db.execute("SELECT decision_json FROM exploration_decisions WHERE idempotency_key=? AND tenant_id=? AND workspace_id=?",
                                 (idempotency_key, *scope.sql_parameters())).fetchone()
                if row: return json.loads(row[0])
                raise
        return final

    def consume(self, *, idempotency_key: str, run_id: str, environment_id: str,
                channel_id: str, model_id: str, requests: int = 1, cost: float = 0.0,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "scheduler.execute", scheduler_only=True)
        if self.authorization_service is not None:
            idempotency_key = scope.idempotency_key(
                operation="exploration_consume", external_key=idempotency_key,
                version="v1")
        if not idempotency_key or requests <= 0 or cost < 0:
            raise ExplorationGovernanceError("invalid_consumption")
        scopes = (("per_run", "run_id", run_id), ("per_environment", "environment_id", environment_id),
                  ("per_channel", "channel_id", channel_id), ("per_model", "model_id", model_id))
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM exploration_consumption WHERE idempotency_key=? AND tenant_id=? AND workspace_id=?",
                               (idempotency_key, *scope.sql_parameters())).fetchone()
            if prior:
                same = (prior["run_id"], prior["environment_id"], prior["channel_id"],
                        prior["model_id"], prior["requests"], prior["cost"]) == (
                            run_id, environment_id, channel_id, model_id, requests, cost)
                if not same:
                    db.rollback()
                    raise ExplorationGovernanceError("idempotency_key_payload_mismatch")
                db.commit(); return json.loads(prior["response_json"])
            for policy_scope, column, value in scopes:
                used = db.execute(f"SELECT COALESCE(SUM(requests),0),COALESCE(SUM(cost),0) FROM exploration_consumption WHERE {column}=? AND tenant_id=? AND workspace_id=?",
                                  (value, *scope.sql_parameters())).fetchone()
                limit = self.policy["budgets"][policy_scope]
                if used[0] + requests > limit["requests"] or used[1] + cost > limit["cost"] + 1e-12:
                    db.rollback(); raise ExplorationGovernanceError(f"{policy_scope}_budget_exhausted")
            result = {"status": "consumed", "idempotency_key": idempotency_key, "requests": requests,
                      "cost": cost, "mode": "shadow_only", "real_execution_allowed": False}
            db.execute("""INSERT INTO exploration_consumption(
              idempotency_key,run_id,environment_id,channel_id,model_id,requests,cost,
              created_at,response_json,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (idempotency_key, run_id, environment_id, channel_id, model_id, requests, cost,
                        _iso(self.clock()), json.dumps(result, sort_keys=True),
                        *scope.sql_parameters()))
            self._audit(db, "budget_consumed", result, scope); db.commit()
            return result

    def list_audit(self, limit: int = 100, *,
                   principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal, "audit.read")
        if not 1 <= limit <= 1000: raise ExplorationGovernanceError("invalid_audit_limit")
        with self._connection() as db:
            rows = db.execute("SELECT * FROM exploration_audit WHERE tenant_id=? AND workspace_id=? ORDER BY audit_id DESC LIMIT ?",
                              (*scope.sql_parameters(), limit)).fetchall()
        return [{**dict(r), "details": json.loads(r["details_json"])} for r in rows]

    def status(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "exploration.read")
        """Bounded read-only governance state; approval identities stay opaque."""
        now = _iso(self.clock())
        with self._connection() as db:
            approvals = db.execute("""SELECT approval_id,environment_id,channel_id,
              model_id,created_at,expires_at,revoked_at FROM exploration_approvals
              WHERE tenant_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 100""",
              scope.sql_parameters()).fetchall()
            switches = db.execute("""SELECT scope_type,scope_id,active,updated_at
              FROM exploration_kill_switches WHERE tenant_id=? AND workspace_id=?
              ORDER BY scope_type,scope_id""", scope.sql_parameters()).fetchall()
            usage = db.execute("""SELECT COALESCE(SUM(requests),0) AS requests,
              COALESCE(SUM(cost),0) AS cost FROM exploration_consumption
              WHERE tenant_id=? AND workspace_id=?""", scope.sql_parameters()).fetchone()
            decisions = db.execute("""SELECT decision_id,request_id,run_id,environment_id,
              channel_id,model_id,selected_candidate_id,selector,created_at
              FROM exploration_decisions WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC,decision_id LIMIT 100""",
              scope.sql_parameters()).fetchall()
        return {
            "status": "blocked" if self.policy["mode"] == "shadow_only" else "ready",
            "mode": self.policy["mode"],
            "feature_enabled": self.environ.get(
                self.policy["feature_flag_env"], "") == self.policy.get("enabled_value", "1"),
            "real_execution_allowed": False,
            "policy_version": self.policy["policy_version"],
            "approvals": [{**dict(row), "active": (
                row["revoked_at"] is None and row["expires_at"] > now)} for row in approvals],
            "kill_switches": [dict(row) for row in switches],
            "usage": {"requests": int(usage["requests"]), "cost": float(usage["cost"])},
            "budgets": self.policy["budgets"],
            "stop_policy": self.policy["stops"],
            "allowed_circuit_states": self.policy["allowed_circuit_states"],
            "decisions": [dict(row) for row in decisions],
            "authorization_status": "identity_provider_and_separate_authorization_required",
            "network_called": False,
        }

    def monitoring_audit(self, limit: int = 100, *,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "audit.read")
        if not 1 <= limit <= 500: raise ExplorationGovernanceError("invalid_audit_limit")
        with self._connection() as db:
            rows = db.execute("""SELECT audit_id,event_type,created_at FROM exploration_audit
                              WHERE tenant_id=? AND workspace_id=? ORDER BY audit_id DESC LIMIT ?""",
                              (*scope.sql_parameters(), limit)).fetchall()
        return {"status": "ready", "items": [dict(row) for row in rows], "network_called": False}
